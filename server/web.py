"""First-party browser client for an individual ReelDub instance."""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse


router = APIRouter()
_assets = Path(__file__).with_name("static")
_headers = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "media-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


@router.get("/", include_in_schema=False)
def library_page():
    return FileResponse(_assets / "library.html", media_type="text/html", headers=_headers)


@router.get("/ui/library.css", include_in_schema=False)
def library_styles():
    return FileResponse(_assets / "library.css", media_type="text/css", headers=_headers)


@router.get("/ui/library.js", include_in_schema=False)
def library_script():
    return FileResponse(_assets / "library.js", media_type="text/javascript", headers=_headers)
