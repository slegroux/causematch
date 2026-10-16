import hmac

from fasthtml.common import APIRouter, Response

from app import data, notify
from app.config import tasks_token

ar = APIRouter()


def _scheduler(req):
    """The request carries Cloud Scheduler's bearer token (TASKS_TOKEN)."""
    token = tasks_token()
    sent = req.headers.get("authorization", "")
    return bool(token) and hmac.compare_digest(
        sent.encode(), f"Bearer {token}".encode()
    )


@ar("/tasks/reminders", methods=["POST"])
def reminders(req):
    """Cloud Scheduler calls this hourly: no background loop runs on a scale-to-zero host.
    The same sweep blanks old claim requests."""
    if not _scheduler(req):
        return Response("Unauthorized", status_code=401)
    sent = notify.send_due_reminders()
    data.scrub_decided_claims()  # decided claim requests keep no contact details for long
    return Response(f"sent {sent}")


@ar("/tasks/urgent", methods=["POST"])
def urgent(req):
    """Cloud Scheduler calls this every 10 minutes: urgent calls' notification waves."""
    if not _scheduler(req):
        return Response("Unauthorized", status_code=401)
    return Response(f"sent {notify.send_urgent_waves()}")
