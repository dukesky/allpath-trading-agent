"""TDSC R5 / dev-requests item 6: reflection token metering.

Reflection already ran through the memory tier's recording client, but its
`llm_usage` rows were indistinguishable from consolidation's (purpose =
"memory", no account), and `reports.tokens_used` was always 0."""

from allpath_trade.app import build_components
from allpath_trade.config import Settings
from allpath_trade.llm.base import LLMResponse
from allpath_trade.llm.factory import _RecordingClient, tag_purpose
from allpath_trade.reflect import Reflector
from allpath_trade.store.db import connect
from allpath_trade.store.llm_usage import LLMUsage
from tests.test_agent_loop import ScriptedLLM, tool_response
from tests.test_cli import FakeBroker
from tests.test_reflect import ET_DATE, NOW, REPORT_TEXT, make_components, make_settings


class _Inner:
    model = "m-mem"

    def complete(self, messages, tools=None):
        return LLMResponse(text="ok", input_tokens=7, output_tokens=3)


def test_tag_purpose_labels_recorded_rows(tmp_path):
    conn = connect(tmp_path / "t.db")
    client = tag_purpose(_RecordingClient(_Inner(), "memory", LLMUsage(conn)),
                         "reflection:shadow")
    client.complete([{"role": "user", "content": "x"}])
    row = conn.execute("SELECT tier, purpose FROM llm_usage").fetchone()
    assert (row["tier"], row["purpose"]) == ("memory", "reflection:shadow")


def test_tag_purpose_is_a_noop_on_plain_clients():
    inner = _Inner()
    assert tag_purpose(inner, "reflection:paper") is inner
    assert not hasattr(inner, "purpose")


def test_build_components_tags_reflection_and_consolidation_per_account(tmp_path):
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", openrouter_api_key="k")
    settings.strategies_dir.mkdir()
    components = build_components(settings, broker=FakeBroker())
    for account in ("paper", "shadow"):
        bundle = components.accounts[account]
        assert bundle.reflector.llm.purpose == f"reflection:{account}"
        assert bundle.consolidator.llm.purpose == f"consolidation:{account}"
        assert bundle.sentinel.review_agent is not None


def _with_tokens(resp: LLMResponse, i: int, o: int) -> LLMResponse:
    return resp.model_copy(update={"input_tokens": i, "output_tokens": o})


def test_ok_report_records_tokens_of_every_call(tmp_path):
    components = make_components(tmp_path)
    llm = ScriptedLLM([
        _with_tokens(tool_response("get_portfolio", {}), 1000, 50),
        _with_tokens(LLMResponse(text=REPORT_TEXT), 1200, 300),
    ])
    Reflector(llm=llm, components=components, settings=make_settings()).run_daily(now=NOW)
    row = components.reports.get(ET_DATE)
    assert row["status"] == "ok"
    assert row["tokens_used"] == 1000 + 50 + 1200 + 300


def test_failed_report_records_tokens_including_corrective_turn(tmp_path):
    components = make_components(tmp_path)
    llm = ScriptedLLM([
        _with_tokens(LLMResponse(text="no structure here"), 500, 20),
        _with_tokens(LLMResponse(text="still no structure"), 600, 10),
    ])
    Reflector(llm=llm, components=components, settings=make_settings()).run_daily(now=NOW)
    row = components.reports.get(ET_DATE)
    assert row["status"] == "failed"
    assert row["tokens_used"] == 500 + 20 + 600 + 10
