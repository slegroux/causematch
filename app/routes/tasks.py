import hmac

from fasthtml.common import APIRouter, Response

from app import data, notify
from app.config import tasks_token

ar = APIRouter()


@ar("/tasks/reminders", methods=["POST"])
def reminders(req):
    """Cloud Scheduler calls this hourly: no background loop runs on a scale-to-zero host.
    The same sweep blanks old claim requests."""
    token = tasks_token()
    sent = req.headers.get("authorization", "")
    if not token or not hmac.compare_digest(sent.encode(), f"Bearer {token}".encode()):
        return Response("Unauthorized", status_code=401)
    sent = notify.send_due_reminders()
    data.scrub_decided_claims()  # decided claim requests keep no contact details for long
    return Response(f"sent {sent}")
