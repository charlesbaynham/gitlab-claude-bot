import json
from pathlib import Path

import pytest

from gitlab_claude_bot.state import State


def test_load_missing_is_empty(tmp_path: Path) -> None:
    state = State.load(tmp_path / "state.json")
    assert state.mr_cursors == {}
    assert state.last_mr_poll is None


def test_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    State(mr_cursors={"1!2": 34}, last_mr_poll="2026-09-15T12:00:00Z").save(path)
    assert State.load(path) == State(mr_cursors={"1!2": 34}, last_mr_poll="2026-09-15T12:00:00Z")


def test_save_leaves_no_tmp(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    State().save(path)
    assert path.exists()
    assert not path.with_suffix(".tmp").exists()
    assert json.loads(path.read_text()) == {"mr_cursors": {}, "last_mr_poll": None}


def test_failed_save_keeps_previous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.json"
    State(mr_cursors={"1!2": 1}).save(path)

    def broken_replace(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("gitlab_claude_bot.state.os.replace", broken_replace)
    with pytest.raises(OSError):
        State(mr_cursors={"1!2": 2}).save(path)
    assert State.load(path).mr_cursors == {"1!2": 1}
