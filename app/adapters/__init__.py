_loaded = False


def load_all() -> None:
    """Import adapter modules once so their @register decorators run."""
    global _loaded
    if _loaded:
        return
    from app.adapters import (  # noqa: F401
        documentation, github, google_drive, live_url, pdf, video, zip_adapter,
    )
    _loaded = True
