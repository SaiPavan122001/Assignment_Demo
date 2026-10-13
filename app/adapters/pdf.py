from pypdf import PdfReader

from app.adapters.base import FAILED, OK, PARTIAL, AdapterResult, register

MAX_PAGES = 100


@register("pdf")
def fetch_pdf(artifact, storage) -> AdapterResult:
    if not artifact.storage_key:
        return AdapterResult(FAILED, "no uploaded file")
    try:
        reader = PdfReader(storage.local_path(artifact.storage_key))
        if reader.is_encrypted:
            return AdapterResult(FAILED, "PDF is encrypted")
        pages = reader.pages[:MAX_PAGES]
        text = "\n\n".join((p.extract_text() or "").strip() for p in pages).strip()
    except Exception as e:  # pypdf raises many types on malformed input
        return AdapterResult(FAILED, f"unreadable PDF: {type(e).__name__}")
    name = (artifact.source_ref or "document.pdf").rsplit("/", 1)[-1] + ".txt"
    if not text:
        return AdapterResult(PARTIAL, "no extractable text (scanned images? OCR is not implemented)", {},
                             {"pages": len(reader.pages)})
    reason = f"only first {MAX_PAGES} pages read" if len(reader.pages) > MAX_PAGES else ""
    return AdapterResult(PARTIAL if reason else OK, reason, {name: text}, {"pages": len(reader.pages)})
