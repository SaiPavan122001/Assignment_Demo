"""Prompt-injection handling: student text is data, never instructions.

1. `delimit()` wraps every piece of student content in explicit data markers and strips marker lookalikes.
2. `detect()` flags text that tries to steer the grader; a flag forces human review (it does NOT
   try to "clean" the text - the evidence stays visible to the reviewer).
This is a heuristic filter, not a guarantee; see README limitations.
"""
import re

_PATTERNS = {
    "ignore_instructions": r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(instruction|rubric|rule|prompt|criteria|guideline)s?\b",
    "ignore_above": r"\b(ignore|disregard)\b.{0,20}\b(above|previous|prior|earlier)\b",
    "give_full_marks": r"\b(give|award|assign|grant|output|return)\b.{0,40}\b(full|maximum|max|perfect|top|highest|100|10\s*/\s*10)\b.{0,20}\b(mark|score|point|grade|rating)s?\b",
    "score_demand": r"\b(score|grade|rating)\s*(of|=|:)\s*(10|100)\b",
    "role_hijack": r"\b(you are now|act as|pretend to be|new instructions?|system prompt)\b",
    "evaluator_address": r"\b(dear|note to|attention)\s+(ai\s+)?(evaluator|grader|reviewer|llm|model)\b",
    "chat_markers": r"(<\|im_start\|>|<\|im_end\|>|\[/?INST\]|###\s*(system|instruction))",
}
_COMPILED = {k: re.compile(v, re.IGNORECASE | re.DOTALL) for k, v in _PATTERNS.items()}

OPEN = "<<<STUDENT_DATA"
CLOSE = "STUDENT_DATA>>>"


def detect(text: str) -> list[str]:
    return [name for name, rx in _COMPILED.items() if rx.search(text)]


def delimit(text: str, source: str) -> str:
    safe = text.replace(OPEN, "[marker-removed]").replace(CLOSE, "[marker-removed]")
    return f"{OPEN} source={source}\n{safe}\n{CLOSE}"
