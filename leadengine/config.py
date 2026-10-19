from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: _env("LEADENGINE_DB", "leadengine.db"))
    api_key: str = field(default_factory=lambda: _env("LEADENGINE_API_KEY"))
    # Agency identity — required in every outreach draft (RERA/DLD advertising rules).
    agency_name: str = field(default_factory=lambda: _env("AGENCY_NAME", "Your Agency LLC"))
    agent_name: str = field(default_factory=lambda: _env("AGENT_NAME", "Your Name"))
    brn: str = field(default_factory=lambda: _env("AGENT_BRN", "<RERA BRN>"))
    trakheesi: str = field(default_factory=lambda: _env("TRAKHEESI_PERMIT", "<Trakheesi permit>"))
    # Compliance knobs (defaults are conservative; confirm with counsel for your licence type).
    retention_days_public: int = field(default_factory=lambda: int(_env("RETENTION_DAYS_PUBLIC", "90")))
    retention_days_optin: int = field(default_factory=lambda: int(_env("RETENTION_DAYS_OPTIN", "730")))
    contact_hour_start: int = field(default_factory=lambda: int(_env("CONTACT_HOUR_START", "9")))
    contact_hour_end: int = field(default_factory=lambda: int(_env("CONTACT_HOUR_END", "20")))
    dnc_file: str = field(default_factory=lambda: _env("DNC_FILE"))  # one E.164 number per line (TDRA DNCR export)
    # Golden-visa property threshold in AED — configuration, not law: verify current rules before relying on it.
    golden_visa_min_aed: float = field(default_factory=lambda: float(_env("GOLDEN_VISA_MIN_AED", "2000000")))
    # Connectors
    reddit_client_id: str = field(default_factory=lambda: _env("REDDIT_CLIENT_ID"))
    reddit_client_secret: str = field(default_factory=lambda: _env("REDDIT_CLIENT_SECRET"))
    reddit_user_agent: str = field(default_factory=lambda: _env("REDDIT_USER_AGENT", "uae-lead-engine/1.0 (contact: you@example.com)"))
    meta_app_secret: str = field(default_factory=lambda: _env("META_APP_SECRET"))
    meta_verify_token: str = field(default_factory=lambda: _env("META_VERIFY_TOKEN"))
    meta_page_token: str = field(default_factory=lambda: _env("META_PAGE_TOKEN"))
    form_webhook_secret: str = field(default_factory=lambda: _env("FORM_WEBHOOK_SECRET"))
    apify_token: str = field(default_factory=lambda: _env("APIFY_TOKEN"))
