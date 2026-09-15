import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import Config
from .gitlab import Project, User

log = logging.getLogger(__name__)

ASKPASS = "#!/bin/sh\nprintf '%s\\n' \"$GITLAB_TOKEN\"\n"
STDERR_TAIL = 4096
BAD_OUTPUT_HEAD = 2048


class JobError(Exception):
    pass


def container_name(job_name: str) -> str:
    return f"gcb-{job_name}"


def _unlock_and_retry(func: Callable[..., Any], path: str, exc: BaseException) -> None:
    os.chmod(os.path.dirname(path), 0o700)
    if os.path.isdir(path) and not os.path.islink(path):
        os.chmod(path, 0o700)
        shutil.rmtree(path, onexc=_unlock_and_retry)
    else:
        os.unlink(path)


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, onexc=_unlock_and_retry)


class JobDir:
    def __init__(self, work_dir: Path, name: str):
        self.path = work_dir / name

    def __enter__(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            remove_tree(self.path)
        self.path.mkdir(mode=0o700)
        return self.path

    def __exit__(self, *exc: object) -> None:
        if self.path.exists():
            remove_tree(self.path)


@dataclass(frozen=True)
class Repo:
    path: Path
    base_ref: str


def askpass_script(state_dir: Path) -> Path:
    bin_dir = state_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    script = bin_dir / "askpass"
    if not script.exists() or script.read_text() != ASKPASS:
        script.write_text(ASKPASS)
    script.chmod(0o700)
    return script


def _auth_env(cfg: Config) -> dict[str, str]:
    return {
        "GIT_ASKPASS": str(askpass_script(cfg.state_dir)),
        "GIT_TERMINAL_PROMPT": "0",
        "GITLAB_TOKEN": cfg.gitlab_token,
    }


def _git(
    where: Repo | Path,
    *args: str,
    env_extra: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    path = where.path if isinstance(where, Repo) else where
    proc = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        errors="replace",
        stdin=subprocess.DEVNULL,
        env={**os.environ, **(env_extra or {})},
    )
    if check and proc.returncode:
        raise JobError(f"git {args[0]} failed ({proc.returncode}): {proc.stderr.strip()[-2000:]}")
    return proc


def clone_url(http_url_to_repo: str) -> str:
    parts = urlsplit(http_url_to_repo)
    if parts.scheme not in ("http", "https"):
        return http_url_to_repo
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit(parts._replace(netloc=f"oauth2@{host}"))


def bot_email(bot: User) -> str:
    return bot.email or f"{bot.username}@users.noreply.gitlab.com"


def clone(cfg: Config, project: Project, branch: str, dest: Path, bot: User, default_branch: str) -> Repo:
    auth = _auth_env(cfg)
    depth = str(cfg.clone_depth)
    url = clone_url(project.http_url_to_repo)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(dest.parent, "clone", "--depth", depth, "--branch", branch, "--single-branch", url, str(dest), env_extra=auth)
    repo = Repo(path=dest, base_ref=f"origin/{branch}")
    if branch != default_branch:
        # --single-branch narrowed the fetch refspec, so name the tracking ref or it lands in FETCH_HEAD only
        tracking = f"{default_branch}:refs/remotes/origin/{default_branch}"
        _git(repo, "fetch", "--depth", depth, "origin", tracking, env_extra=auth)
    _git(repo, "config", "user.name", bot.name)
    _git(repo, "config", "user.email", bot_email(bot))
    exclude = dest / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    with exclude.open("a") as f:
        f.write(".gcb/\n")
    return repo


def create_branch(repo: Repo, name: str) -> None:
    _git(repo, "checkout", "-b", name)


@dataclass(frozen=True)
class AgentResult:
    result: str
    is_error: bool
    subtype: str | None
    exit_code: int
    stderr_tail: str
    duration_s: float
    cost_usd: float | None
    raw: dict | None


def docker_command(cfg: Config, jobdir: Path, system_prompt: str, bot: User, job_name: str) -> list[str]:
    email = bot_email(bot)
    env_names = [*cfg.claude_env]
    env_values = {
        "HOME": "/home/agent",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "CI": "1",
        "GIT_AUTHOR_NAME": bot.name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": bot.name,
        "GIT_COMMITTER_EMAIL": email,
    }
    if cfg.claude_model:
        env_values["ANTHROPIC_MODEL"] = cfg.claude_model
    env_args = [arg for name in env_names for arg in ("-e", name)]
    env_args += [arg for name, value in env_values.items() for arg in ("-e", f"{name}={value}")]
    budget = ["--max-budget-usd", str(cfg.max_budget_usd)] if cfg.max_budget_usd is not None else []
    return [
        "docker", "run", "--rm", "--name", container_name(job_name),
        "--user", "1000:1000", "--read-only",
        "--tmpfs", "/home/agent:rw,uid=1000,gid=1000,size=512m",
        "--tmpfs", "/tmp:rw,size=1g",
        "--mount", f"type=bind,src={jobdir},dst=/work", "--workdir", "/work",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", "3g", "--memory-swap", "3g", "--cpus", "2", "--pids-limit", "512",
        *env_args,
        cfg.agent_image,
        "claude", "-p", "--output-format", "json",
        "--dangerously-skip-permissions", "--no-session-persistence",
        "--append-system-prompt", system_prompt,
        *budget,
    ]  # fmt: skip


def _docker_env(cfg: Config) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(Path.home()), **cfg.claude_env}


def _parse_result(stdout: str, exit_code: int, stderr: str, duration: float) -> AgentResult:
    try:
        raw = json.loads(stdout)
        if not isinstance(raw, dict):
            raise ValueError("not an object")
    except ValueError:
        return AgentResult(stdout[:BAD_OUTPUT_HEAD], True, "bad_output", exit_code, stderr, duration, None, None)
    cost = raw.get("total_cost_usd")
    return AgentResult(
        result=raw.get("result") or "",
        is_error=bool(raw.get("is_error")) or exit_code != 0,
        subtype=raw.get("subtype"),
        exit_code=exit_code,
        stderr_tail=stderr,
        duration_s=duration,
        cost_usd=float(cost) if cost is not None else None,
        raw=raw,
    )


def run_agent(cfg: Config, jobdir: Path, system_prompt: str, user_prompt: str, bot: User, job_name: str) -> AgentResult:
    gcb = jobdir / ".gcb"
    gcb.mkdir(exist_ok=True)
    prompt = gcb / "prompt.md"
    prompt.write_text(user_prompt)
    env = _docker_env(cfg)
    argv = docker_command(cfg, jobdir, system_prompt, bot, job_name)
    started = time.monotonic()
    try:
        with prompt.open("rb") as stdin:
            proc = subprocess.run(
                argv, stdin=stdin, capture_output=True, text=True, errors="replace",
                timeout=cfg.job_timeout, env=env,
            )  # fmt: skip
        duration = time.monotonic() - started
        result = _parse_result(proc.stdout, proc.returncode, proc.stderr[-STDERR_TAIL:], duration)
    except subprocess.TimeoutExpired as timeout:
        duration = time.monotonic() - started
        subprocess.run(["docker", "rm", "-f", container_name(job_name)], capture_output=True, env=env, check=False)
        stderr = timeout.stderr or b""
        stderr_text = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
        result = AgentResult("", True, "timeout", -1, stderr_text[-STDERR_TAIL:], duration, None, None)
    finally:
        shutil.rmtree(gcb, ignore_errors=True)
    log.info(
        "agent %s finished: subtype=%s exit=%d duration=%.0fs cost=%s",
        job_name, result.subtype, result.exit_code, result.duration_s, result.cost_usd,
    )  # fmt: skip
    return result


@dataclass(frozen=True)
class Changes:
    commits: tuple[str, ...]
    dirty: bool


def harvest(repo: Repo) -> Changes:
    log_out = _git(repo, "log", "--reverse", "--format=%s", f"{repo.base_ref}..HEAD").stdout
    status = _git(repo, "status", "--porcelain").stdout
    return Changes(commits=tuple(log_out.splitlines()), dirty=bool(status.strip()))


def commit_leftovers(repo: Repo, message: str) -> bool:
    _git(repo, "add", "-A")
    if _git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    _git(repo, "commit", "-m", message)
    return True


def push(cfg: Config, repo: Repo, branch: str) -> None:
    _git(repo, "push", "-u", "origin", branch, env_extra=_auth_env(cfg))


def diff_stat(repo: Repo) -> str:
    return _git(repo, "diff", "--stat", f"{repo.base_ref}..HEAD").stdout
