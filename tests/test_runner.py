import json
import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from gitlab_claude_bot.config import Config
from gitlab_claude_bot.gitlab import Project, User
from gitlab_claude_bot.runner import (
    Changes,
    JobDir,
    JobError,
    Repo,
    askpass_script,
    clone,
    clone_url,
    commit_leftovers,
    create_branch,
    diff_stat,
    docker_command,
    harvest,
    push,
    run_agent,
)

BOT = User(id=7, username="gcb", name="GCB Bot", email="bot@example.com")
HTTPS_URL = "https://gitlab.example.com/group/proj.git"


def config(tmp_path: Path, **overrides: object) -> Config:
    fields: dict = dict(
        gitlab_url="https://gitlab.example.com",
        gitlab_token="glpat-secret",
        claude_env={"CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret"},
        allowed_users=frozenset({"alice"}),
        agent_image="ghcr.io/x/agent:1",
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
        clone_depth=5,
    )
    return Config(**(fields | overrides))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Setup\n\temail = setup@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "README.md").write_text("one\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "first")
    (src / "README.md").write_text("one\ntwo\n")
    git(src, "commit", "-q", "-am", "second")
    git(src, "checkout", "-q", "-b", "feature")
    (src / "feature.txt").write_text("f\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "feature work")
    git(src, "checkout", "-q", "main")
    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(bare))
    return bare


@pytest.fixture(params=["file", "https"])
def project(request: pytest.FixtureRequest, bare: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    url = bare.as_uri()
    if request.param == "https":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{bare.as_uri()}.insteadOf")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", clone_url(HTTPS_URL))
        url = HTTPS_URL
    return Project(id=1, path_with_namespace="group/proj", default_branch="main", http_url_to_repo=url)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return config(tmp_path)


@pytest.fixture
def repo(cfg: Config, project: Project, tmp_path: Path) -> Repo:
    return clone(cfg, project, "main", tmp_path / "work" / "job", BOT, "main")


def test_clone_url_adds_oauth2_user_to_http_only() -> None:
    assert clone_url(HTTPS_URL) == "https://oauth2@gitlab.example.com/group/proj.git"
    assert clone_url("http://git.local:8080/a/b.git") == "http://oauth2@git.local:8080/a/b.git"
    assert clone_url("file:///srv/x.git") == "file:///srv/x.git"


def test_clone_checks_out_branch_and_configures_repo(repo: Repo, project: Project) -> None:
    assert repo.base_ref == "origin/main"
    assert git(repo.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert (repo.path / "README.md").read_text() == "one\ntwo\n"
    assert git(repo.path, "config", "user.name").strip() == "GCB Bot"
    assert git(repo.path, "config", "user.email").strip() == "bot@example.com"
    assert ".gcb/" in (repo.path / ".git" / "info" / "exclude").read_text().splitlines()
    remote = git(repo.path, "config", "remote.origin.url").strip()
    if project.http_url_to_repo.startswith("https://"):
        assert remote == "https://oauth2@gitlab.example.com/group/proj.git"
    assert "glpat" not in (repo.path / ".git" / "config").read_text()


def test_clone_never_writes_token_into_tree(repo: Repo) -> None:
    for path in repo.path.rglob("*"):
        if path.is_file():
            assert b"glpat-secret" not in path.read_bytes(), path


def test_clone_of_non_default_branch_fetches_default(cfg: Config, project: Project, tmp_path: Path) -> None:
    repo = clone(cfg, project, "feature", tmp_path / "work" / "job", BOT, "main")
    assert repo.base_ref == "origin/feature"
    assert (repo.path / "feature.txt").exists()
    assert git(repo.path, "rev-parse", "--verify", "origin/main")
    assert git(repo.path, "log", "--format=%s", "origin/main..HEAD").strip() == "feature work"


def test_clone_of_missing_branch_raises(cfg: Config, project: Project, tmp_path: Path) -> None:
    with pytest.raises(JobError, match="clone"):
        clone(cfg, project, "nope", tmp_path / "work" / "job", BOT, "main")


def test_noreply_email_when_bot_has_none(cfg: Config, project: Project, tmp_path: Path) -> None:
    bot = User(id=7, username="gcb", name="GCB Bot", email=None)
    repo = clone(cfg, project, "main", tmp_path / "work" / "job", bot, "main")
    assert git(repo.path, "config", "user.email").strip() == "gcb@users.noreply.gitlab.com"


def test_create_branch(repo: Repo) -> None:
    create_branch(repo, "gcb/issue-1")
    assert git(repo.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "gcb/issue-1"


def test_harvest_clean_clone(repo: Repo) -> None:
    assert harvest(repo) == Changes((), False)


def test_harvest_sees_commits_and_dirty_files(repo: Repo) -> None:
    (repo.path / "a.txt").write_text("a\n")
    git(repo.path, "add", "-A")
    git(repo.path, "commit", "-q", "-m", "add a")
    (repo.path / "b.txt").write_text("b\n")
    git(repo.path, "add", "-A")
    git(repo.path, "commit", "-q", "-m", "add b")
    (repo.path / "untracked.txt").write_text("u\n")
    changes = harvest(repo)
    assert changes.commits == ("add a", "add b")
    assert changes.dirty


def test_harvest_ignores_gcb_dir(repo: Repo) -> None:
    (repo.path / ".gcb").mkdir()
    (repo.path / ".gcb" / "prompt.md").write_text("x")
    assert not harvest(repo).dirty


def test_commit_leftovers_returns_false_when_clean(repo: Repo) -> None:
    assert commit_leftovers(repo, "leftovers") is False
    assert harvest(repo).commits == ()


def test_commit_leftovers_commits_with_bot_identity(repo: Repo) -> None:
    (repo.path / "new.txt").write_text("n\n")
    (repo.path / "README.md").write_text("changed\n")
    assert commit_leftovers(repo, "Uncommitted agent changes") is True
    assert harvest(repo) == Changes(("Uncommitted agent changes",), False)
    assert git(repo.path, "log", "-1", "--format=%an <%ae>").strip() == "GCB Bot <bot@example.com>"
    assert git(repo.path, "log", "-1", "--format=%cn <%ce>").strip() == "GCB Bot <bot@example.com>"


def test_diff_stat(repo: Repo) -> None:
    (repo.path / "new.txt").write_text("n\n")
    commit_leftovers(repo, "add new")
    assert "new.txt" in diff_stat(repo)
    assert "1 file changed" in diff_stat(repo)


def test_push_lands_branch_in_remote(cfg: Config, repo: Repo, bare: Path) -> None:
    create_branch(repo, "gcb/work")
    (repo.path / "pushed.txt").write_text("p\n")
    commit_leftovers(repo, "pushed commit")
    push(cfg, repo, "gcb/work")
    assert git(bare, "log", "-1", "--format=%s", "gcb/work").strip() == "pushed commit"
    assert git(repo.path, "config", "branch.gcb/work.merge").strip() == "refs/heads/gcb/work"


def test_push_is_not_forced(cfg: Config, repo: Repo, bare: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", bare.as_uri(), str(other))
    (other / "race.txt").write_text("r\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "someone else")
    git(other, "push", "-q", "origin", "main")
    (repo.path / "mine.txt").write_text("m\n")
    commit_leftovers(repo, "mine")
    with pytest.raises(JobError, match="push"):
        push(cfg, repo, "main")
    assert git(bare, "log", "-1", "--format=%s", "main").strip() == "someone else"


def test_jobdir_creates_private_dir_and_removes_it(tmp_path: Path) -> None:
    work = tmp_path / "work"
    with JobDir(work, "job-1") as path:
        assert path == work / "job-1"
        assert path.is_dir()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        (path / "file").write_text("x")
    assert not path.exists()


def test_jobdir_removes_read_only_subdir_on_exception(tmp_path: Path) -> None:
    job = JobDir(tmp_path / "work", "job-2")
    with pytest.raises(RuntimeError):
        with job as path:
            locked = path / "locked"
            locked.mkdir()
            (locked / "file").write_text("x")
            locked.chmod(0o500)
            raise RuntimeError("boom")
    assert not job.path.exists()


def test_jobdir_replaces_stale_dir(tmp_path: Path) -> None:
    stale = tmp_path / "work" / "job-3"
    stale.mkdir(parents=True)
    (stale / "old").write_text("x")
    with JobDir(tmp_path / "work", "job-3") as path:
        assert list(path.iterdir()) == []


def test_askpass_script_prints_token_from_env(tmp_path: Path) -> None:
    script = askpass_script(tmp_path / "state")
    assert script == tmp_path / "state" / "bin" / "askpass"
    assert stat.S_IMODE(script.stat().st_mode) == 0o700
    assert stat.S_IMODE(script.parent.stat().st_mode) == 0o700
    out = subprocess.run([str(script), "Password for x:"], capture_output=True, text=True, env={"GITLAB_TOKEN": "tok"})
    assert out.stdout == "tok\n"
    assert askpass_script(tmp_path / "state") == script


DOCKER_PREFIX = [
    "docker", "run", "--rm", "--name", "gcb-job-9",
    "--user", "1000:1000", "--read-only",
    "--tmpfs", "/home/agent:rw,uid=1000,gid=1000,size=512m",
    "--tmpfs", "/tmp:rw,size=1g",
    "--mount", "type=bind,src=/var/lib/bot/work/job-9,dst=/work", "--workdir", "/work",
    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
    "--memory", "3g", "--memory-swap", "3g", "--cpus", "2", "--pids-limit", "512",
    "-e", "CLAUDE_CODE_OAUTH_TOKEN",
    "-e", "HOME=/home/agent", "-e", "DISABLE_AUTOUPDATER=1", "-e", "DISABLE_TELEMETRY=1", "-e", "CI=1",
    "-e", "GIT_AUTHOR_NAME=GCB Bot", "-e", "GIT_AUTHOR_EMAIL=bot@example.com",
    "-e", "GIT_COMMITTER_NAME=GCB Bot", "-e", "GIT_COMMITTER_EMAIL=bot@example.com",
]  # fmt: skip
CLAUDE_ARGS = [
    "claude", "-p", "--output-format", "json", "--dangerously-skip-permissions", "--no-session-persistence",
    "--append-system-prompt", "be brief",
]  # fmt: skip


def test_docker_command_full_config(tmp_path: Path) -> None:
    cfg = config(tmp_path, claude_model="claude-fable-5-1", max_budget_usd=2.5)
    argv = docker_command(cfg, Path("/var/lib/bot/work/job-9"), "be brief", BOT, "job-9")
    assert argv == [
        *DOCKER_PREFIX, "-e", "ANTHROPIC_MODEL=claude-fable-5-1", "ghcr.io/x/agent:1",
        *CLAUDE_ARGS, "--max-budget-usd", "2.5",
    ]  # fmt: skip


def test_docker_command_without_model_or_budget(tmp_path: Path) -> None:
    argv = docker_command(config(tmp_path), Path("/var/lib/bot/work/job-9"), "be brief", BOT, "job-9")
    assert argv == [*DOCKER_PREFIX, "ghcr.io/x/agent:1", *CLAUDE_ARGS]


def test_docker_command_carries_no_secret_values(tmp_path: Path) -> None:
    cfg = config(tmp_path, claude_env={"ANTHROPIC_API_KEY": "sk-secret"})
    argv = docker_command(cfg, Path("/w"), "sys", BOT, "j")
    assert "sk-secret" not in " ".join(argv)
    assert "glpat-secret" not in " ".join(argv)
    assert argv[argv.index("ANTHROPIC_API_KEY") - 1] == "-e"


Stub = Callable[[str], Path]


@pytest.fixture
def docker_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stub:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "record"
    record.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def install(body: str) -> Path:
        script = bin_dir / "docker"
        script.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = rm ]; then echo "$@" > "{record}/rm"; exit 0; fi\n'
            f'env > "{record}/env"\n'
            f'cat > "{record}/stdin"\n'
            f'printf "%s\\n" "$@" > "{record}/argv"\n'
            f"{body}\n"
        )
        script.chmod(0o700)
        return record

    return install


SUCCESS = {"type": "result", "subtype": "success", "is_error": False, "result": "done it", "total_cost_usd": 0.42}


def run(cfg: Config, jobdir: Path, prompt: str = "do the thing") -> object:
    jobdir.mkdir(parents=True, exist_ok=True)
    return run_agent(cfg, jobdir, "system", prompt, BOT, "job-1")


def test_run_agent_success(docker_stub: Stub, tmp_path: Path) -> None:
    record = docker_stub(f"echo '{json.dumps(SUCCESS)}'")
    cfg = config(tmp_path)
    result = run(cfg, tmp_path / "work" / "job-1")
    assert result.result == "done it"
    assert result.is_error is False
    assert result.subtype == "success"
    assert result.exit_code == 0
    assert result.cost_usd == 0.42
    assert result.raw == SUCCESS
    assert result.duration_s >= 0
    assert (record / "stdin").read_text() == "do the thing"
    assert (record / "argv").read_text().splitlines() == docker_command(cfg, tmp_path / "work" / "job-1", "system", BOT, "job-1")[1:]


def test_run_agent_error_result(docker_stub: Stub, tmp_path: Path) -> None:
    payload = {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "gave up"}
    docker_stub(f"echo '{json.dumps(payload)}'; echo 'warn: x' >&2; exit 1")
    result = run(config(tmp_path), tmp_path / "work" / "job-1")
    assert result.is_error is True
    assert result.subtype == "error_max_turns"
    assert result.result == "gave up"
    assert result.exit_code == 1
    assert result.stderr_tail == "warn: x\n"
    assert result.cost_usd is None


def test_run_agent_bad_output(docker_stub: Stub, tmp_path: Path) -> None:
    docker_stub("echo 'not json at all'")
    result = run(config(tmp_path), tmp_path / "work" / "job-1")
    assert result.is_error is True
    assert result.subtype == "bad_output"
    assert result.result == "not json at all\n"
    assert result.raw is None


def test_run_agent_timeout_removes_container(docker_stub: Stub, tmp_path: Path) -> None:
    record = docker_stub("exec sleep 30")
    result = run(config(tmp_path, job_timeout=1), tmp_path / "work" / "job-1")
    assert result.is_error is True
    assert result.subtype == "timeout"
    assert (record / "rm").read_text().strip() == "rm -f gcb-job-1"
    assert 1 <= result.duration_s < 10


def test_run_agent_cleans_gcb_dir(docker_stub: Stub, tmp_path: Path) -> None:
    docker_stub(f"echo '{json.dumps(SUCCESS)}'")
    jobdir = tmp_path / "work" / "job-1"
    run(config(tmp_path), jobdir)
    assert not (jobdir / ".gcb").exists()
    assert jobdir.is_dir()


def test_run_agent_env_is_minimal(docker_stub: Stub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-secret")
    monkeypatch.setenv("SOME_DAEMON_SECRET", "leak")
    record = docker_stub(f"echo '{json.dumps(SUCCESS)}'")
    run(config(tmp_path), tmp_path / "work" / "job-1")
    env = dict(line.split("=", 1) for line in (record / "env").read_text().splitlines())
    shell_added = {"PWD", "OLDPWD", "SHLVL", "_"}
    assert set(env) - shell_added == {"PATH", "HOME", "CLAUDE_CODE_OAUTH_TOKEN"}
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-secret"
    assert "GITLAB_TOKEN" not in env
    assert "SOME_DAEMON_SECRET" not in env
