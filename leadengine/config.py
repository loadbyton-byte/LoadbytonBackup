from __future__ import annotations

import os
from dataclasses import dataclass, field, fields


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Rates:
    """Market/legal rates used by the calculators. Defaults are indicative starting points, NOT legal advice:
    verify current DLD fees, agency commission, mortgage rules and LTV limits before quoting a client."""
    dld_pct: float = 4.0                # Dubai Land Department transfer fee, % of price (verify current rules)
    dld_admin_fee: float = 580.0        # fixed registration/admin fee, AED (verify current rules)
    agency_pct: float = 2.0             # buyer-side agency commission, % of price (negotiable; verify)
    vat_pct: float = 5.0                # VAT on the commission (verify)
    mortgage_reg_pct: float = 0.25      # mortgage registration, % of loan (verify)
    mortgage_reg_fee: float = 290.0     # fixed mortgage registration fee, AED (verify)
    default_ltv_pct: float = 80.0       # typical max loan-to-value for a resident first purchase (varies by price/buyer; verify)
    default_rate_pct: float = 4.5       # illustrative interest rate (verify with lenders)
    default_years: int = 25
    dbr_pct: float = 50.0               # max monthly debt payments as % of income used by the affordability calculator (verify current rules)


    @classmethod
    def from_env(cls) -> "Rates":
        """Defaults overridden by env vars of the same name upper-cased (DLD_PCT, AGENCY_PCT, ...). Bad values fall back silently."""
        base = cls()
        vals = {}
        for f in fields(cls):
            raw = os.environ.get(f.name.upper())
            if raw:
                try:
                    vals[f.name] = type(getattr(base, f.name))(raw)
                except ValueError:
                    pass
        return cls(**vals)


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
    rates: Rates = field(default_factory=Rates.from_env)
    # Golden-visa property threshold in AED — configuration, not law: verify current rules before relying on it.
    golden_visa_min_aed: float = field(default_factory=lambda: float(_env("GOLDEN_VISA_MIN_AED", "2000000")))
    # Number of reverse proxies in front of the app that append to X-Forwarded-For (0 = trust only the socket peer).
    trusted_proxies: int = field(default_factory=lambda: int(_env("TRUSTED_PROXIES", "0") or 0))
    # Public lead capture: comma-separated browser origins allowed to POST /capture (empty = same host only).
    capture_origins: list[str] = field(default_factory=lambda: [o.strip() for o in _env("CAPTURE_ORIGINS").split(",") if o.strip()])
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL"))  # base URL embedded in /widget.js (defaults to the request host)
    # WhatsApp Cloud API. Sending is OFF by default (dry-run): replies are only recorded until explicitly enabled.
    whatsapp_send_enabled: bool = field(default_factory=lambda: _env("WHATSAPP_SEND_ENABLED").lower() in ("1", "true", "yes"))
    whatsapp_token: str = field(default_factory=lambda: _env("WHATSAPP_TOKEN"))
    whatsapp_phone_id: str = field(default_factory=lambda: _env("WHATSAPP_PHONE_ID"))
    # Platform mode (accounts + workspaces). Legacy single-tenant mode when CONTROL_DB is empty.
    control_db: str = field(default_factory=lambda: _env("CONTROL_DB"))
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "data"))
    encryption_key: str = field(default_factory=lambda: _env("ENCRYPTION_KEY"))
    base_url: str = field(default_factory=lambda: _env("BASE_URL", "http://127.0.0.1:8000"))
    dev_insecure_cookies: bool = field(default_factory=lambda: _env("DEV_INSECURE_COOKIES") in ("1", "true", "yes"))
    signup_mode: str = field(default_factory=lambda: _env("SIGNUP_MODE", "open"))          # open | invite
    mail_backend: str = field(default_factory=lambda: _env("MAIL_BACKEND", "console"))     # console | smtp
    contact_email: str = field(default_factory=lambda: _env("CONTACT_EMAIL"))               # shown on the public Contact page
    mail_from: str = field(default_factory=lambda: _env("MAIL_FROM", "DigitalBurj Leads <no-reply@localhost>"))
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: int(_env("SMTP_PORT", "587") or 587))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    # Connectors
    reddit_client_id: str = field(default_factory=lambda: _env("REDDIT_CLIENT_ID"))
    reddit_client_secret: str = field(default_factory=lambda: _env("REDDIT_CLIENT_SECRET"))
    reddit_user_agent: str = field(default_factory=lambda: _env("REDDIT_USER_AGENT", "uae-lead-engine/1.0 (contact: you@example.com)"))
    meta_app_secret: str = field(default_factory=lambda: _env("META_APP_SECRET"))
    meta_verify_token: str = field(default_factory=lambda: _env("META_VERIFY_TOKEN"))
    meta_page_token: str = field(default_factory=lambda: _env("META_PAGE_TOKEN"))
    form_webhook_secret: str = field(default_factory=lambda: _env("FORM_WEBHOOK_SECRET"))
    apify_token: str = field(default_factory=lambda: _env("APIFY_TOKEN"))
