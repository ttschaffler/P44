import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("WAYPOINT_DB", "waypoint.db")
    secret_key: str = os.environ.get("WAYPOINT_SECRET_KEY", "dev-insecure-change-me")
    bootstrap_token: str = os.environ.get("WAYPOINT_BOOTSTRAP_TOKEN", "")
    public_url: str = os.environ.get("WAYPOINT_PUBLIC_URL", "http://localhost:8000")
    session_hours: int = int(os.environ.get("WAYPOINT_SESSION_HOURS", "12"))
    invite_hours: int = int(os.environ.get("WAYPOINT_INVITE_HOURS", "72"))
    sweep_seconds: int = int(os.environ.get("WAYPOINT_SWEEP_SECONDS", "300"))


def get_settings() -> Settings:
    return Settings()
