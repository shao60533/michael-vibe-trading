"""Unit tests for the pure (no-I/O) helpers in mcp_launcher.

Imported via tests/conftest.py, which installs stub modules for the
container-only dependencies so the real source loads in CI.
"""
import time

import mcp_launcher as L


# ─────────── run_id validation (path-traversal guard) ───────────

def test_valid_run_id_accepts_real_format():
    assert L._valid_run_id("swarm-20260506-171102-016a0768") is True
    assert L._valid_run_id("swarm-20260506-171102-016a0768abcd") is True


def test_valid_run_id_rejects_traversal_and_junk():
    for bad in ("../../etc", "a/b", "a\\b", "swarm-2026..0768", "",
                "swarm-20260506-171102-016A0768",  # uppercase hex
                "swarm-2026-1711-016a0768",          # wrong digit groups
                "../swarm-20260506-171102-016a0768"):
        assert L._valid_run_id(bad) is False, bad


# ─────────── base64url + JWT (stdlib HS256) ───────────

def test_b64url_roundtrip():
    for raw in (b"", b"a", b"abc", b"\x00\xff\x10", b"hello world"):
        assert L._b64url_decode(L._b64url(raw)) == raw


def test_jwt_roundtrip_and_expiry():
    now = int(time.time())
    tok = L._jwt_encode({"typ": "access", "iat": now, "exp": now + 3600})
    payload = L._jwt_decode(tok)
    assert payload is not None and payload["typ"] == "access"

    expired = L._jwt_encode({"typ": "access", "iat": now - 10, "exp": now - 1})
    assert L._jwt_decode(expired) is None


def test_jwt_rejects_tamper():
    now = int(time.time())
    tok = L._jwt_encode({"typ": "access", "exp": now + 3600})
    head, payload, sig = tok.split(".")
    tampered = f"{head}.{payload}.{sig[:-2]}xx"
    assert L._jwt_decode(tampered) is None
    assert L._jwt_decode("not-a-jwt") is None


# ─────────── OAuth redirect_uri binding (P1-6) ───────────

def _make_client_id(uris):
    now = int(time.time())
    return "mcp-" + L._jwt_encode({"typ": "client", "redirect_uris": uris,
                                    "iat": now, "exp": now + 3600})


def test_redirect_uri_registered_exact_match():
    cid = _make_client_id(["https://app/cb", "myapp://cb"])
    assert L._client_redirect_uris(cid) == ["https://app/cb", "myapp://cb"]
    assert L._redirect_uri_registered(cid, "https://app/cb") is True
    assert L._redirect_uri_registered(cid, "myapp://cb") is True


def test_redirect_uri_rejects_unregistered_and_legacy():
    cid = _make_client_id(["https://app/cb"])
    assert L._redirect_uri_registered(cid, "https://evil/cb") is False
    # Legacy / unknown client_id (not a signed token) is rejected outright.
    assert L._client_redirect_uris("mcp-randomlegacy") is None
    assert L._redirect_uri_registered("mcp-randomlegacy", "https://app/cb") is False
    # A client that registered no redirect_uri cannot authorize.
    empty = _make_client_id([])
    assert L._redirect_uri_registered(empty, "https://app/cb") is False


# ─────────── ticker extraction (regex fallback) ───────────

def test_extract_target_cn_shanghai_and_shenzhen():
    assert L._extract_target("分析 600519") == ("600519.SH", "CN")
    assert L._extract_target("看下 000333") == ("000333.SZ", "CN")


def test_extract_target_hk_crypto_us_and_none():
    assert L._extract_target("买点 1810.HK") == ("1810.HK", "HK")
    assert L._extract_target("BTC 怎么样") == ("BTC-USD", "CRYPTO")
    assert L._extract_target("buy AAPL now") == ("AAPL", "US")
    assert L._extract_target("hello there") == (None, None)


def test_extract_target_blacklists_common_words():
    # "CEO" / "ETF" look like tickers but are blacklisted.
    assert L._extract_target("the CEO said") == (None, None)


# ─────────── intent classification + explicit preset ───────────

def test_classify_preset_keyword_and_default():
    assert L._classify_preset("帮我做技术面分析", "investment_committee") == "technical_analysis_panel"
    assert L._classify_preset("看下财报", "investment_committee") == "earnings_research_desk"
    assert L._classify_preset("随便聊聊", "investment_committee") == "investment_committee"


def test_parse_explicit_preset():
    preset, cleaned = L._parse_explicit_preset("preset:technical_analysis_panel SOXL")
    assert preset == "technical_analysis_panel"
    assert cleaned == "SOXL"
    assert L._parse_explicit_preset("just analyze AAPL") == (None, "just analyze AAPL")


# ─────────── mention stripping ───────────

def test_strip_mentions():
    assert L._strip_mentions('<at user_id="ou_x">Bob</at> 分析 AAPL') == "分析 AAPL"
    assert L._strip_mentions("@someone 看下 茅台") == "看下 茅台"


# ─────────── markdown → Feishu blocks / inline ───────────

def test_md_to_feishu_blocks_types():
    md = "## 标题\n\n- 第一条\n- 第二条\n\n---\n\n正文段落"
    blocks = L._md_to_feishu_blocks(md)
    types_seen = {b.get("block_type") for b in blocks}
    assert 4 in types_seen   # heading2
    assert 12 in types_seen  # bullet
    assert 22 in types_seen  # divider
    assert 2 in types_seen   # text paragraph


def test_md_table_to_bullets():
    md = "| 列A | 列B |\n|---|---|\n| 1 | 2 |"
    blocks = L._md_to_feishu_blocks(md)
    # table rows render as bullets (block_type 12)
    assert any(b.get("block_type") == 12 for b in blocks)


def test_parse_inline_md_styles():
    runs = L._parse_inline_md("**粗** 普通 `代码`")
    styles = [r["text_run"]["text_element_style"] for r in runs]
    assert {"bold": True} in styles
    assert {"inline_code": True} in styles


def test_md_blocks_respect_max():
    md = "\n\n".join(f"段落 {i}" for i in range(200))
    blocks = L._md_to_feishu_blocks(md, max_blocks=10)
    assert len(blocks) <= 10
