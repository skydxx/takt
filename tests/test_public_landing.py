import asyncio

import pytest


@pytest.fixture()
def public_app(tmp_path, monkeypatch):
    import web_app
    from database import Database

    test_db = Database(str(tmp_path / "public.sqlite3"))
    monkeypatch.setattr(web_app, "db", test_db)
    web_app.app.config.update(TESTING=True)
    return web_app.app, test_db


def test_landing_defaults_to_russian_and_sets_csrf(public_app):
    app, _ = public_app
    client = app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    assert 'lang="ru"' in response.text
    assert "Такт" in response.text
    assert client.get_cookie("public_csrf_token") is not None
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_language_switch_renders_english(public_app):
    app, _ = public_app
    client = app.test_client()

    switch = client.get("/lang/en", follow_redirects=True)

    assert switch.status_code == 200
    assert 'lang="en"' in switch.text
    assert "Takt" in switch.text
    assert "ПВЗ · Ленина 14" not in switch.text
    assert "Cabinet data collection" not in switch.text
    assert "Automatic import" in switch.text
    assert "Done-for-you setup" in switch.text


def test_connection_request_requires_csrf_and_stores_lead(public_app):
    app, database = public_app
    client = app.test_client()
    client.get("/")
    csrf = client.get_cookie("public_csrf_token").value

    invalid = client.post(
        "/request-access",
        data={"name": "Owner", "contact": "@owner", "points": "1", "consent": "1"},
    )
    assert invalid.status_code == 400

    response = client.post(
        "/request-access",
        data={
            "csrf_token": csrf,
            "lang": "en",
            "name": "Owner",
            "contact": "@owner",
            "points": "2-4",
            "message": "Need a clean handover flow",
            "consent": "1",
        },
    )

    assert response.status_code == 200
    assert "Request received" in response.text
    leads = asyncio.run(database.get_lead_requests())
    assert len(leads) == 1
    assert leads[0]["contact"] == "@owner"
    assert leads[0]["language"] == "en"


def test_legacy_vpn_surface_is_disabled_by_default(public_app):
    app, _ = public_app
    response = app.test_client().get("/vpn")

    assert response.status_code == 404


def test_pc_concept_demo_renders_interactive_markup_starting_hidden(public_app):
    app, _ = public_app
    response = app.test_client().get("/")
    html = response.text

    # The employee-code input drives the demo and must accept 4/6/8 digits.
    assert 'id="pcconcept-code"' in html
    assert 'maxlength="8"' in html
    assert 'inputmode="numeric"' in html
    assert 'aria-describedby="pcconcept-status"' in html
    assert 'data-hint="' in html
    assert 'data-invalid-text="' in html
    assert 'data-valid-text="' in html

    # The success panel must start hidden server-side — it only reveals via
    # client-side validation, never a real check against a backend.
    assert '<div class="pcconcept-success" id="pcconcept-success" hidden>' in html
    assert 'id="pcconcept-open-app"' in html
    assert 'data-clicked-text="' in html

    # No claim of real auth or a real shift ever being opened.
    assert "демо" in html.lower()
    assert "не проверяется" in html or "не открывает настоящую смену" in html


def test_pc_concept_demo_copy_is_translated_in_english(public_app):
    app, _ = public_app
    client = app.test_client()
    response = client.get("/lang/en", follow_redirects=True)
    html = response.text

    assert "Employee code" in html
    assert "demo" in html.lower()
    assert "No code here is verified" in html or "not a working workspace feature" in html
