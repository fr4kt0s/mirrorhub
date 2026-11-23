"""Theme switching works under the strict CSP (external scripts only)."""

import re

from fastapi.testclient import TestClient


def test_theme_script_is_served_and_loaded_first(client: TestClient) -> None:
    page = client.get("/login").text
    head = page.split("</head>", 1)[0]
    scripts = re.findall(r"<script[^>]*>", head)
    assert scripts[0] == '<script src="/static/theme.js"></script>'  # not deferred: no flash
    assert "<script>" not in page  # no inline script, the CSP would block it
    assert 'name="color-scheme" content="light dark"' in head

    script = client.get("/static/theme.js")
    assert script.status_code == 200
    assert "localStorage" in script.text


def test_stylesheet_has_both_themes(client: TestClient) -> None:
    css = client.get("/static/app.css").text
    assert "prefers-color-scheme: dark" in css
    assert ':root[data-theme="dark"]' in css


def test_sidebar_marks_the_current_page(admin: TestClient) -> None:
    page = admin.get("/jobs").text
    assert '<a href="/jobs" class="active" aria-current="page">Jobs</a>' in page
    assert 'id="theme-toggle"' in page
