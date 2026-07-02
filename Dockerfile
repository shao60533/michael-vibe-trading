# Vibe-Trading MCP — remote (SSE+Bearer/OAuth) + Feishu webhook deployment.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Build deps for some scientific wheels (numba/llvmlite, scipy, lxml).
# fonts-noto-cjk: 收盘复盘长图(Pillow)渲染中文所需字体。
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl ca-certificates libgomp1 fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pin to the version we tested locally.
RUN pip install --no-cache-dir \
    vibe-trading-ai==0.1.10 \
    uvicorn[standard] \
    python-multipart \
    stockstats \
    pandas \
    numpy \
    scikit-learn \
    lightgbm \
    Pillow

# a-stock-data skill needs mootdx (TCP 7709 quote client).
# mootdx pulls httpx[socks]<0.26 which conflicts with langgraph's newer httpx,
# triggering pip resolver to backtrack until the build daemon times out.
# Skill only uses mootdx.quotes.Quotes (TCP via pytdx); httpx[socks] is unused.
# Install with --no-deps and pin pytdx explicitly.
RUN pip install --no-cache-dir --no-deps mootdx pytdx

# 成本优化:investment_committee swarm 里"行情/工具获取"的两个研究员(bull/bear,
# 工具含 get_market_data+factor_analysis、迭代最多)改用便宜的 deepseek-chat(flash);
# 风控官 + 基金经理(纯研判)不设 model_name → 继承全局 LANGCHAIN_MODEL_NAME(pro)。
# 就地给装好的 preset 注入 per-agent model_name(worker 原生支持 SwarmAgentSpec.model_name)。
RUN PRESET="$(find / -path '*/src/swarm/presets/investment_committee.yaml' 2>/dev/null | head -1)" \
    && test -n "$PRESET" \
    && sed -i '/^  - id: bull_advocate$/a\    model_name: deepseek-chat' "$PRESET" \
    && sed -i '/^  - id: bear_advocate$/a\    model_name: deepseek-chat' "$PRESET" \
    # 决策 agent portfolio_manager 去掉 backtest → 变纯综合角色(仅通用工具),
    # 否则被判 data-agent、只输出文本决策会触发 "no tool evidence" 契约失败(task-decision failed)。
    && sed -i 's/    tools: \[bash, read_file, write_file, load_skill, backtest\]/    tools: [bash, read_file, write_file, load_skill]/' "$PRESET" \
    && echo "=== committee per-agent model_name + decision tools ===" \
    && grep -nE "^  - id:|model_name:|backtest" "$PRESET"

COPY mcp_launcher.py /app/mcp_launcher.py
COPY factor_analysis/ /app/factor_analysis/
COPY sequoia_x/ /app/sequoia_x/
COPY hot_event_research/ /app/hot_event_research/
COPY khunter_x/ /app/khunter_x/
COPY intraday/ /app/intraday/

# Add-on skills (e.g., a-stock-data). Installed into vibe-trading-ai's bundled
# skills dir so SkillsLoader picks them up regardless of runtime user / HOME.
COPY skills/ /tmp/extra_skills/

# Add-on swarm presets (e.g., value_chain_teardown_team). Presets load ONLY from
# the package's src/swarm/presets dir (no custom path), so copy ours in there.
COPY swarm_presets/ /tmp/extra_presets/
RUN PRESETS_DIR="$(python -c "from src.swarm.presets import PRESETS_DIR; print(PRESETS_DIR)")" \
    && echo "swarm presets dir = $PRESETS_DIR" \
    && cp /tmp/extra_presets/*.yaml "$PRESETS_DIR/" \
    && for f in /tmp/extra_presets/*.yaml; do \
         n=$(basename "$f" .yaml); \
         python -c "from src.swarm.presets import load_preset; load_preset('$n')" \
           || { echo "PRESET LOAD FAILED: $n"; exit 1; }; \
         echo "✓ preset installed: $n"; \
       done

# Non-root setup + skill installation + writable state dirs.
RUN useradd --create-home --shell /usr/sbin/nologin vibe \
    && mkdir -p /app/runs /app/sessions \
    && AGENT_DIR=$(python -c "import mcp_server, pathlib; print(pathlib.Path(mcp_server.__file__).resolve().parent)") \
    && for d in ".swarm/runs" "runs" "sessions" "uploads"; do \
         mkdir -p "$AGENT_DIR/$d" && chown -R vibe:vibe "$AGENT_DIR/$d"; \
       done \
    && SKILLS_DIR=$(python -c "from src.agent.skills import SkillsLoader; print(SkillsLoader().skills_dir)") \
    && echo "SkillsLoader.skills_dir = $SKILLS_DIR" \
    && mkdir -p "$SKILLS_DIR" \
    && cp -r /tmp/extra_skills/* "$SKILLS_DIR/" \
    && for d in /tmp/extra_skills/*/; do \
         name=$(basename "$d"); \
         test -f "$SKILLS_DIR/$name/SKILL.md" || { echo "MISSING: $name/SKILL.md"; exit 1; }; \
         echo "✓ skill installed: $name"; \
       done \
    && chown -R vibe:vibe /app
USER vibe

EXPOSE 8000
ENV PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/healthz || exit 1

CMD ["python", "/app/mcp_launcher.py"]
