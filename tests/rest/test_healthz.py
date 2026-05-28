from __future__ import annotations


def test_healthz_open_no_auth(client):
    """/healthz is always open even when auth would gate /v1/*.

    With the fake gRPC client (reachable=True by default) and an
    ephemeral Postgres up, both flags should be true and status=ok.
    """
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"status", "grpc_reachable", "db_reachable"}
    assert body["db_reachable"] is True
    assert body["grpc_reachable"] is True
    assert body["status"] == "ok"


def test_healthz_degraded_when_grpc_unreachable(client):
    """grpc_reachable=False flips status to degraded; db_reachable also
    False because /healthz can no longer reach the server that runs the
    SELECT 1."""
    client.app.state.grpc_client.set_reachable(False)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["grpc_reachable"] is False
    assert body["db_reachable"] is False
    assert body["status"] == "degraded"
