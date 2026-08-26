"""Configuration, loaded from environment / .env. No secrets live in the repo."""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- App ---
    app_name: str = "Personal Flight Tracker"
    timezone: str = "Europe/Brussels"  # used for rendering local times in the UI
    database_url: str = "sqlite:///./data/flights.db"
    log_level: str = "INFO"

    # --- AeroDataBox (via RapidAPI) ---
    aerodatabox_api_key: str = ""
    aerodatabox_host: str = "aerodatabox.p.rapidapi.com"
    # Free tier: ~600 units/month, a status call costs 2 units.
    aerodatabox_monthly_unit_budget: int = 600
    aerodatabox_units_per_status_call: int = 2
    # Free tier allows 1 request/second; we stay comfortably under it.
    aerodatabox_min_seconds_between_calls: float = 1.5

    # --- Adaptive polling cadence (minutes unless stated) ---
    poll_far_out_hours: int = 168  # sanity check for flights >24h away (weekly)
    poll_within_24h_minutes: int = 60
    poll_within_2h_minutes: int = 10
    poll_airborne_minutes: int = 5
    poll_retry_minutes: int = 30  # after an error or an unresolved flight number
    # Keep polling this long after scheduled arrival before giving up on a flight
    # that never reported a terminal status.
    poll_abandon_after_hours: int = 12

    # --- Notifications: Gmail SMTP ---
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""  # your full Gmail address
    smtp_password: str = ""  # Gmail *app password*, not your account password
    mail_from: str = ""  # defaults to smtp_user
    mail_to: str = ""  # where readable alerts go; defaults to smtp_user

    # --- Notifications: IFTTT Email trigger ---
    ifttt_enabled: bool = True
    ifttt_trigger_email: str = "trigger@applet.ifttt.com"
    ifttt_hashtag: str = "#flight"

    notifications_enabled: bool = True

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @property
    def effective_mail_from(self) -> str:
        return self.mail_from or self.smtp_user

    @property
    def effective_mail_to(self) -> str:
        return self.mail_to or self.smtp_user

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_user and self.smtp_password and self.effective_mail_to)

    @property
    def aerodatabox_configured(self) -> bool:
        return bool(self.aerodatabox_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
