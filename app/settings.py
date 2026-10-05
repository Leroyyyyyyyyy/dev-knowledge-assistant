"""
Service configuration, read from environment variables (prefix DKA_) or a local .env.

Credentials live only here, never in code, logs or responses.
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DKA_", env_file=".env", extra="ignore")

    # Bearer token that callers of /api/* (Dify's HTTP node) must present.
    # Optional here so the index build command runs without it; the API refuses
    # to start without one (app.main.create_app).
    service_token: str | None = None

    data_dir: Path = REPO_ROOT / "data"
    repos_config: Path = REPO_ROOT / "config" / "repos.json"
    # Base for resolving each repo's relative "checkout" path in repos_config.
    checkout_base: Path = REPO_ROOT

    embed_model: str = "BAAI/bge-m3"
    # One chunk per batch: padded batches of long chunks exhausted MPS memory (NOTES 11).
    embed_batch_size: int = Field(default=1, ge=1)

    top_k_default: int = Field(default=8, ge=1)
    top_k_max: int = Field(default=20, ge=1)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.sqlite3"

    @property
    def chroma_dir(self) -> Path:
        return self.data_dir / "chroma"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshots"
