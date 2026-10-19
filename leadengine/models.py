from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

# How the signal was obtained. Drives scoring weight AND which contact channels are lawful.
SOURCE_TYPES = ("lead_form", "portal_inquiry", "public_post", "public_comment", "import")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Signal(BaseModel):
    """One observation about one person on one platform (post, comment, form submission...)."""

    platform: str                                  # reddit|facebook|instagram|tiktok|linkedin|x|portal|web|csv
    external_id: str                               # platform-native id; (platform, external_id) is the dedupe key
    source_type: str = "public_post"
    text: str = ""
    url: str | None = None
    author_handle: str | None = None
    author_name: str | None = None
    phone: str | None = None                       # only ever populated by first-party forms / portals / uploads
    email: str | None = None
    consent_marketing: bool = False                # explicit opt-in captured with the form
    consent_text: str | None = None                # the exact wording shown to the person (audit evidence)
    created_at: datetime = Field(default_factory=utcnow)
    campaign: str | None = None
    listing_ref: str | None = None                 # portal listing/ad reference the enquiry was about
    extra: dict = Field(default_factory=dict)
