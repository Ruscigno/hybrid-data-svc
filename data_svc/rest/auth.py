"""Optional bearer auth.

When `REST_AUTH_TOKEN` is unset or empty, the dependency is a no-op and
every request passes through (suitable for the private docker network).
When it's set, every `/v1/*` request must carry
`Authorization: Bearer <token>` exact-match; failure is 401.

`/healthz` is wired without this dependency so liveness probes work
regardless of token configuration.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .settings import RestSettings

_bearer_scheme = HTTPBearer(auto_error=False)


def require_bearer(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    settings: RestSettings = request.app.state.settings
    expected = settings.rest_auth_token
    if not expected:
        return  # open mode

    presented = creds.credentials if creds and creds.scheme.lower() == "bearer" else ""
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "missing or invalid bearer token"},
            headers={"WWW-Authenticate": "Bearer"},
        )
