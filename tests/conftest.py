import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leadengine.config import Settings  # noqa: E402
from leadengine.db import DB  # noqa: E402
from leadengine.pipeline import Pipeline  # noqa: E402


@pytest.fixture()
def settings():
    return Settings(db_path=":memory:", api_key="", dnc_file="", meta_app_secret="s3cret", meta_verify_token="vt",
                    meta_page_token="tok", form_webhook_secret="fsecret", apify_token="t", reddit_client_id="i", reddit_client_secret="s")


@pytest.fixture()
def pipe(settings):
    return Pipeline(DB(":memory:"), settings)
