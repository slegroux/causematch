import hmac

from fasthtml.common import APIRouter, Response

from app import notify
from app.config import tasks_token

ar = APIRouter()


@ar("/tasks/reminders", methods=["POST"])
def reminders(req):
    """Cloud Scheduler calls this hourly: no background loop runs on a scale-to-zero host."""
    token = tasks_token()
    sent = req.headers.get("authorization", "")
    if not token or not hmac.compare_digest(sent.encode(), f"Bearer {token}".encode()):
        return Response("Unauthorized", status_code=401)
    return Response(f"sent {notify.send_due_reminders()}")
