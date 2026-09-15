from pathlib import Path

import pytest
from conftest import ALLOWED, BOT, FakeGitLab
from gitlab_claude_bot import jobs, runner
from gitlab_claude_bot.config import Config
from gitlab_claude_bot.gitlab import Project
from gitlab_claude_bot.runner import AgentResult, Changes, JobError, Repo
from gitlab_claude_bot.triggers import Target, Trigger

PROJECT = Project(42, "group/app", "main", "https://gitlab.example/group/app.git")
FORK = Project(77, "someone/app", "main", "https://gitlab.example/someone/app.git")
ISSUE = {"iid": 7, "title": "Fix the config loader", "description": "It crashes on an empty file."}
BRANCH = "bot/issue-7-fix-the-config-loader"

ASSIGNED = Trigger(Target(42, "issues", 7), "assigned", "alice", 1)
MR_COMMENT = Trigger(Target(42, "merge_requests", 3), "own_mr_comment", "bob", 2, (104,), "d-aaa")


def mr(source_project_id: int = 42, source_branch: str = BRANCH) -> dict:
    return {
        "id": 1000,
        "iid": 3,
        "project_id": 42,
        "source_project_id": source_project_id,
        "source_branch": source_branch,
        "title": "Resolve #7: Fix the config loader",
        "description": "Closes #7",
        "author": {"id": BOT.id},
    }


def config(tmp_path: Path) -> Config:
    return Config(
        gitlab_url="https://gitlab.example",
        gitlab_token="token",
        claude_env={"ANTHROPIC_API_KEY": "key"},
        allowed_users=ALLOWED,
        agent_image="agent:test",
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
    )


def agent(result: str = "Rewrote the loader.", subtype: str | None = "success", is_error: bool = False) -> AgentResult:
    return AgentResult(result, is_error, subtype, 0, "", 12.0, None, None)


class FakeRunner:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        result: AgentResult | None = None,
        commits: tuple[str, ...] = ("Rewrite the loader",),
        leftovers: bool = False,
        push_error: Exception | None = None,
    ):
        self.result = result or agent()
        self.commits = commits
        self.leftovers = leftovers
        self.push_error = push_error
        self.cloned: list[tuple[int, str]] = []
        self.created: list[str] = []
        self.prompts: list[str] = []
        self.messages: list[str] = []
        self.pushed: list[str] = []
        for name in ("clone", "create_branch", "run_agent", "harvest", "commit_leftovers", "push", "diff_stat"):
            monkeypatch.setattr(runner, name, getattr(self, name))

    def clone(self, cfg: Config, project: Project, branch: str, dest: Path, bot: object, default: str) -> Repo:
        self.cloned.append((project.id, branch))
        return Repo(path=dest, base_ref=f"origin/{branch}")

    def create_branch(self, repo: Repo, name: str) -> None:
        self.created.append(name)

    def run_agent(self, cfg: Config, jobdir: Path, system: str, user: str, bot: object, name: str) -> AgentResult:
        self.prompts.append(user)
        return self.result

    def harvest(self, repo: Repo) -> Changes:
        return Changes(commits=self.commits, dirty=self.leftovers)

    def commit_leftovers(self, repo: Repo, message: str) -> bool:
        self.messages.append(message)
        return self.leftovers

    def push(self, cfg: Config, repo: Repo, branch: str) -> None:
        if self.push_error:
            raise self.push_error
        self.pushed.append(branch)

    def diff_stat(self, repo: Repo) -> str:
        return " src/config.py | 4 ++--\n 1 file changed\n"


def issue_gl(*mrs: dict) -> FakeGitLab:
    return FakeGitLab(mrs=list(mrs), projects={42: PROJECT}, issues={(42, 7): ISSUE})


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Fix the config loader", "fix-the-config-loader"),
        ("  Spaces, commas & Caps!  ", "spaces-commas-caps"),
        ("---weird---", "weird"),
        ("日本語", ""),
        ("a" * 60, "a" * 40),
        ("x" * 39 + " y", "x" * 39),
    ],
)
def test_slug(title: str, expected: str) -> None:
    assert jobs.slug(title) == expected


def test_branch_name_drops_the_dash_when_the_slug_is_empty() -> None:
    assert jobs.branch_name(7, "Fix the config loader") == BRANCH
    assert jobs.branch_name(7, "日本語") == "bot/issue-7"


def test_find_existing_mr_matches_the_issue_not_its_prefix() -> None:
    gl = issue_gl(mr(source_branch="bot/issue-70-other"), mr(source_branch="bot/issue-7"))
    assert jobs.find_existing_mr(gl, 42, BOT.id, 7)["source_branch"] == "bot/issue-7"
    assert jobs.find_existing_mr(gl, 42, BOT.id, 8) is None


def test_assigned_issue_opens_a_merge_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gl, fake = issue_gl(), FakeRunner(monkeypatch)

    assert jobs.run_job(gl, config(tmp_path), BOT, ASSIGNED, "job-1")

    assert fake.cloned == [(42, "main")]
    assert fake.created == [BRANCH]
    assert fake.pushed == [BRANCH]
    (created,) = gl.created_mrs
    assert (created["source_branch"], created["target_branch"]) == (BRANCH, "main")
    assert created["title"] == "Resolve #7: Fix the config loader"
    assert created["description"].endswith("Closes #7")
    assert created["assignee_ids"] == [1]
    assert gl.notes == [("comment", 42, "issues", 7, None, "Opened !50: Rewrote the loader.")]


def test_assigned_issue_without_changes_only_replies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gl, fake = issue_gl(), FakeRunner(monkeypatch, commits=())

    assert jobs.run_job(gl, config(tmp_path), BOT, ASSIGNED, "job-1")

    assert fake.pushed == []
    assert gl.created_mrs == []
    assert gl.notes == [("comment", 42, "issues", 7, None, "Rewrote the loader.")]


def test_an_empty_result_still_says_something(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gl = issue_gl()
    FakeRunner(monkeypatch, result=agent(result=""), commits=())

    jobs.run_job(gl, config(tmp_path), BOT, ASSIGNED, "job-1")

    assert gl.notes[0][-1] == "I made no changes."


def test_a_dirty_tree_alone_counts_as_a_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gl, fake = issue_gl(), FakeRunner(monkeypatch, commits=(), leftovers=True)

    jobs.run_job(gl, config(tmp_path), BOT, ASSIGNED, "job-1")

    assert fake.messages == ["Resolve #7: Fix the config loader"]
    assert fake.pushed == [BRANCH]
    assert len(gl.created_mrs) == 1


def test_an_issue_with_an_open_bot_mr_continues_on_its_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gl, fake = issue_gl(mr(source_branch="bot/issue-7-older-slug")), FakeRunner(monkeypatch)

    assert jobs.run_job(gl, config(tmp_path), BOT, ASSIGNED, "job-1")

    assert fake.cloned == [(42, "bot/issue-7-older-slug")]
    assert fake.created == []
    assert fake.pushed == ["bot/issue-7-older-slug"]
    assert gl.created_mrs == []
    assert gl.notes[0][-1].startswith("Updated !3.")


def test_own_mr_comment_pushes_and_replies_in_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gl = FakeGitLab(mrs=[mr()], projects={42: PROJECT})
    fake = FakeRunner(monkeypatch)

    assert jobs.run_job(gl, config(tmp_path), BOT, MR_COMMENT, "job-1")

    assert fake.cloned == [(42, BRANCH)]
    assert fake.created == []
    assert fake.messages == ["Apply review feedback (bot)"]
    assert fake.pushed == [BRANCH]
    assert gl.created_mrs == []
    kind, project_id, _, iid, discussion_id, body = gl.notes[0]
    assert (kind, project_id, iid, discussion_id) == ("reply", 42, 3, "d-aaa")
    assert body.startswith("Rewrote the loader.")
    assert "```\nsrc/config.py | 4 ++--\n 1 file changed\n```" in body


def test_a_fork_merge_request_is_cloned_but_never_pushed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gl = FakeGitLab(mrs=[mr(source_project_id=77, source_branch="patch-1")], projects={42: PROJECT, 77: FORK})
    fake = FakeRunner(monkeypatch)

    assert jobs.run_job(gl, config(tmp_path), BOT, MR_COMMENT, "job-1")

    assert fake.cloned == [(77, "patch-1")]
    assert fake.pushed == []
    body = gl.notes[0][-1]
    assert "fork" in body
    assert "1 file changed" in body


def test_an_agent_error_replies_and_pushes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gl = issue_gl()
    fake = FakeRunner(monkeypatch, result=AgentResult("", True, "timeout", -1, "secret stderr", 1800.0, None, None))

    assert not jobs.run_job(gl, config(tmp_path), BOT, ASSIGNED, "job-1")

    assert fake.pushed == []
    assert gl.created_mrs == []
    body = gl.notes[0][-1]
    assert body == "I couldn't complete this: the agent timed out after 1800s."


def test_a_failed_agent_run_names_its_subtype(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gl = issue_gl()
    FakeRunner(monkeypatch, result=AgentResult("junk", True, "bad_output", 1, "secret stderr", 1.0, None, None))

    jobs.run_job(gl, config(tmp_path), BOT, ASSIGNED, "job-1")

    assert gl.notes[0][-1] == "I couldn't complete this: the agent run failed (bad_output)."


def test_a_failing_push_posts_one_comment_and_returns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gl = issue_gl()
    FakeRunner(monkeypatch, push_error=JobError("git push failed (1): protected branch"))

    assert not jobs.run_job(gl, config(tmp_path), BOT, ASSIGNED, "job-1")

    assert gl.created_mrs == []
    assert gl.notes == [
        ("comment", 42, "issues", 7, None, "I couldn't complete this: JobError. Check the bot's log for job job-1.")
    ]


def test_the_job_directory_is_removed_even_when_the_job_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeRunner(monkeypatch, push_error=JobError("boom"))
    cfg = config(tmp_path)

    jobs.run_job(issue_gl(), cfg, BOT, ASSIGNED, "job-1")

    assert not (cfg.work_dir / "job-1").exists()


def test_job_name_is_shell_safe() -> None:
    name = jobs.job_name(MR_COMMENT)
    assert name.startswith("42-me3-")
    assert name.replace("-", "").isalnum()
