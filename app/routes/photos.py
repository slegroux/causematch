"""GET /photos/{key}.jpg and its 160 px square: re-encoded JPEGs, for whoever may see them.

Every role reaches these paths (app/auth.py); data.photo_for decides. A photo someone may
not see answers 404, like one that doesn't exist.
"""

from fasthtml.common import Response

from app import data
from app.ui import Router

ar = Router()


def _serve(req, key, thumb):
    found = data.photo_for(req.scope["uid"], key, thumb)
    if found is None:
        return Response("Not found", status_code=404)
    jpeg, etag = found
    # Private: one viewer's browser may keep it a day; no shared cache ever may.
    headers = {"ETag": etag, "Cache-Control": "private, max-age=86400"}
    if req.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(jpeg, media_type="image/jpeg", headers=headers)


@ar("/photos/{key}.jpg", methods=["GET"])
def photo(req, key: str):
    return _serve(req, key, False)


@ar("/photos/thumb/{key}.jpg", methods=["GET"])
def thumb(req, key: str):
    return _serve(req, key, True)
