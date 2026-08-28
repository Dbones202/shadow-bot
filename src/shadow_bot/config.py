from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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


def _optional_path(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    return Path(value)


def _optional_str(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


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
    events_dir: Path | None
    enable_members_intent: bool
    enable_message_content_intent: bool
    log_level: str
    #: The only user id allowed to run /media allow|revoke. Deliberately not
    #: bot_owner_ids — that set already carries economy-admin fallback
    #: authority in every guild, and reusing it here would mean anyone ever
    #: added there for economy support also silently gained media-request
    #: gatekeeping. A missing value just means /media allow refuses everyone,
    #: which is the safe default until Donovan sets it.
    media_owner_id: int | None
    radarr_url: str | None
    radarr_api_key: str | None
    radarr_quality_profile_id: int | None
    radarr_root_folder: str | None
    sonarr_url: str | None
    sonarr_api_key: str | None
    sonarr_quality_profile_id: int | None
    sonarr_root_folder: str | None

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
            # Not required to exist — a missing or empty directory just means
            # load_event_library() falls back to the packaged narration.
            # Validating it here would turn "not deployed yet" into a boot failure.
            events_dir=_optional_path("EVENTS_DIR"),
            enable_members_intent=_boolean("ENABLE_MEMBERS_INTENT", True),
            enable_message_content_intent=_boolean("ENABLE_MESSAGE_CONTENT_INTENT", False),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            media_owner_id=_optional_snowflake("MEDIA_OWNER_ID"),
            radarr_url=_optional_str("RADARR_URL"),
            radarr_api_key=_optional_str("RADARR_API_KEY"),
            radarr_quality_profile_id=_optional_int("RADARR_QUALITY_PROFILE_ID"),
            radarr_root_folder=_optional_str("RADARR_ROOT_FOLDER"),
            sonarr_url=_optional_str("SONARR_URL"),
            sonarr_api_key=_optional_str("SONARR_API_KEY"),
            sonarr_quality_profile_id=_optional_int("SONARR_QUALITY_PROFILE_ID"),
            sonarr_root_folder=_optional_str("SONARR_ROOT_FOLDER"),
        )
