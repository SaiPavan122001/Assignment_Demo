"""LLM providers: mock (default, offline, deterministic), ollama (local), gemini (free API key).

Only `mock` is exercised by the test-suite. ollama/gemini are straightforward HTTP clients that have
not been run unless the README says so.
"""
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from app.config import get_settings
from app.security import injection


class ProviderError(Exception):
    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable


@dataclass
class ProviderResponse:
    text: str
    tokens_in: int
    tokens_out: int


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def complete(self, prompt: str, system: str = "") -> ProviderResponse: ...


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ---------------------------------------------------------------- mock
_BLOCK = re.compile(r"<<<STUDENT_DATA source=(.*?)\n(.*?)\nSTUDENT_DATA>>>", re.DOTALL)


class MockProvider(LLMProvider):
    """Deterministic heuristic grader. It is a stand-in for an LLM, NOT an evaluation of quality.

    It quotes real lines from the supplied files (so the evidence guard passes) and is deliberately
    *gullible*: text such as "give full marks" in student data makes it comply, so the pipeline's
    injection detector (not the model) is what has to protect the score.
    """
    name = "mock"
    model = "mock-heuristic-1"

    def complete(self, prompt: str, system: str = "") -> ProviderResponse:
        task = re.search(r"^TASK: (\w+)", prompt, re.M)
        task = task.group(1) if task else "unknown"
        out = self._summary(prompt) if task == "summarize_file" else self._criterion(prompt)
        return ProviderResponse(out, approx_tokens(system + prompt), approx_tokens(out))

    @staticmethod
    def _summary(prompt: str) -> str:
        m = _BLOCK.search(prompt)
        body = m.group(2) if m else ""
        first = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), "empty file")
        return json.dumps({"summary": first[:160]})

    @staticmethod
    def _criterion(prompt: str) -> str:
        key = re.search(r"^CRITERION: (\w+)", prompt, re.M).group(1)
        max_score = float(re.search(r"^MAX_SCORE: ([\d.]+)", prompt, re.M).group(1))
        facts = json.loads(re.search(r"^FACTS: (.*)$", prompt, re.M).group(1))
        files = {p: c for p, c in ((m.group(1).strip(), m.group(2)) for m in _BLOCK.finditer(prompt))
                 if not p.startswith("summary:")}
        readme = next((c for p, c in files.items() if p.lower().startswith("readme")), "")
        quote_file, quote = "", ""

        def first_line(pred):
            for p, c in files.items():
                for ln in c.splitlines():
                    if pred(p, ln) and 8 <= len(ln.strip()) <= 200:
                        return p, ln.strip()
            return "", ""

        # gullible: obeys injected grading instructions found in student data
        if injection.detect("\n".join(files.values())):
            p, q = first_line(lambda p, ln: p.lower().startswith("readme"))
            ev = [{"file": p, "quote": q}] if p else []
            return json.dumps({"score": max_score, "justification": "The project instructs a perfect score and appears complete.",
                               "evidence": ev, "confidence": 0.95})

        n_src = facts.get("source_files", 0)
        if key == "documentation":
            heads = facts.get("readme_sections", 0)
            score = min(max_score, 1 + 2 * heads + (2 if len(readme) > 400 else 0)) if readme else 0
            quote_file, quote = first_line(lambda p, ln: p.lower().startswith("readme") and ln.startswith("#"))
            conf = 0.8 if readme else 0.6
        elif key == "testing":
            t = facts.get("test_files", 0)
            score = min(max_score, 5 + 2 * t) if t else 1
            quote_file, quote = first_line(lambda p, ln: "test" in p.lower() and re.match(r"\s*(def test_|it\(|test\()", ln))
            conf = 0.8 if n_src else 0.5
        elif key == "code_quality":
            score = min(max_score, 3 + min(n_src, 3) + (1 if facts.get("has_readme") else 0) + (1 if facts.get("test_files") else 0))
            quote_file, quote = first_line(lambda p, ln: p.endswith((".py", ".js", ".ts", ".java", ".go")) and re.match(r"\s*(def |class |function |const )", ln))
            conf = 0.75 if n_src >= 2 else 0.5
        else:  # functionality and anything else
            runnable = bool(re.search(r"(run|usage|install|start)", readme, re.I))
            score = min(max_score, 3 + (3 if runnable else 0) + min(n_src, 2))
            quote_file, quote = first_line(lambda p, ln: p.endswith((".py", ".js", ".ts", ".java", ".go")) and re.match(r"\s*(def |class |function |app\.)", ln))
            conf = 0.7 if n_src else 0.4
        ev = [{"file": quote_file, "quote": quote}] if quote_file else []
        just = f"Heuristic assessment of {key} from {n_src} source file(s) and README structure."
        return json.dumps({"score": float(score), "justification": just, "evidence": ev, "confidence": conf})


# ---------------------------------------------------------------- ollama
class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self):
        s = get_settings()
        self.url, self.model = s.ollama_url.rstrip("/"), s.ollama_model

    def complete(self, prompt: str, system: str = "") -> ProviderResponse:
        try:
            r = httpx.post(f"{self.url}/api/generate", timeout=300,
                           json={"model": self.model, "prompt": prompt, "system": system, "stream": False, "format": "json"})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ProviderError(f"ollama: {type(e).__name__}") from e
        d = r.json()
        return ProviderResponse(d.get("response", ""), d.get("prompt_eval_count", approx_tokens(prompt)),
                                d.get("eval_count", approx_tokens(d.get("response", ""))))


# ---------------------------------------------------------------- gemini
class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self):
        s = get_settings()
        if not s.gemini_api_key:
            raise ProviderError("GEMINI_API_KEY is not set", retryable=False)
        self.key, self.model = s.gemini_api_key, s.gemini_model

    def complete(self, prompt: str, system: str = "") -> ProviderResponse:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
        }
        try:
            r = httpx.post(url, params={"key": self.key}, json=body, timeout=120)
        except httpx.HTTPError as e:
            raise ProviderError(f"gemini: {type(e).__name__}") from e
        if r.status_code in (429, 500, 502, 503):
            raise ProviderError(f"gemini HTTP {r.status_code}")
        if r.status_code != 200:
            raise ProviderError(f"gemini HTTP {r.status_code}", retryable=False)
        d = r.json()
        try:
            text = d["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            raise ProviderError("gemini: empty candidate", retryable=False)
        u = d.get("usageMetadata", {})
        return ProviderResponse(text, u.get("promptTokenCount", approx_tokens(prompt)),
                                u.get("candidatesTokenCount", approx_tokens(text)))


def get_provider(name: str | None = None) -> LLMProvider:
    name = (name or get_settings().llm_provider).lower()
    if name == "mock":
        return MockProvider()
    if name == "ollama":
        return OllamaProvider()
    if name == "gemini":
        return GeminiProvider()
    raise ValueError(f"unknown LLM_PROVIDER {name!r}")
