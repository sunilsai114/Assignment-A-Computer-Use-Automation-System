"""Settings. Secrets come from .env only and are SecretStr so they never print or serialize."""
from pathlib import Path

import yaml
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash-lite"


def load_policy_dict(path: Path | None = None) -> dict:
    return yaml.safe_load((path or ROOT / "config" / "policy.yaml").read_text())
