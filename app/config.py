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
    app_version: str = "0.7.0"
    timezone: str = "Europe/Brussels"  # used for rendering local times in the UI
    database_url: str = "sqlite:///./data/flights.db"
    log_level: str = "INFO"

    # --- AeroDataBox (via RapidAPI) ---
    aerodatabox_api_key: str = ""
    aerodatabox_host: str = "aerodatabox.p.rapidapi.com"
    # Free tier: ~600 units/month, a status call costs 2 units.
    aerodatabox_monthly_unit_budget: int = 600
    # RapidAPI resets quota on your subscription's billing anniversary, NOT on
    # the 1st of the calendar month. Set this to the day of month you
    # subscribed so the local counter tracks the real window.
    aerodatabox_quota_reset_day: int = 1
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

    # --- adsb.lol (live position, Phase 2) ---
    positions_enabled: bool = True
    adsblol_base_url: str = "https://api.adsb.lol/v2"
    # adsb.lol is community-run and rejects generic User-Agents: it requires a
    # contact point so operators can reach you if your client misbehaves.
    # A project URL is fine; an email address also works.
    adsblol_contact: str = "https://github.com/rvanbruggen/personalflighttracker"
    adsblol_min_seconds_between_calls: float = 1.0
    adsblol_timeout_seconds: float = 15.0
    # Position polling is free, so it can be much tighter than status polling.
    position_poll_seconds: int = 60
    # Drop fixes older than this rather than drawing a stale aircraft.
    position_max_age_seconds: int = 300
    # Keep the trail readable: skip fixes closer together than this.
    position_min_move_km: float = 1.0

    # --- Notifications: Gmail SMTP ---
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""  # your full Gmail address
    smtp_password: str = ""  # Gmail *app password*, not your account password
    mail_from: str = ""  # defaults to smtp_user
    mail_to: str = ""  # where readable alerts go; defaults to smtp_user

    # --- Notifications: IFTTT Webhooks (phone push; needs IFTTT Pro) ---
    ifttt_enabled: bool = True
    # The key from https://ifttt.com/maker_webhooks/settings (the part after
    # /use/ in the URL shown there). Anyone holding it can fire your applets.
    ifttt_webhook_key: str = ""
    # Must match the event name in the applet's "Receive a web request" trigger.
    ifttt_event: str = "flight_notification"
    ifttt_timeout_seconds: float = 10.0
    # This app's address as your phone reaches it, e.g. http://192.168.68.78:8080.
    # Pushes carry a link to the flight page when set; empty leaves it out.
    public_base_url: str = ""

    notifications_enabled: bool = True
    # Email everyone on a flight when tracking starts, and anyone added to a
    # flight later, so the first message they see isn't a surprise delay alert.
    send_tracking_confirmations: bool = True

    # --- Web UI ---
    # How often open pages ask the server whether anything changed (seconds).
    # They only reload when it has. 0 turns auto-refresh off.
    auto_refresh_seconds: int = 30

    # --- Email design ---
    # Render a map image into each alert (OpenStreetMap tiles, cached on disk).
    email_map_enabled: bool = True
    # OSM's tile policy asks for an identifying User-Agent (see
    # http_user_agent) and local caching; heavy use needs permission. A
    # personal tracker fetches a handful of tiles per email.
    map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    map_tile_cache_dir: str = "./data/tiles"
    map_tile_cache_days: int = 14
    # Prepended to every outgoing email subject, so alerts are easy to spot
    # and to filter on in Gmail. Set empty to disable.
    email_subject_prefix: str = "PFT"

    @field_validator("aerodatabox_quota_reset_day")
    @classmethod
    def _valid_day(cls, v: int) -> int:
        if not 1 <= v <= 31:
            raise ValueError("AERODATABOX_QUOTA_RESET_DAY must be between 1 and 31")
        return v

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @property
    def http_user_agent(self) -> str:
        """Identifying User-Agent for outbound requests (adsb.lol, OSM tiles)."""
        return self.adsblol_user_agent

    @property
    def adsblol_user_agent(self) -> str:
        slug = self.app_name.lower().replace(" ", "-")
        return f"{slug}/{self.app_version} (+{self.adsblol_contact})"

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
    def ifttt_configured(self) -> bool:
        return bool(self.ifttt_enabled and self.ifttt_webhook_key and self.ifttt_event)

    def app_link(self, path: str) -> str:
        """Absolute link into the web UI, or "" without PUBLIC_BASE_URL."""
        base = self.public_base_url.strip().rstrip("/")
        return f"{base}{path}" if base else ""

    @property
    def aerodatabox_configured(self) -> bool:
        return bool(self.aerodatabox_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
