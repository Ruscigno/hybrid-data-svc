"""REST gateway settings, sourced from env (matches the docker-compose env_file).

Reuses `POSTGRES_URL` from the existing data-svc / bar-grpc env — does NOT
introduce a separate DATABASE_URL despite what the original spec doc suggested.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
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
    postgres_url: str = Field(
        default="postgresql://datasvc:datasvc@postgres:5432/datasvc"
    )
    assets_yaml_path: Path = Field(default=_DEFAULT_ASSETS_YAML)
