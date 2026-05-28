"""REST gateway settings, sourced from env (matches the docker-compose env_file).

REST is a pure thin gateway — the only backend dependency is the gRPC
target (BarService + AssetService). Postgres / assets.yaml live entirely
on the gRPC server side (see data_svc/grpc_server/__main__.py).
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RestSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    rest_listen_host: str = Field(default="0.0.0.0")
    rest_listen_port: int = Field(default=8001, ge=1, le=65535)
    rest_auth_token: str = Field(default="")
    # Target of the BarService + AssetService gRPC. Spec compose snippet
    # documents this as GRPC_TARGET.
    grpc_target: str = Field(default="bar-grpc:50051")
