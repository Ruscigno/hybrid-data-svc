"""Optional bearer auth.

Two dependencies:

  * `require_bearer` — used by read routes (and POST /v1/assets when no
    dedicated admin token is configured). When `REST_AUTH_TOKEN` is set,
    every request must carry `Authorization: Bearer <token>` exact-match;
    failure is 401. When it's unset, the dependency is a no-op.

  * `require_bearer_admin` — used by catalog-mutating routes. Precedence:
      1. `REST_ADMIN_TOKEN` set → that token is the only accepted credential.
      2. unset + `REST_AUTH_TOKEN` set → fall back to `REST_AUTH_TOKEN`.
      3. both unset → open mode (matches the rest of `/v1/*`).
    Same `hmac.compare_digest` constant-time comparison and 401 shape.

`/healthz` is wired without either dependency so liveness probes work
regardless of token configuration.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .settings import RestSettings

_bearer_scheme = HTTPBearer(auto_error=False)


def _extract_bearer(creds: HTTPAuthorizationCredentials | None) -> str:
    return creds.credentials if creds and creds.scheme.lower() == "bearer" else ""


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "unauthorized", "message": "missing or invalid bearer token"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_bearer(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    settings: RestSettings = request.app.state.settings
    expected = settings.rest_auth_token
    if not expected:
        return  # open mode

    presented = _extract_bearer(creds)
    if not hmac.compare_digest(presented, expected):
        raise _unauthorized()


def require_bearer_admin(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """Auth gate for catalog-mutating routes. See module docstring for the
    precedence rule (admin → auth → open)."""
    settings: RestSettings = request.app.state.settings
    expected = settings.rest_admin_token or settings.rest_auth_token
    if not expected:
        return  # open mode (matches /v1/* reads)

    presented = _extract_bearer(creds)
    if not hmac.compare_digest(presented, expected):
        raise _unauthorized()
