"""The public website (site/, published by .github/workflows/pages.yml)."""

import re
import subprocess

from conftest import ROOT


def build(tmp_path):
    out = tmp_path / "_site"
    subprocess.run([str(ROOT / "scripts/build_site.sh"), str(out)], check=True)
    return out, (out / "index.html").read_text()


def test_every_file_the_page_loads_is_in_the_built_site(tmp_path):
    out, html = build(tmp_path)
    refs = re.findall(r'(?:src|srcset|href)="([^"#][^"]*)"', html)
    local = [r for r in refs if not r.startswith(("https://", "http://", "mailto:"))]
    assert local and all((out / r).is_file() for r in local), local
    # it loads nothing from elsewhere: links out are fine, resources are not
    loads = re.findall(
        r'<(?:img|script|source|video)[^>]*(?:src|srcset)="(https?://[^"]+)"', html
    )
    assert loads == []


def test_the_page_never_points_at_the_private_preview(tmp_path):
    """The preview's link carries its password; nothing public may lead there."""
    _, html = build(tmp_path)
    assert "run.app" not in html and "access=" not in html
