import threading

from conftest import coordinator_for

from app import data
from app.db import orgs


def test_data_layer_survives_concurrent_threads():
    """The apsw connection is shared; unserialized, this raises ThreadingViolationError."""
    uid, email = coordinator_for(0)
    org_id = orgs()[0].id
    errors, replays = [], []
    barrier = threading.Barrier(16)

    def work(n):
        try:
            barrier.wait()
            # 60 rounds of 16 threads is plenty to interleave every call; the deck
            # ranks every opening, and each round adds 16, so more rounds only add time
            # that a loaded machine can push past the join below (it timed out once).
            for i in range(60):
                data.get_profile(uid)
                data.org_openings(uid)
                data.deck(uid)
                data.coordinator_by_email(email)
                token = data.create_login_token(uid)
                data.consume_login_token(token)
                if data.consume_login_token(token) is not None:
                    replays.append(token)
                data.create_opening(
                    uid, org_id, title=f"t{n}-{i}", about="a", cause="food-insecurity",
                    lat=34.0, lon=-118.2, neighborhood="n", starts_at="2030-01-01T09:00",
                    recurrence="weekly", slots=1, orientation_required="no", notes="",
                    reply_channel="email",
                )  # fmt: skip
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(16)]
    [t.start() for t in threads]
    # the join only catches a deadlock: generous, so a slow machine isn't one
    [t.join(timeout=240) for t in threads]
    assert not any(t.is_alive() for t in threads)
    assert errors == [] and replays == []
