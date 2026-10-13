"""Deterministic facts: no LLM involved, so they are cheap, reproducible and checkable."""
import os
import re

from app.security import injection

SOURCE_EXT = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript",
    ".java": "Java", ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP", ".c": "C", ".cpp": "C++",
    ".cs": "C#", ".kt": "Kotlin", ".swift": "Swift", ".sql": "SQL", ".sh": "Shell",
}
_TEST_RX = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|[^/]*_test\.\w+$|[^/]*\.(test|spec)\.\w+$", re.I)


def is_test_file(path: str) -> bool:
    return bool(_TEST_RX.search(path))


def is_readme(path: str) -> bool:
    return os.path.basename(path).lower().startswith("readme")


def compute_facts(files: dict[str, str]) -> dict:
    langs: dict[str, int] = {}
    src = 0
    loc = 0
    for p, c in files.items():
        ext = os.path.splitext(p)[1].lower()
        if ext in SOURCE_EXT and not is_test_file(p):
            src += 1
            langs[SOURCE_EXT[ext]] = langs.get(SOURCE_EXT[ext], 0) + 1
            loc += c.count("\n") + 1
    readmes = [p for p in files if is_readme(p)]
    readme_text = files[readmes[0]] if readmes else ""
    lower = {p.lower() for p in files}
    return {
        "total_files": len(files),
        "source_files": src,
        "languages": dict(sorted(langs.items())),
        "approx_loc": loc,
        "has_readme": bool(readmes),
        "readme_length": len(readme_text),
        "readme_sections": len(re.findall(r"^#{1,3}\s+\S", readme_text, re.M)),
        "test_files": sum(1 for p in files if is_test_file(p)),
        "has_ci": any(p.startswith(".github/workflows/") for p in lower),
        "has_dockerfile": any(os.path.basename(p) == "dockerfile" for p in lower),
        "has_license": any(os.path.basename(p).startswith("license") for p in lower),
    }


def scan_injection(files: dict[str, str]) -> dict[str, list[str]]:
    """{path: [pattern names]} for every file whose text tries to steer the grader."""
    out = {}
    for p, c in files.items():
        hits = injection.detect(c)
        if hits:
            out[p] = hits
    return out
