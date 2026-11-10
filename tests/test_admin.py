"""ADMIN_EMAILS: the first admins, made at startup where there is no shell for
scripts/add_admin.py (production and the preview)."""

import logging

import pytest
from conftest import coordinator_for

from app import data
from app.config import admin_emails

PROD = {"CAUSEMATCH_ENV": "production"}


def test_bootstrap_reads_addresses_in_production_and_preview_only():
    raw = " Katie@Example.org, ops@example.org ,katie@example.org,, "
    for env in (PROD, {"CAUSEMATCH_ENV": "preview"}, {"K_SERVICE": "causematch"}):
        assert admin_emails({**env, "ADMIN_EMAILS": raw}) == [
            "katie@example.org",
            "ops@example.org",
        ]
    assert admin_emails(PROD) == admin_emails({**PROD, "ADMIN_EMAILS": " , "}) == []
    for env in ({"CAUSEMATCH_ENV": "development"}, {"CAUSEMATCH_ENV": "test"}):
        with pytest.raises(RuntimeError, match="add_admin.py"):
            admin_emails({**env, "ADMIN_EMAILS": "katie@example.org"})
    for bad in ("Katie <katie@example.org>", "katie@example", "kätie@example.org"):
        with pytest.raises(RuntimeError, match="bare addresses"):
            admin_emails({**PROD, "ADMIN_EMAILS": f"ops@example.org,{bad}"})


def test_bootstrap_adds_missing_admins_once_and_never_removes_one(caplog):
    _, coord_email = coordinator_for(0)
    data.add_admin("old@example.org", "Old")
    caplog.set_level(logging.INFO, logger="causematch")

    emails = ["katie@example.org", coord_email]
    assert data.add_admins(emails) == (1, 1)
    katie = data.admin_by_email("katie@example.org")
    assert katie and not data.admin_by_email(coord_email)
    assert data.admin_by_email("old@example.org")  # not listed, still an admin
    assert "example.org" not in caplog.text and "1 admin(s) added" in caplog.text

    assert data.add_admins(emails) == (0, 1)  # a restart: nothing new
    assert data.admin_by_email("katie@example.org") == katie

    data.remove_admin("katie@example.org")
    assert data.add_admins(emails) == (0, 1)  # removed stays removed
    assert not data.admin_by_email("katie@example.org")
