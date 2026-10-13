"""Gap-closing tests: SSRF env gating, JWT expiry, provider HTTP parsing (mocked), file selection, storage keys."""
import logging
from types import SimpleNamespace

import httpx
import pytest

from app.db import session_scope
from app.llm import providers
from app.llm.providers import GeminiProvider, OllamaProvider, ProviderError
from app.models import User
from app.pipeline import summarize
from app.pipeline.evaluate import select_files
from app.security import ssrf
from app.storage.local import LocalStorage


# ---- SSRF_ALLOW_PRIVATE is honored only when APP_ENV=test ----
def test_ssrf_allow_private_honored_in_test_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE", "true")
    assert ssrf.validate_url("http://127.0.0.1:9/x") == "http://127.0.0.1:9/x"


@pytest.mark.parametrize("env", ["dev", "prod", "production", ""])
def test_ssrf_allow_private_ignored_outside_test_env(monkeypatch, caplog, env):
    monkeypatch.setenv("APP_ENV", env)
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE", "true")
    monkeypatch.setattr(ssrf, "_warned", False)
    monkeypatch.setattr(ssrf, "get_settings", lambda: SimpleNamespace(app_env="dev"))
    with caplog.at_level(logging.WARNING, logger="ssrf"):
        with pytest.raises(ssrf.SSRFBlocked):
            ssrf.validate_url("http://127.0.0.1:9/x")  # http scheme and loopback: both must stay blocked
        with pytest.raises(ssrf.SSRFBlocked, match="non-public"):
            ssrf.validate_url("https://127.0.0.1/x")
    assert any("SSRF_ALLOW_PRIVATE" in r.message and "ignoring" in r.message for r in caplog.records)


def test_ssrf_allow_private_not_in_env_example():
    from pathlib import Path
    assert "SSRF_ALLOW_PRIVATE" not in (Path(__file__).parent.parent / ".env.example").read_text()


# ---- JWT expiry ----
def test_expired_token_rejected(api):
    from app.auth import create_token
    from sqlalchemy import select
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == "student1@example.com"))
        expired, valid = create_token(user, ttl_minutes=-1), create_token(user)
    assert api.c.get("/submissions", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    assert api.c.get("/submissions", headers={"Authorization": f"Bearer {valid}"}).status_code == 200


def test_tampered_token_rejected(api):
    tok = api.h("student1@example.com")["Authorization"].split()[1]
    bad = tok[:-3] + ("AAA" if not tok.endswith("AAA") else "BBB")
    assert api.c.get("/submissions", headers={"Authorization": f"Bearer {bad}"}).status_code == 401


# ---- ollama / gemini: request and response parsing against mocked HTTP (NOT live provider runs) ----
def _resp(status, payload):
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "http://x"))


def test_ollama_parses_response_and_token_counts(monkeypatch):
    seen = {}

    def fake_post(url, **kw):
        seen.update(url=url, json=kw["json"])
        return _resp(200, {"response": '{"a": 1}', "prompt_eval_count": 42, "eval_count": 7})

    monkeypatch.setattr(providers.httpx, "post", fake_post)
    r = OllamaProvider().complete("hello", "sys")
    assert (r.text, r.tokens_in, r.tokens_out) == ('{"a": 1}', 42, 7)
    assert seen["url"].endswith("/api/generate") and seen["json"]["stream"] is False and seen["json"]["system"] == "sys"


def test_ollama_http_error_is_provider_error(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(providers.httpx, "post", boom)
    with pytest.raises(ProviderError, match="ollama"):
        OllamaProvider().complete("x")


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setattr(providers, "get_settings",
                        lambda: SimpleNamespace(gemini_api_key="k", gemini_model="m", ollama_url="http://o", ollama_model="x"))
    return GeminiProvider()


def test_gemini_parses_candidate_and_usage(gemini, monkeypatch):
    seen = {}

    def fake_post(url, **kw):
        seen.update(url=url, params=kw["params"], json=kw["json"])
        return _resp(200, {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}],
                           "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 3}})

    monkeypatch.setattr(providers.httpx, "post", fake_post)
    r = gemini.complete("hi", "sys")
    assert (r.text, r.tokens_in, r.tokens_out) == ('{"ok": true}', 11, 3)
    assert seen["params"] == {"key": "k"} and "models/m:generateContent" in seen["url"]
    assert seen["json"]["generationConfig"]["responseMimeType"] == "application/json"


@pytest.mark.parametrize("status,retryable", [(429, True), (503, True), (400, False), (403, False)])
def test_gemini_error_status_retryability(gemini, monkeypatch, status, retryable):
    monkeypatch.setattr(providers.httpx, "post", lambda url, **kw: _resp(status, {"error": "x"}))
    with pytest.raises(ProviderError) as e:
        gemini.complete("x")
    assert e.value.retryable is retryable


def test_gemini_empty_candidates_not_retryable(gemini, monkeypatch):
    monkeypatch.setattr(providers.httpx, "post", lambda url, **kw: _resp(200, {"candidates": []}))
    with pytest.raises(ProviderError) as e:
        gemini.complete("x")
    assert e.value.retryable is False


def test_gemini_requires_key(monkeypatch):
    monkeypatch.setattr(providers, "get_settings",
                        lambda: SimpleNamespace(gemini_api_key="", gemini_model="m"))
    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        GeminiProvider()


def test_get_provider_unknown_name():
    with pytest.raises(ValueError):
        providers.get_provider("nope")


# ---- file selection / ignore lists ----
def test_is_ignored_dirs_files_and_suffixes():
    ign = summarize.is_ignored
    assert ign("node_modules/x/index.js") and ign("src/node_modules/y.js")
    assert ign("package-lock.json") and ign("app.min.js") and ign("logo.png") and ign("data.csv")
    assert not ign("src/main.py") and not ign("README.md")


def test_rank_files_orders_readme_entry_source_tests_and_drops_ignored():
    files = {
        "node_modules/a.js": "x" * 10, "tests/test_a.py": "x" * 50, "util.py": "x" * 500, "main.py": "x" * 20,
        "README.md": "# hi", "notes.txt": "x", "package-lock.json": "{}",
    }
    ranked = summarize.rank_files(files)
    assert ranked == ["README.md", "main.py", "util.py", "tests/test_a.py", "notes.txt"]


def test_chunk_is_capped():
    big = "a" * (summarize.CHUNK_CHARS * 10)
    chunks = summarize.chunk(big)
    assert len(chunks) == summarize.MAX_CHUNKS and all(len(c) <= summarize.CHUNK_CHARS for c in chunks)
    assert summarize.chunk("") == [""]


def test_select_files_respects_prompt_budget_and_ignore():
    from app.pipeline import evaluate
    files = {f"src/m{i}.py": "x" * 5000 for i in range(10)}
    files["node_modules/z.js"] = "ignored"
    files["README.md"] = "# readme"
    chosen = select_files("code_quality", files, summarize.rank_files(files))
    assert "node_modules/z.js" not in chosen
    assert all(len(v) <= evaluate.PER_FILE_CHARS for v in chosen.values())
    assert sum(len(v) for v in chosen.values()) <= evaluate.PROMPT_FILE_CHARS


def test_select_files_testing_prefers_test_files():
    files = {"src/a.py": "def a(): pass", "tests/test_a.py": "def test_a(): pass", "README.md": "# r"}
    chosen = select_files("testing", files, summarize.rank_files(files))
    assert "tests/test_a.py" in chosen and "README.md" in chosen


# ---- local storage key safety ----
def test_storage_rejects_traversal_keys(tmp_path):
    st = LocalStorage(str(tmp_path / "root"))
    (tmp_path / "secret.txt").write_text("nope")
    for bad in ("../secret.txt", "..\\secret.txt", str(tmp_path / "secret.txt"), "a/../../secret.txt"):
        with pytest.raises(ValueError):
            st.get(bad)


def test_storage_put_sanitises_suffix_and_roundtrips(tmp_path):
    st = LocalStorage(str(tmp_path / "root"))
    key = st.put(b"data", "../../evil.py")
    assert "/" not in key and "\\" not in key
    assert (tmp_path / "root" / key).exists() and st.get(key) == b"data"
    assert not (tmp_path / "evil.py").exists()
