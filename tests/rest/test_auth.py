from __future__ import annotations

import pytest


@pytest.fixture
def authed_client(pg_url, reset_db, monkeypatch):
    """Client with REST_AUTH_TOKEN set to 'secret'."""
    from data_svc.db import postgres as pg_mod
    pg_mod.close_pool()

    monkeypatch.setenv("POSTGRES_URL", pg_url)
    monkeypatch.setenv("REST_AUTH_TOKEN", "secret")
    monkeypatch.setenv("ASSETS_YAML_PATH", "/nonexistent/assets.yaml")

    from fastapi.testclient import TestClient

    from data_svc.rest.app import create_app
    from data_svc.rest.settings import RestSettings

    settings = RestSettings()
    with TestClient(create_app(settings)) as c:
        yield c


def test_open_mode_no_token_passes(client):
    """REST_AUTH_TOKEN empty in conftest.app fixture → no Authorization required."""
    r = client.get("/v1/search?q=x")
    assert r.status_code == 200


def test_auth_missing_token_401(authed_client):
    r = authed_client.get("/v1/search?q=x")
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized", "message": "missing or invalid bearer token"}


def test_auth_wrong_token_401(authed_client):
    r = authed_client.get(
        "/v1/search?q=x",
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_auth_correct_token_200(authed_client):
    r = authed_client.get(
        "/v1/search?q=x",
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 200


def test_healthz_always_open_even_with_auth(authed_client):
    r = authed_client.get("/healthz")
    assert r.status_code == 200
