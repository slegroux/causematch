import logging

from fasthtml.common import (
    H1,
    A,
    Button,
    Div,
    P,
    Redirect,
    Script,
    Span,
    to_xml,
)
from fasthtml.common import Form as PlainForm
from monsterui.all import TextPresets
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app import data, notify, security
from app.auth import ws_session_user
from app.forms import bad_request, query, str_form
from app.ui import (
    SEND,
    SMALL_LINK,
    AddToCalendar,
    BackLink,
    Icon,
    Notes,
    OrgFace,
    OrgShell,
    Refusal,
    Router,
    TextInput,
    VolunteerHeader,
    VolunteerShell,
)

ar = Router()
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
    # theirs on the left in white, mine on the right in the brand's soft tint
    return Div(
        P(
            "You" if mine else m["sender"],
            cls="text-xs font-bold " + ("text-brand-ink" if mine else "text-ink-muted"),
        ),
        P(m["body"], cls="whitespace-pre-wrap break-words text-ink"),
        cls="max-w-[85%] rounded-2xl px-4 py-2.5 shadow-sm "
        + (
            "self-end rounded-br-md bg-brand-soft"
            if mine
            else "self-start rounded-bl-md border border-line bg-white"
        ),
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
    rbase = "/matches" if role == "volunteer" else "/org/matches"
    back = ("/matches", "Matches") if role == "volunteer" else ("/org/inbox", "Inbox")
    box = Div(
        Div(
            *[Bubble(m, uid) for m in msgs],
            id="messages",
            cls="flex max-h-[55vh] flex-col gap-2 overflow-y-auto py-3",
        ),
        ChatError(NOTICES.get(notice, "")),
        # cm-composer (static/theme.css): sticks to the bottom, above the tab bar
        PlainForm(
            TextInput(
                name="msg",
                maxlength=data.MAX_MESSAGE,
                required=True,
                autocomplete="off",
                aria_label="Message",
                placeholder="Write a message",
                cls="flex-1",
            ),
            Button(
                Icon(SEND, size=20),
                Span("Send", cls="sr-only"),
                type="submit",
                cls="grid h-11 w-11 shrink-0 place-items-center rounded-full bg-brand "
                "text-white shadow-warm hover:bg-brand-strong focus-visible:outline "
                "focus-visible:outline-4 focus-visible:outline-offset-2 "
                "focus-visible:outline-brand/40",
            ),
            action=f"{base}/{match_id}/send",
            method="post",
            ws_send=True,
            cls="cm-composer z-20 flex items-center gap-2 bg-cream py-2",
        )
        if head["writable"]
        else P(
            "This match was withdrawn, so the chat is read-only.",
            cls="rounded-2xl bg-sand px-4 py-3 text-sm text-ink-muted",
        ),
        hx_ext="ws" if head["writable"] else None,
        ws_connect=f"{base}/{match_id}/ws" if head["writable"] else None,
    )
    content = (
        BackLink(back[0], f"Back to {back[1].lower()}"),
        Div(
            Div(
                None
                if role == "coordinator"
                else OrgFace(head["org"], head["logo"], head["photo"], head["slug"]),
                H1(
                    head["title"],
                    cls="font-display text-xl font-extrabold leading-snug text-ink",
                ),
                cls="flex items-center gap-3",
            ),
            VolunteerHeader(head["other"], head["photo_key"])
            if role == "coordinator"
            else None,
            P(
                head["org"]
                if role == "coordinator"
                else f"{head['org']} - {head['other']}",
                cls=TextPresets.muted_sm,
            ),
            Notes(head["notes"]) if role == "volunteer" else None,
            AddToCalendar(head["shift"])
            if role == "volunteer" and head["shift"]
            else None,
            cls="space-y-1 border-b border-line pb-3",
        ),
        P(
            "You're in! Your shift is booked. Say hi to the team here.",
            role="status",
            cls="mt-3 rounded-2xl bg-cause-parks-soft px-4 py-3 font-bold "
            "text-cause-parks-ink",
        )
        if notice == "in" and role == "volunteer" and head["writable"]
        else None,
        box,
        Div(
            A("Report", href=f"{rbase}/{match_id}/report", cls=SMALL_LINK),
            A("Block", href=f"{rbase}/{match_id}/block", cls=SMALL_LINK),
            cls="flex gap-x-5 pt-2 text-sm",
        ),
        Script(src="/static/chat.js", defer=True),
    )
    if role == "coordinator":  # the nonprofit's own top nav, from the inbox
        return OrgShell("inbox", data.org_waiting(uid), Div(*content, cls="max-w-md"))
    return VolunteerShell("matches", *content)


def _chat_page(req, match_id, role, notice):
    uid = req.scope["uid"]
    try:
        if data.chat_access(uid, match_id) != role:
            raise PermissionError("wrong side")
        return ChatPage(uid, match_id, role, notice)
    except PermissionError:
        return Refusal(403)


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


async def _refuse(ws, code=1008):
    for socks in _sockets.values():
        socks.pop(ws, None)
    if (
        ws.application_state != WebSocketState.DISCONNECTED
    ):  # a message can follow a close
        try:
            await ws.close(code=code)
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


def volunteer_online(match_id, vol_uid):
    """True while the volunteer has the chat open on this match (a live socket)."""
    return any(
        uid == vol_uid and role == "volunteer"
        for uid, role in list(_sockets.get(match_id, {}).values())
    )


async def _deliver(uid, match_id, text):
    """Save, then broadcast, then tell notify.on_new_message. ValueError for a bad message,
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
            if not security.take_token("chat", uid):
                # 1013, try again later: the ws extension reconnects after a pause
                return await _refuse(ws, code=1013)
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
        return Refusal(403)
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
