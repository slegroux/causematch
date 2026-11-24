"""The preview's password gate in a real browser: an emailed sign-in link opened on a
device that never had the preview's link survives the password form. Served over https
(a preview is production: Secure cookies, an https origin), with a self-signed cert."""

import datetime
import ipaddress
import os
import socket
import threading
import time

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app import data
from e2e.helpers import guard
from main import app

PASSWORD = "p" * 20  # as tests/test_preview.py: low entropy on purpose (secret scan)
SECRET = "s" * 32


def _self_signed(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_file, cert_file = tmp_path / "key.pem", tmp_path / "cert.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_file, cert_file


@pytest.fixture
def preview_url(tmp_path, monkeypatch):
    """The app in a preview, on https://127.0.0.1:<free port>, in this process."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"https://127.0.0.1:{port}"
    for k, v in {
        "CAUSEMATCH_ENV": "preview",
        "PREVIEW_PASSWORD": PASSWORD,
        "PHONE_HASH_KEY": SECRET,
        "CERTIFICATE_KEY": SECRET,
        "TASKS_TOKEN": SECRET,
        "SESSION_SECRET": SECRET,
        "CONTACT_EMAIL": "hello@causematch.test",
        "PUBLIC_BASE_URL": url,
    }.items():
        monkeypatch.setenv(k, v)
    key_file, cert_file = _self_signed(tmp_path)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            ssl_keyfile=str(key_file),
            ssl_certfile=str(cert_file),
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    yield url
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert os.environ["CAUSEMATCH_ENV"] == "preview"  # monkeypatch restores it after


def test_an_emailed_link_survives_the_gate_in_a_browser(browser, preview_url):
    """No access cookie: the link shows the gate's page; the password typed there lands on
    the sign-in confirm page the link pointed to, its token unused."""
    token = data.create_email_token("river@example.org")
    ctx = guard(browser.new_context(ignore_https_errors=True, base_url=preview_url))
    try:
        page = ctx.new_page()
        r = page.goto(f"/login/verify?token={token}")
        assert r.status == 401 and "private preview" in page.content()
        page.get_by_label("Preview password").fill(PASSWORD)
        with page.expect_navigation():
            page.get_by_role("button", name="Open").click()
        assert page.url == f"{preview_url}/login/verify"  # the app took the token in
        page.get_by_text("river@example.org").first.wait_for()
        assert data.peek_login_token(token)[1] == "river@example.org"  # still unused
    finally:
        ctx.close()  # its open connections would hold the server's shutdown
