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
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app import data, notify
from app.auth import ws_session_user
from app.ui import VolunteerNav

ar = APIRouter()

NOTICES = {
    "empty": "Write a message first.",
    "long": f"Messages can be up to {data.MAX_MESSAGE} characters.",
}
# match id -> {socket: (user id, role)}: every open socket, for the live broadcast
_sockets = {}


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


async def _refuse(ws):
    for socks in _sockets.values():
        socks.pop(ws, None)
    if (
        ws.application_state != WebSocketState.DISCONNECTED
    ):  # a message can follow a close
        await ws.close(code=1008)


async def _broadcast(match_id, m):
    """Send a saved message to every open socket on the match whose owner may still read it."""
    for ws, (uid, role) in list(_sockets.get(match_id, {}).items()):
        user = ws_session_user(ws, role)
        try:
            if user is None or user.id != uid:
                raise PermissionError("session ended")
            data.chat_access(uid, match_id, write=True)
            await ws.send_text(
                to_xml(Div(Bubble(m, uid), id="messages", hx_swap_oob="beforeend"))
            )
        except PermissionError:
            await _refuse(ws)
        except (
            RuntimeError,
            OSError,
            WebSocketDisconnect,
        ):  # a dead socket must not stop the others
            _sockets.get(match_id, {}).pop(ws, None)
    if not _sockets.get(match_id):
        _sockets.pop(match_id, None)


async def _deliver(uid, match_id, text):
    """Save, then broadcast. ValueError for a bad message, PermissionError for a bad caller."""
    m = data.send_message(uid, match_id, text)
    notify.on_new_message(match_id, uid)
    await _broadcast(match_id, m)


def _live(role, base):
    """The three websocket handlers of one side; each checks the caller itself, as Beforeware
    does not run for websockets."""

    async def conn(ws):
        user = ws_session_user(ws, role)
        match_id = _match_id(ws)
        try:
            if user is None or data.chat_access(user.id, match_id, write=True) != role:
                raise PermissionError("no chat for you")
        except PermissionError:
            return await _refuse(ws)
        _sockets.setdefault(match_id, {})[ws] = (user.id, role)

    async def recv(ws, msg: str = ""):
        user = ws_session_user(ws, role)
        match_id = _match_id(ws)
        try:
            if user is None or data.chat_access(user.id, match_id, write=True) != role:
                raise PermissionError("no chat for you")
            await _deliver(user.id, match_id, msg)
        except PermissionError:
            return await _refuse(ws)
        except ValueError as e:
            return await ws.send_text(to_xml(ChatError(str(e), oob=True)))
        await ws.send_text(to_xml(ChatError(oob=True)))

    async def disconn(ws):
        match_id = _match_id(ws)
        _sockets.get(match_id, {}).pop(ws, None)
        if match_id in _sockets and not _sockets[match_id]:
            del _sockets[match_id]

    ar.ws(f"{base}/{{match_id}}/ws", conn=conn, disconn=disconn)(recv)


async def _post(req, match_id, msg, role, base):
    try:
        if data.chat_access(req.scope["uid"], match_id) != role:
            raise PermissionError("wrong side")
        await _deliver(req.scope["uid"], match_id, msg)
    except PermissionError:
        return Response("Forbidden", status_code=403)
    except ValueError:
        kind = "empty" if not (msg or "").strip() else "long"
        return Redirect(f"{base}/{match_id}?notice={kind}")
    return Redirect(f"{base}/{match_id}")


_live("volunteer", "/chat")
_live("coordinator", "/org/chat")


@ar("/chat/{match_id}", methods=["GET"])
def chat(req, match_id: int, notice: str = ""):
    return _chat_page(req, match_id, "volunteer", notice)


@ar("/chat/{match_id}/send", methods=["POST"])
async def chat_send(req, match_id: int, msg: str = ""):
    return await _post(req, match_id, msg, "volunteer", "/chat")


@ar("/org/chat/{match_id}", methods=["GET"])
def org_chat(req, match_id: int, notice: str = ""):
    return _chat_page(req, match_id, "coordinator", notice)


@ar("/org/chat/{match_id}/send", methods=["POST"])
async def org_chat_send(req, match_id: int, msg: str = ""):
    return await _post(req, match_id, msg, "coordinator", "/org/chat")
