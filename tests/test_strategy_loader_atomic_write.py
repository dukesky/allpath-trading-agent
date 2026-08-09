"""Coverage for atomic_write_text -- the shared helper both the web
notify-email toggle (web/routes/strategies.py) and draft_strategy
(agent/action_tools.py) use to write strategy YAML without exposing a
truncated file to the in-process sentinel scheduler mid-write."""

from allpath_trade.strategy.loader import atomic_write_text


def test_atomic_write_leaves_no_temp_file_behind_on_success(tmp_path):
    target = tmp_path / "semis.yaml"
    atomic_write_text(target, "name: test\n")
    leftovers = [p for p in tmp_path.iterdir() if p != target]
    assert leftovers == []


def test_atomic_write_target_content_is_complete(tmp_path):
    target = tmp_path / "semis.yaml"
    text = "name: test\nrules:\n  - id: r1\n"
    atomic_write_text(target, text)
    assert target.read_text() == text


def test_atomic_write_overwrites_existing_file_completely(tmp_path):
    target = tmp_path / "semis.yaml"
    target.write_text("name: old\nrules:\n  - id: r1\n  - id: r2\n  - id: r3\n")
    new_text = "name: new\n"
    atomic_write_text(target, new_text)
    # No trailing bytes from the longer old content survive -- a plain
    # write_text over a longer prior file can leave a stale tail if the new
    # content is shorter and something reads mid-write; os.replace swaps the
    # whole file in one step so that can't happen.
    assert target.read_text() == new_text
