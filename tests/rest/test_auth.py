from __future__ import annotations


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
