"""Deployed project URL: title, status code and visible text. Fetched through the SSRF guard (https only)."""
import re
from html.parser import HTMLParser

from app.adapters.base import FAILED, OK, PARTIAL, AdapterResult, register
from app.security.ssrf import SSRFBlocked, safe_get

MAX_BYTES = 1 * 1024 * 1024


class _Visible(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.parts.append(data.strip())


@register("live_url")
def fetch_live_url(artifact, storage) -> AdapterResult:
    url = (artifact.source_ref or "").strip()
    try:
        res = safe_get(url, max_bytes=MAX_BYTES, timeout=10, headers={"User-Agent": "eval-platform-poc/0.1"})
    except SSRFBlocked as e:
        return AdapterResult(FAILED, f"blocked: {e}")
    except Exception as e:  # noqa: BLE001 - network errors, timeouts
        return AdapterResult(FAILED, f"fetch error: {type(e).__name__}")
    if res.status_code >= 400:
        return AdapterResult(FAILED, f"HTTP {res.status_code}", meta={"status_code": res.status_code})
    html = res.body.decode("utf-8", errors="replace")
    p = _Visible()
    p.feed(html)
    title = re.sub(r"\s+", " ", p.title).strip()
    text = "\n".join(p.parts)
    body = f"URL: {res.url}\nStatus: {res.status_code}\nTitle: {title}\n\n{text}"
    reason = "response truncated at size/time cap" if res.truncated else ""
    return AdapterResult(PARTIAL if reason else OK, reason, {"live_url.txt": body},
                         {"status_code": res.status_code, "title": title, "final_url": res.url})
