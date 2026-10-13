"""One LLM call per rubric criterion, strict JSON validated by pydantic, retry once with the error."""
import json
import re
from decimal import Decimal

from pydantic import BaseModel, Field, ValidationError

from app.llm.gateway import BudgetExceeded, LLMGateway
from app.llm.prompts import SYSTEM, build_criterion_prompt
from app.llm.providers import ProviderError
from app.pipeline.evidence import validate_evidence
from app.pipeline.facts import is_readme, is_test_file
from app.pipeline.summarize import is_ignored

PER_FILE_CHARS = 4000
PROMPT_FILE_CHARS = 16000
CONFIDENCE_PENALTY_PER_BAD_ARTIFACT = 0.6  # confidence *= 0.6 for each failed/skipped related artifact

RELATED_ARTIFACTS = {
    "documentation": {"documentation", "pdf", "google_drive"},
    "testing": {"github", "zip"},
    "code_quality": {"github", "zip"},
    "functionality": {"github", "zip", "live_url", "video"},
}


class Evidence(BaseModel):
    file: str
    quote: str = Field(min_length=3)


class CriterionOutput(BaseModel):
    score: Decimal = Field(ge=0)
    justification: str = Field(min_length=1)
    evidence: list[Evidence] = []
    confidence: float = Field(ge=0, le=1)


def parse_output(text: str, max_score: Decimal) -> CriterionOutput:
    """Raises ValueError (incl. pydantic ValidationError) with a message fit to send back to the model."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ValueError("top-level JSON value must be an object")
    try:
        out = CriterionOutput.model_validate(data)
    except ValidationError as e:
        raise ValueError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()))
    if out.score > max_score:
        raise ValueError(f"score {out.score} exceeds max_score {max_score}")
    return out


def select_files(key: str, files: dict[str, str], ranked: list[str]) -> dict[str, str]:
    live = [p for p in ranked if not is_ignored(p)]
    if key == "documentation":
        pick = [p for p in live if is_readme(p) or p.endswith((".md", ".rst", ".docx.txt")) or "/" in p and p.split("/")[0] in
                ("documentation", "pdf", "google_drive")][:6]
    elif key == "testing":
        pick = [p for p in live if is_test_file(p)][:5] + [p for p in live if is_readme(p)][:1] + \
               [p for p in files if p.startswith(".github/workflows/")][:1]
    else:
        pick = live[:8]
    if not pick:
        pick = live[:5]
    out, used = {}, 0
    for p in dict.fromkeys(pick):
        text = files[p][:PER_FILE_CHARS]
        if used + len(text) > PROMPT_FILE_CHARS:
            break
        out[p] = text
        used += len(text)
    return out


def evaluate_criterion(criterion: dict, rubric_version: int, files: dict[str, str], ranked: list[str], facts: dict,
                       summaries: dict, artifacts: list[dict], gw: LLMGateway, job_id: str) -> dict:
    key, max_score = criterion["key"], Decimal(str(criterion["max_score"]))
    base = {"key": key, "name": criterion["name"], "weight": criterion["weight"], "max_score": criterion["max_score"],
            "score": None, "justification": "", "evidence": [], "rejected_evidence": [], "confidence": None,
            "needs_review": False, "review_reason": "", "warnings": []}

    related = RELATED_ARTIFACTS.get(key, {a["type"] for a in artifacts})
    bad = [a for a in artifacts if a["type"] in related and a["status"] in ("failed", "skipped")]
    partial = [a for a in artifacts if a["type"] in related and a["status"] == "partial"]
    base["warnings"] = [f"{a['type']} artifact {a['status']}: {a.get('reason', '')}" for a in bad + partial]

    if not files:
        return {**base, "needs_review": True, "review_reason": "no_content"}

    chosen = select_files(key, files, ranked)
    prompt_warnings = base["warnings"]
    prompt = build_criterion_prompt(criterion, rubric_version, facts, summaries, chosen, prompt_warnings)

    output: CriterionOutput | None = None
    err = ""
    for attempt in range(2):  # original + ONE retry carrying the validation error
        p = prompt if attempt == 0 else (
            prompt + f"\nPREVIOUS_RESPONSE_INVALID: {err}\nReturn a corrected JSON object only.")
        try:
            r = gw.complete(prompt=p, system=SYSTEM, purpose=f"criterion:{key}", job_id=job_id,
                            rubric_version=rubric_version)
        except BudgetExceeded:
            return {**base, "needs_review": True, "review_reason": "token_budget_exceeded"}
        except ProviderError as e:
            return {**base, "needs_review": True, "review_reason": f"llm_unavailable:{str(e)[:80]}"}
        try:
            output = parse_output(r.text, max_score)
            break
        except ValueError as e:
            err = str(e)[:300]
    if output is None:
        return {**base, "needs_review": True, "review_reason": f"invalid_output:{err}"}

    accepted, rejected = validate_evidence([e.model_dump() for e in output.evidence], files)
    confidence = output.confidence
    review_reason = ""
    if rejected:
        confidence = min(confidence, 0.3)  # cited evidence that does not exist: do not trust this answer
        review_reason = "evidence_not_found_in_files"
    elif not accepted and output.score * 2 > max_score:
        confidence = min(confidence, 0.4)
        review_reason = "high_score_without_evidence"
    confidence *= CONFIDENCE_PENALTY_PER_BAD_ARTIFACT ** len(bad)

    return {**base, "score": str(output.score), "justification": output.justification, "evidence": accepted,
            "rejected_evidence": rejected, "confidence": round(confidence, 3),
            "needs_review": bool(review_reason), "review_reason": review_reason}
