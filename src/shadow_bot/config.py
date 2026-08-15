from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigurationError(RuntimeError):
    """Raised when required application configuration is missing or invalid."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is missing")
    return value


def _optional_snowflake(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        snowflake = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a Discord numeric ID") from exc
    if snowflake <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return snowflake


def _snowflake_set(name: str) -> frozenset[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return frozenset()
    try:
        values = frozenset(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise ConfigurationError(
            f"{name} must contain comma-separated Discord numeric IDs"
        ) from exc
    if any(value <= 0 for value in values):
        raise ConfigurationError(f"Every ID in {name} must be positive")
    return values


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str
    database_url: str
    bot_owner_ids: frozenset[int]
    test_guild_id: int | None
    enable_members_intent: bool
    enable_message_content_intent: bool
    log_level: str

    @classmethod
    def from_environment(cls) -> Settings:
        load_dotenv()
        database_url = _required("DATABASE_URL")
        if not database_url.startswith("postgresql+psycopg://"):
            raise ConfigurationError(
                "DATABASE_URL must start with postgresql+psycopg:// for async PostgreSQL access"
            )
        return cls(
            discord_token=_required("DISCORD_TOKEN"),
            database_url=database_url,
            bot_owner_ids=_snowflake_set("BOT_OWNER_IDS"),
            test_guild_id=_optional_snowflake("TEST_GUILD_ID"),
            enable_members_intent=_boolean("ENABLE_MEMBERS_INTENT", True),
            enable_message_content_intent=_boolean("ENABLE_MESSAGE_CONTENT_INTENT", False),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
