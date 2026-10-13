"""Relevant-file selection, ignore lists, chunking and per-file summaries.

The whole project is never sent to the model in one call: at most MAX_SUMMARY_FILES files are
summarised, each in chunks of CHUNK_CHARS (first MAX_CHUNKS chunks only).
"""
import json
import os

from app.adapters.common import IGNORE_DIRS, IGNORE_FILES
from app.llm.gateway import BudgetExceeded, LLMGateway
from app.llm.prompts import SYSTEM, build_summary_prompt
from app.pipeline.facts import SOURCE_EXT, is_readme, is_test_file

MAX_SUMMARY_FILES = 12
CHUNK_CHARS = 6000
MAX_CHUNKS = 3
_SKIP_SUFFIX = (".min.js", ".map", ".svg", ".lock", ".csv", ".png", ".jpg", ".jpeg", ".gif", ".pdf")
_ENTRY = {"main", "app", "index", "server", "manage", "cli", "__main__"}


def is_ignored(path: str) -> bool:
    parts = path.split("/")
    if any(p in IGNORE_DIRS for p in parts[:-1]):
        return True
    return parts[-1] in IGNORE_FILES or path.lower().endswith(_SKIP_SUFFIX)


def rank_files(files: dict[str, str]) -> list[str]:
    """Most relevant first: README/docs, entry points, then other source by size."""
    def score(p: str) -> tuple:
        base = os.path.splitext(os.path.basename(p))[0].lower()
        ext = os.path.splitext(p)[1].lower()
        if is_readme(p):
            tier = 0
        elif p.startswith(("documentation/", "google_drive/", "pdf/", "video/", "live_url/")):
            tier = 1
        elif base in _ENTRY and ext in SOURCE_EXT:
            tier = 2
        elif ext in SOURCE_EXT and not is_test_file(p):
            tier = 3
        elif is_test_file(p):
            tier = 4
        else:
            tier = 5
        return (tier, -len(files[p]), p)

    return sorted((p for p in files if not is_ignored(p)), key=score)


def chunk(text: str) -> list[str]:
    return [text[i:i + CHUNK_CHARS] for i in range(0, min(len(text), CHUNK_CHARS * MAX_CHUNKS), CHUNK_CHARS)] or [""]


def mechanical_summary(text: str) -> str:
    first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), "empty file")
    return first[:160]


def summarize_files(files: dict[str, str], gw: LLMGateway, job_id: str, rubric_version: int) -> tuple[dict, list[str], list[str]]:
    """Returns (summaries, selected_paths, warnings)."""
    selected = rank_files(files)[:MAX_SUMMARY_FILES]
    summaries: dict[str, str] = {}
    warnings: list[str] = []
    budget_hit = False
    for p in selected:
        if budget_hit:
            summaries[p] = mechanical_summary(files[p])
            continue
        parts = []
        try:
            for i, c in enumerate(chunk(files[p])):
                r = gw.complete(prompt=build_summary_prompt(p, c), system=SYSTEM, purpose=f"summary:{p}#{i}",
                                job_id=job_id, rubric_version=rubric_version)
                try:
                    parts.append(str(json.loads(r.text).get("summary", "")).strip())
                except (ValueError, AttributeError):
                    parts.append(mechanical_summary(c))
        except BudgetExceeded:
            budget_hit = True
            warnings.append("token budget reached during summarisation; remaining files use mechanical summaries")
            parts = [mechanical_summary(files[p])]
        summaries[p] = " ".join(x for x in parts if x)[:400]
    return summaries, selected, warnings
