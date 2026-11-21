"""Configuration management for the Telegram Business Assistant Bot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Configuration error with user-friendly message."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


def _parse_bool(value: str | bool) -> bool:
    """Parse boolean from string."""
    if isinstance(value, bool):
        return value
    return value.lower() in ("true", "1", "yes", "on")


def _parse_int(value: str | int, *, minimum: int = 1) -> int:
    """Parse integer with minimum validation."""
    try:
        result = int(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Expected integer, got: {value}") from e
    if result < minimum:
        raise ConfigError(f"Value must be at least {minimum}, got: {result}")
    return result


def _validate_token_format(token: str, *, field_name: str) -> str:
    """Validate token format."""
    if not token or not isinstance(token, str):
        raise ConfigError(f"{field_name} is required")
    return token.strip()


def _validate_bot_token(token: str) -> str:
    """Validate Telegram bot token format."""
    token = _validate_token_format(token, field_name="TELEGRAM_BOT_TOKEN")
    parts = token.split(":")
    if len(parts) != 2 or not parts[0].isdigit():
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN must be in format <number>:<string>",
            hint="Check .env file or environment variables",
        )
    return token


def _validate_owner_id(value: str | int) -> int:
    """Validate OWNER_ID."""
    try:
        result = int(value)
    except (TypeError, ValueError) as e:
        raise ConfigError("OWNER_ID must be a number") from e
    if result <= 0:
        raise ConfigError("OWNER_ID must be positive")
    return result


def _validate_claude_oauth_token(token: str | None) -> str | None:
    """Validate CLAUDE_CODE_OAUTH_TOKEN."""
    if token is None or token == "":
        return None
    token = token.strip()
    if not token:
        return None
    if " " in token or "\n" in token or "\r" in token or "\t" in token:
        raise ConfigError(
            "CLAUDE_CODE_OAUTH_TOKEN must be a single line without whitespace",
            hint="Paste token in editor as single line, not with echo",
        )
    if token.startswith("sk-ant-api"):
        raise ConfigError(
            "CLAUDE_CODE_OAUTH_TOKEN: this is an API key, not a subscription token",
            hint="Run 'claude setup-token' in a browser machine to get sk-ant-oat... token",
        )
    if not token.startswith("sk-ant-oat"):
        raise ConfigError(
            "CLAUDE_CODE_OAUTH_TOKEN must start with 'sk-ant-oat'",
            hint="Run 'claude setup-token' to get a valid subscription token",
        )
    return token


def _validate_effort(value: str) -> str:
    """Validate CLAUDE_EFFORT."""
    allowed = {"low", "medium", "high", "xhigh", "max"}
    if value not in allowed:
        raise ConfigError(
            f"CLAUDE_EFFORT must be one of: {', '.join(sorted(allowed))}",
            hint="Check .env file or environment variables",
        )
    return value


def _env_file_warning(path: Path) -> str | None:
    """Check .env file permissions and return warning if needed."""
    try:
        stat = path.stat()
        # Check if group or others have read/write/execute permissions
        if stat.st_mode & 0o077:
            return (
                f"WARNING: {path} is readable by group/others. "
                f"Run: chmod 600 {path}"
            )
    except OSError:
        pass
    return None


SETTING_KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "OWNER_ID",
    "AUTO_SEND",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_BIN",
    "CLAUDE_MODEL",
    "CLAUDE_EFFORT",
    "CLAUDE_TIMEOUT_SECONDS",
    "LIMIT_COOLDOWN_MINUTES",
    "OWNER_TAKEOVER_MINUTES",
    "HISTORY_LIMIT",
    "DATA_DIR",
    "PROFILE_PATH",
]


@dataclass(frozen=True)
class Settings:
    """Application settings."""

    bot_token: str = field(repr=False)
    owner_id: int
    auto_send: bool
    claude_oauth_token: str | None = field(repr=False, default=None)
    claude_bin: str = "claude"
    claude_model: str | None = None
    claude_effort: str = "low"
    claude_timeout_seconds: int = 120
    limit_cooldown_minutes: int = 30
    owner_takeover_minutes: int = 15
    history_limit: int = 30
    data_dir: str = "data"
    profile_path: str = "profile.md"
    from_environment: bool = False

    @property
    def claude_auth_mode(self) -> str:
        """Return human-readable auth mode description."""
        if self.claude_oauth_token is None:
            return "this machine's claude login"
        if self.from_environment:
            return "subscription token from environment variables"
        return "subscription token from .env"


def load_settings(
    env_file: str | Path = ".env",
    *,
    require_telegram: bool = True,
    overrides: dict[str, Any] | None = None,
    source: dict[str, str] | None = None,
) -> Settings:
    """
    Load settings from .env file or provided source dict.

    Args:
        env_file: Path to .env file (used for relative paths and warnings)
        require_telegram: Whether to require TELEGRAM_BOT_TOKEN and OWNER_ID
        overrides: Dict of overrides (for testing)
        source: Dict of key-value pairs to use instead of file (for container mode)
    """
    env_path = Path(env_file).resolve()
    values: dict[str, str] = {}

    if source is not None:
        # Container mode: use provided source dict
        values = {k: v for k, v in source.items() if v is not None}
    else:
        # Local mode: read from .env file using dotenv_values
        try:
            from dotenv import dotenv_values
        except ImportError as e:
            raise ConfigError("python-dotenv not installed") from e

        file_values = dotenv_values(env_path)
        values = {k: v for k, v in file_values.items() if v is not None}

    # Apply overrides (for testing)
    if overrides:
        values.update({k: v for k, v in overrides.items() if v is not None})

    # Validate and parse
    try:
        if require_telegram:
            bot_token = _validate_bot_token(values.get("TELEGRAM_BOT_TOKEN", ""))
            owner_id = _validate_owner_id(values.get("OWNER_ID", ""))
        else:
            bot_token = values.get("TELEGRAM_BOT_TOKEN", "dummy:token")
            owner_id = _validate_owner_id(values.get("OWNER_ID", "0"))

        auto_send = _parse_bool(values.get("AUTO_SEND", "false"))
        claude_oauth_token = _validate_claude_oauth_token(values.get("CLAUDE_CODE_OAUTH_TOKEN"))
        claude_bin = values.get("CLAUDE_BIN", "").strip() or "claude"
        claude_model = values.get("CLAUDE_MODEL", "").strip() or None
        claude_effort = _validate_effort(values.get("CLAUDE_EFFORT", "low"))
        claude_timeout_seconds = _parse_int(values.get("CLAUDE_TIMEOUT_SECONDS", "120"), minimum=1)
        limit_cooldown_minutes = _parse_int(values.get("LIMIT_COOLDOWN_MINUTES", "30"), minimum=1)
        owner_takeover_minutes = _parse_int(values.get("OWNER_TAKEOVER_MINUTES", "15"), minimum=0)
        history_limit = _parse_int(values.get("HISTORY_LIMIT", "30"), minimum=1)
        data_dir = values.get("DATA_DIR", "data")
        profile_path = values.get("PROFILE_PATH", "profile.md")

    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"Invalid configuration: {e}") from e

    # Check .env permissions in local mode
    warning = None
    if source is None and env_path.exists():
        warning = _env_file_warning(env_path)

    return Settings(
        bot_token=bot_token,
        owner_id=owner_id,
        auto_send=auto_send,
        claude_oauth_token=claude_oauth_token,
        claude_bin=claude_bin,
        claude_model=claude_model,
        claude_effort=claude_effort,
        claude_timeout_seconds=claude_timeout_seconds,
        limit_cooldown_minutes=limit_cooldown_minutes,
        owner_takeover_minutes=owner_takeover_minutes,
        history_limit=history_limit,
        data_dir=data_dir,
        profile_path=profile_path,
        from_environment=source is not None,
    )