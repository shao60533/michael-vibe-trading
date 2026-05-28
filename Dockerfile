# Vibe-Trading MCP — remote (SSE+Bearer/OAuth) + Feishu webhook deployment.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Build deps for some scientific wheels (numba/llvmlite, scipy, lxml)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pin to the version we tested locally.
RUN pip install --no-cache-dir \
    vibe-trading-ai==0.1.6 \
    uvicorn[standard] \
    python-multipart \
    stockstats \
    pandas \
    numpy \
    scikit-learn \
    lightgbm

# a-stock-data skill needs mootdx (TCP 7709 quote client).
# mootdx pulls httpx[socks]<0.26 which conflicts with langgraph's newer httpx,
# triggering pip resolver to backtrack until the build daemon times out.
# Skill only uses mootdx.quotes.Quotes (TCP via pytdx); httpx[socks] is unused.
# Install with --no-deps and pin pytdx explicitly.
RUN pip install --no-cache-dir --no-deps mootdx pytdx

COPY mcp_launcher.py /app/mcp_launcher.py
COPY factor_analysis/ /app/factor_analysis/
COPY sequoia_x/ /app/sequoia_x/
COPY hot_event_research/ /app/hot_event_research/
COPY khunter_x/ /app/khunter_x/

# Add-on skills (e.g., a-stock-data). Installed into vibe-trading-ai's bundled
# skills dir so SkillsLoader picks them up regardless of runtime user / HOME.
COPY skills/ /tmp/extra_skills/

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
