import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import ALLOWED, BOT, FakeGitLab, fixture
from gitlab_claude_bot import feeds, main
from gitlab_claude_bot.config import Config
from gitlab_claude_bot.gitlab import GitLabError, Project
from gitlab_claude_bot.health import Status
from gitlab_claude_bot.state import State
from gitlab_claude_bot.triggers import Trigger

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
PROJECT = Project(42, "group/app", "main", "https://gitlab.example/group/app.git")


def config(tmp_path: Path) -> Config:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return Config(
        gitlab_url="https://gitlab.example",
        gitlab_token="token",
        claude_env={"ANTHROPIC_API_KEY": "key"},
        gitlab_allowed_users=ALLOWED,
        agent_image="agent:test",
        state_dir=state_dir,
        work_dir=tmp_path / "work",
    )


def poll(gl: FakeGitLab, cfg: Config, st: Status) -> int:
    account = main.Account(gl, feeds.todo_triggers, ALLOWED, BOT, State(), cfg.state_dir / "state.json")
    return main.poll_once([account], cfg, st, NOW)


def status() -> Status:
    return Status(poll_interval=30, job_timeout=1800, bot_username=BOT.username)


def gitlab(*todos: dict) -> FakeGitLab:
    return FakeGitLab(
        todos=list(todos),
        discussions={(42, "issues", 7): fixture("discussions_issue")},
        projects={42: PROJECT},
    )


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Trigger, str]]:
    calls: list[tuple[Trigger, str]] = []

    def run_job(gl: object, cfg: Config, bot: object, trigger: Trigger, name: str | None = None) -> bool:
        calls.append((trigger, name))
        return True

    monkeypatch.setattr(main.jobs, "run_job", run_job)
    return calls


def test_a_trigger_runs_one_job_and_updates_the_status(tmp_path: Path, ran: list) -> None:
    gl, cfg, st = gitlab(fixture("todo_assigned_issue")), config(tmp_path), status()

    assert poll(gl, cfg, st) == 1

    (trigger, name), = ran
    assert (trigger.target.iid, trigger.action) == (7, "assigned")
    assert name.startswith("42-is7-")
    assert (st.jobs_done, st.jobs_failed, st.job_running) == (1, 0, None)
    assert st.last_poll_ok == NOW
    assert st.healthy(NOW)


def test_ignored_todos_are_marked_done_without_a_job(tmp_path: Path, ran: list) -> None:
    gl = gitlab(fixture("todo_review_requested"), fixture("todo_not_allowed"))

    assert poll(gl, config(tmp_path), status()) == 0

    assert gl.done == [507, 508]
    assert ran == []


def test_an_already_acknowledged_note_is_skipped(tmp_path: Path, ran: list) -> None:
    gl = gitlab(fixture("todo_note_mention_issue"))
    todo = fixture("todo_note_mention_issue")
    note_id = int(todo["target_url"].rsplit("#note_", 1)[1])
    gl.eyes.add((todo["project"]["id"], "issues", todo["target"]["iid"], note_id, BOT.id))

    assert poll(gl, config(tmp_path), status()) == 0

    assert ran == []
    assert gl.done == [todo["id"]]


def test_the_state_file_is_written_every_poll(tmp_path: Path, ran: list) -> None:
    cfg = config(tmp_path)

    poll(gitlab(), cfg, status())

    saved = json.loads((cfg.state_dir / "state.json").read_text())
    assert saved["last_mr_poll"] == "2026-09-15T12:00:00+00:00"


def test_a_failed_job_counts_as_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.jobs, "run_job", lambda *args, **kwargs: False)
    st = status()

    assert poll(gitlab(fixture("todo_assigned_issue")), config(tmp_path), st) == 1

    assert (st.jobs_done, st.jobs_failed) == (1, 1)


def test_a_gitlab_error_is_recorded_not_raised(tmp_path: Path, ran: list) -> None:
    gl = gitlab()
    gl.pending_todos = lambda: (_ for _ in ()).throw(GitLabError(500, "upstream exploded"))
    st = status()

    assert poll(gl, config(tmp_path), st) == 0

    assert st.last_poll_ok is None
    assert "500" in st.last_poll_error
    assert not st.healthy(NOW)


def test_a_later_good_poll_clears_the_error(tmp_path: Path, ran: list) -> None:
    st = status()
    st.last_poll_error = "upstream exploded"

    poll(gitlab(), config(tmp_path), st)

    assert st.last_poll_error is None


def test_one_forge_failing_does_not_stop_the_other(tmp_path: Path, ran: list) -> None:
    broken = gitlab()
    broken.pending_todos = lambda: (_ for _ in ()).throw(GitLabError(502, "bad gateway"))
    cfg, st = config(tmp_path), status()
    healthy = gitlab(fixture("todo_assigned_issue"))
    accounts = [
        main.Account(broken, feeds.todo_triggers, ALLOWED, BOT, State(), cfg.state_dir / "a.json"),
        main.Account(healthy, feeds.todo_triggers, ALLOWED, BOT, State(), cfg.state_dir / "b.json"),
    ]

    assert main.poll_once(accounts, cfg, st, NOW) == 1

    assert st.last_poll_ok is None
    assert st.last_poll_error.startswith("GitLab: GitLab returned 502")
    assert (cfg.state_dir / "b.json").exists()


def test_each_configured_forge_gets_an_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Client(FakeGitLab):
        def __init__(self, url: str, token: str):
            super().__init__()

        def me(self) -> object:
            return BOT

    monkeypatch.setattr(main, "GitLab", Client)
    monkeypatch.setattr(main, "GitHub", Client)
    cfg = config(tmp_path)

    only_gitlab = main._accounts(cfg)
    both = main._accounts(dataclasses.replace(cfg, github_token="ghp", github_allowed_users=frozenset({"carol"})))

    assert [a.state_path.name for a in only_gitlab] == ["state.json"]
    assert [(a.state_path.name, a.allowed) for a in both][1] == ("state-github.json", frozenset({"carol"}))
