from pathlib import Path

from fasthtml.common import APIRouter
from starlette.responses import FileResponse

ar = APIRouter()
ICON = (
    Path(__file__).resolve().parent.parent.parent / "static" / "icons" / "favicon.ico"
)


@ar("/favicon.ico", methods=["GET"])
def favicon():
    return FileResponse(ICON, media_type="image/x-icon")
