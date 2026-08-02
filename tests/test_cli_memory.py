from tests.test_sentinel import FakeBroker
from tradewind.cli import main


def setup_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()


def test_memory_show_empty(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    code = main(["memory", "show"])  # broker-less path
    out = capsys.readouterr().out
    assert code == 0 and "no memory files" in out


def test_memory_show_lists_and_prints(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    from tradewind.memory.store import MemoryStore
    from tradewind.store.db import connect

    MemoryStore(tmp_path / "memory", connect(tmp_path / "tradewind.db")).apply(
        "stock", "AAPL", "add", text="earnings vol ±8%")
    assert main(["memory", "show"]) == 0
    out = capsys.readouterr().out
    assert "stocks/AAPL.md" in out
    assert main(["memory", "show", "--layer", "stock", "--key", "AAPL"]) == 0
    assert "±8%" in capsys.readouterr().out


def test_memory_consolidate_without_llm_exits_2(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    code = main(["memory", "consolidate"], broker_factory=lambda s: FakeBroker())
    assert code == 2
    assert "LLM" in capsys.readouterr().err


def test_memory_show_invalid_layer_friendly_error(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    code = main(["memory", "show", "--layer", "identity"])
    assert code == 1
    assert "error:" in capsys.readouterr().err


def test_memory_show_layer_without_key_friendly_error(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    code = main(["memory", "show", "--layer", "strategy"])
    assert code == 1
    assert "error:" in capsys.readouterr().err
