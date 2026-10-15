import logging

from fasthtml.common import (
    H1,
    A,
    APIRouter,
    Div,
    Input,
    P,
    Redirect,
    Response,
    Script,
    to_xml,
)
from fasthtml.common import Form as PlainForm
from monsterui.all import AT, Button, ButtonT, Container, TextPresets
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app import data, notify
from app.auth import ws_session_user
from app.forms import bad_request, query, str_form
from app.ui import VolunteerNav

ar = APIRouter()
log = logging.getLogger("causematch")

NOTICES = {
    "empty": "Write a message first.",
    "long": f"Messages can be up to {data.MAX_MESSAGE} characters.",
}
# match id -> {socket: (user id, role)}: every open socket, for the live broadcast
_sockets = {}
MAX_SOCKETS_PER_MATCH = 5  # one user's sockets on one match: a few tabs and devices
MAX_SOCKETS = 20  # one user's sockets overall


def Bubble(m, viewer_uid):
    mine = m["sender_uid"] == viewer_uid
    return Div(
        P("You" if mine else m["sender"], cls=TextPresets.muted_sm),
        P(m["body"], cls="whitespace-pre-wrap break-words"),
        cls="rounded-lg p-3 max-w-[85%] "
        + ("self-end bg-secondary" if mine else "self-start border"),
    )


def ChatError(text="", oob=False):
    return Div(
        text,
        id="chat-error",
        role="alert",
        cls="text-sm text-red-600",
        hx_swap_oob="true" if oob else None,
    )


def ChatPage(uid, match_id, role, notice=""):
    """History, and a form that sends over the socket (or posts, without JavaScript)."""
    head = data.chat_header(uid, match_id)
    msgs = data.list_messages(uid, match_id)
    base = "/chat" if role == "volunteer" else "/org/chat"
    back = ("/matches", "Matches") if role == "volunteer" else ("/org/inbox", "Inbox")
    box = Div(
        Div(
            *[Bubble(m, uid) for m in msgs],
            id="messages",
            cls="flex flex-col gap-2 overflow-y-auto max-h-[60vh] py-2",
        ),
        ChatError(NOTICES.get(notice, "")),
        PlainForm(
            Input(
                name="msg",
                maxlength=data.MAX_MESSAGE,
                required=True,
                autocomplete="off",
                aria_label="Message",
                cls="uk-input",
            ),
            Button("Send", cls=ButtonT.primary),
            action=f"{base}/{match_id}/send",
            method="post",
            ws_send=True,
            cls="flex gap-2 mt-2",
        )
        if head["writable"]
        else P("This match was withdrawn, so the chat is read-only."),
        hx_ext="ws" if head["writable"] else None,
        ws_connect=f"{base}/{match_id}/ws" if head["writable"] else None,
    )
    return Container(
        VolunteerNav() if role == "volunteer" else None,
        A(f"Back to {back[1].lower()}", href=back[0], cls=AT.muted + " text-sm"),
        H1(head["title"], cls="text-2xl font-bold my-2"),
        P(f"{head['org']} - {head['other']}", cls=TextPresets.muted_sm + " mb-2"),
        box,
        Script(src="/static/chat.js", defer=True),
        cls="max-w-md pb-8",
    )


def _chat_page(req, match_id, role, notice):
    uid = req.scope["uid"]
    try:
        if data.chat_access(uid, match_id) != role:
            raise PermissionError("wrong side")
        return ChatPage(uid, match_id, role, notice)
    except PermissionError:
        return Response("Forbidden", status_code=403)


def _match_id(ws):
    try:
        return int(ws.path_params.get("match_id", ""))
    except ValueError:
        return 0  # not an id: chat_access refuses it


def _authorized(ws, role, match_id):
    """The caller's user id when this socket may write in this chat now, else PermissionError.

    Sync, so it runs in the threadpool: it takes the data lock, which a request thread may hold.
    """
    user = ws_session_user(ws, role)
    if user is None or data.chat_access(user.id, match_id, write=True) != role:
        raise PermissionError("no chat for you")
    return user.id


def _too_many(uid, match_id):
    mine = [
        (m, ws)
        for m, socks in _sockets.items()
        for ws, (u, _) in socks.items()
        if u == uid
    ]
    return len(mine) >= MAX_SOCKETS or (
        sum(1 for m, _ in mine if m == match_id) >= MAX_SOCKETS_PER_MATCH
    )


async def _refuse(ws):
    for socks in _sockets.values():
        socks.pop(ws, None)
    if (
        ws.application_state != WebSocketState.DISCONNECTED
    ):  # a message can follow a close
        try:
            await ws.close(code=1008)
        except (RuntimeError, OSError, WebSocketDisconnect):
            pass  # already gone: nothing to tell it


async def _broadcast(match_id, m):
    """Send a saved message to every open socket on the match whose owner may still read it."""
    for ws, (uid, role) in list(_sockets.get(match_id, {}).items()):
        try:
            if await run_in_threadpool(_authorized, ws, role, match_id) != uid:
                raise PermissionError("session ended")
            await ws.send_text(
                to_xml(Div(Bubble(m, uid), id="messages", hx_swap_oob="beforeend"))
            )
        except PermissionError:
            await _refuse(ws)
        except (RuntimeError, OSError, WebSocketDisconnect):
            _sockets.get(match_id, {}).pop(ws, None)  # dead: must not stop the others
    if not _sockets.get(match_id):
        _sockets.pop(match_id, None)


async def _deliver(uid, match_id, text):
    """Save, then broadcast, then tell item 7's hook. ValueError for a bad message,
    PermissionError for a bad caller. Every data call runs off the event loop."""
    m = await run_in_threadpool(data.send_message, uid, match_id, text)
    await _broadcast(match_id, m)
    try:
        await run_in_threadpool(notify.on_new_message, match_id, uid)
    except Exception as e:  # noqa: BLE001  the message is saved and delivered either way
        log.warning("new-message hook failed: %s", type(e).__name__)


def _notice_for(text):
    return "empty" if not (isinstance(text, str) and text.strip()) else "long"


def _live(role, base):
    """The three websocket handlers of one side; each checks the caller itself, as Beforeware
    does not run for websockets."""

    async def conn(ws):
        match_id = _match_id(ws)
        try:
            uid = await run_in_threadpool(_authorized, ws, role, match_id)
            if _too_many(uid, match_id):
                raise PermissionError("too many sockets")
        except PermissionError:
            return await _refuse(ws)
        _sockets.setdefault(match_id, {})[ws] = (uid, role)

    # `data` is FastHTML's name for the parsed frame, so it hides the data module here;
    # the frame is read by key because a cookie named `msg` would win over a parameter.
    async def recv(ws, data):
        if ws.application_state != WebSocketState.CONNECTED:
            return  # conn refused this socket; a frame sent before the close arrived is dropped
        match_id = _match_id(ws)
        msg = data.get("msg") if isinstance(data, dict) else None
        try:
            uid = await run_in_threadpool(_authorized, ws, role, match_id)
            await _deliver(uid, match_id, msg)
        except PermissionError:
            return await _refuse(ws)
        except ValueError:
            reply = ChatError(NOTICES[_notice_for(msg)], oob=True)
        else:
            reply = ChatError(oob=True)
        try:
            await ws.send_text(to_xml(reply))
        except (RuntimeError, OSError, WebSocketDisconnect):
            pass  # closed since (a revoked session): nothing to tell it

    async def disconn(ws):
        match_id = _match_id(ws)
        _sockets.get(match_id, {}).pop(ws, None)
        if match_id in _sockets and not _sockets[match_id]:
            del _sockets[match_id]

    ar.ws(f"{base}/{{match_id}}/ws", conn=conn, disconn=disconn)(recv)


async def _post(req, match_id, role, base):
    form = await str_form(req)
    if form is None:
        return bad_request()
    msg = form.get("msg", "")
    uid = req.scope["uid"]
    try:
        if await run_in_threadpool(data.chat_access, uid, match_id) != role:
            raise PermissionError("wrong side")
        await _deliver(uid, match_id, msg)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    except ValueError:
        return Redirect(f"{base}/{match_id}?notice={_notice_for(msg)}")
    return Redirect(f"{base}/{match_id}")


_live("volunteer", "/chat")
_live("coordinator", "/org/chat")


@ar("/chat/{match_id}", methods=["GET"])
def chat(req, match_id: int):
    return _chat_page(req, match_id, "volunteer", query(req, "notice"))


@ar("/chat/{match_id}/send", methods=["POST"])
async def chat_send(req, match_id: int):
    return await _post(req, match_id, "volunteer", "/chat")


@ar("/org/chat/{match_id}", methods=["GET"])
def org_chat(req, match_id: int):
    return _chat_page(req, match_id, "coordinator", query(req, "notice"))


@ar("/org/chat/{match_id}/send", methods=["POST"])
async def org_chat_send(req, match_id: int):
    return await _post(req, match_id, "coordinator", "/org/chat")
