"""REST gateway settings, sourced from env (matches the docker-compose env_file).

Reuses `POSTGRES_URL` from the existing data-svc / bar-grpc env — does NOT
introduce a separate DATABASE_URL despite what the original spec doc suggested.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_ASSETS_YAML = Path(__file__).resolve().parent.parent / "assets.yaml"


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
    # Honor either env var; spec uses DATABASE_URL, existing services use POSTGRES_URL.
    postgres_url: str = Field(
        default="postgresql://datasvc:datasvc@postgres:5432/datasvc",
        validation_alias=AliasChoices("postgres_url", "database_url"),
    )
    # Target of the BarService gRPC; consumed by the gRPC client in the
    # /v1/quote and /healthz routes (spec §Deployment).
    grpc_target: str = Field(default="bar-grpc:50051")
    assets_yaml_path: Path = Field(default=_DEFAULT_ASSETS_YAML)
