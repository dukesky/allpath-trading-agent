import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.store.conversations import ConversationStore
from allpath_trade.web.app import create_app
from tests.helpers import assert_english_only
from tests.test_sentinel import FakeBroker


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        c.post("/login", data={"token": "secret"})
        yield c


def add_report(client, date="2026-08-10", body="REPORT\nbody text", summary="A short summary.",
               conversation_id=None, status="ok"):
    reports = client.app.state.holder.get().reports
    reports.add(date=date, body=body, summary=summary, conversation_id=conversation_id,
               model="opus", tokens_used=100, status=status)


def add_revision(client, date="2026-08-10", strategy_id="s1", status="pending"):
    # `ts` isn't exposed by ReviewQueue.add_strategy_revision (it always
    # stamps "now") -- a proposal-linkage test needs to control which ET
    # date a row belongs to, so it's queued normally and then the `ts`
    # column is overwritten directly, same raw-SQL pattern test_reflect.py
    # already uses for controlling a row's date in these tests.
    c = client.app.state.holder.get()
    (c.strategies.directory / f"{strategy_id}.yaml").write_text(
        "name: S1\nstatus: active\nversion: 1\n"
        "position: {ticker: AAPL, target_weight: 15%}\n"
        "rules:\n  - {id: r1, type: hard, condition: \"price < 100\", action: \"sell all\"}\n")
    rid = c.queue.add_strategy_revision(
        strategy_id=strategy_id, ticker="AAPL",
        old_yaml=(c.strategies.directory / f"{strategy_id}.yaml").read_text(),
        new_yaml="name: S1\nstatus: active\nversion: 2\n"
                 "position: {ticker: AAPL, target_weight: 10%}\n"
                 "rules:\n  - {id: r1, type: hard, condition: \"price < 90\", action: \"sell all\"}\n",
        diff="d", rationale="reflection rationale")
    # A noon ET timestamp keeps this comfortably inside `date` regardless of
    # which UTC offset the test happens to run under.
    c.conn.execute("UPDATE pending_reviews SET ts = ? WHERE id = ?",
                   (f"{date}T16:00:00+00:00", rid))
    c.conn.commit()
    if status != "pending":
        c.conn.execute("UPDATE pending_reviews SET status = ? WHERE id = ?", (status, rid))
        c.conn.commit()
    return rid


def test_list_shows_ok_and_failed_rows(client):
    add_report(client, date="2026-08-10", summary="Everything on plan.")
    add_report(client, date="2026-08-09", status="failed", summary="")
    body = client.get("/reports").text
    assert "2026-08-10" in body and "Everything on plan." in body
    assert "2026-08-09" in body
    assert "failed" in body


def test_list_shows_proposal_count_badge(client):
    add_report(client, date="2026-08-10")
    add_revision(client, date="2026-08-10")
    add_revision(client, date="2026-08-10")
    body = client.get("/reports").text
    assert "2 proposal" in body


def test_list_is_date_desc(client):
    add_report(client, date="2026-08-09")
    add_report(client, date="2026-08-10")
    body = client.get("/reports").text
    assert body.index("2026-08-10") < body.index("2026-08-09")


def test_detail_renders_body_escaped(client):
    add_report(client, date="2026-08-10", body="line one\n<b>bold html</b>\nline two")
    body = client.get("/reports/2026-08-10").text
    assert "<b>bold html</b>" not in body
    assert "&lt;b&gt;bold html&lt;/b&gt;" in body
    assert "line one" in body and "line two" in body


def test_detail_404s_on_missing_date(client):
    r = client.get("/reports/2026-08-10")
    assert r.status_code == 404


def test_detail_404s_on_malformed_date(client):
    r = client.get("/reports/not-a-date")
    assert r.status_code == 404
    r2 = client.get("/reports/..%2f..%2fetc")
    assert r2.status_code in (400, 404)


def test_detail_shows_linked_proposals_with_status(client):
    add_report(client, date="2026-08-10")
    add_revision(client, date="2026-08-10", strategy_id="s1")
    body = client.get("/reports/2026-08-10").text
    assert "s1" in body
    assert "pending" in body.lower()
    assert 'href="/reviews"' in body


def test_transcript_renders_roles_and_tool_lines_and_escapes(client):
    c = client.app.state.holder.get()
    conversations = ConversationStore(c.conn)
    conv_id = conversations.start(kind="reflection")
    conversations.append(conv_id, {"role": "user", "content": "briefing <script>evil()</script>"})
    conversations.append(conv_id, {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "1", "name": "get_bars", "arguments": {"ticker": "MU", "days": 5}}]})
    conversations.append(conv_id, {"role": "tool", "tool_call_id": "1", "content": "x" * 1500})
    conversations.append(conv_id, {"role": "assistant", "content": "REPORT\n...\nSUMMARY\n..."})

    add_report(client, date="2026-08-10", conversation_id=conv_id)
    body = client.get("/reports/2026-08-10/transcript").text

    assert "<script>evil()</script>" not in body
    assert "&lt;script&gt;evil()&lt;/script&gt;" in body
    assert "get_bars(ticker=MU, days=5)" in body
    assert "→" in body
    assert "result:" in body
    assert "1.5k chars" in body
    assert "x" * 100 not in body  # full tool-result content is never rendered


def test_transcript_404s_on_missing_date(client):
    assert client.get("/reports/2026-08-10/transcript").status_code == 404


def test_transcript_with_no_conversation_renders_empty_state(client):
    add_report(client, date="2026-08-10", conversation_id=None)
    body = client.get("/reports/2026-08-10/transcript").text
    assert "No transcript recorded" in body


def test_reports_pages_are_english_only(client):
    add_report(client, date="2026-08-10", summary="A short summary.")
    add_revision(client, date="2026-08-10")
    assert_english_only(client.get("/reports").text)
    assert_english_only(client.get("/reports/2026-08-10").text)
    assert_english_only(client.get("/reports/2026-08-10/transcript").text)


def test_nav_link_present(client):
    add_report(client)
    body = client.get("/").text
    assert 'href="/reports"' in body
    assert ">Reports<" in body
