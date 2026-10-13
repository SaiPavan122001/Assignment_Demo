"""Evidence hallucination guard: a quote must literally exist in the cited extracted file."""


def validate_evidence(evidence: list[dict], files: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Returns (accepted, rejected). rejected items carry a `why`."""
    accepted, rejected = [], []
    for ev in evidence:
        path, quote = ev.get("file", ""), (ev.get("quote") or "").strip()
        if path not in files:
            rejected.append({**ev, "why": "cited file was not part of the extracted project"})
        elif not quote:
            rejected.append({**ev, "why": "empty quote"})
        elif quote not in files[path]:
            rejected.append({**ev, "why": "quote not found verbatim in cited file"})
        else:
            accepted.append({"file": path, "quote": quote})
    return accepted, rejected
