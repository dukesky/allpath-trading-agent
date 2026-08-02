import os

import pytest

from allpath_trade.config import Settings
from allpath_trade.llm.factory import build_llm

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not os.getenv("OPENROUTER_API_KEY"),
                    reason="OPENROUTER_API_KEY not set")
def test_openrouter_round_trip():
    s = Settings(_env_file=None, llm_provider="openrouter",
                 openrouter_api_key=os.environ["OPENROUTER_API_KEY"])
    out = build_llm(s, tier="review").complete(
        [{"role": "user", "content": "Reply with exactly: OK"}])
    assert out.text and out.text.strip()
