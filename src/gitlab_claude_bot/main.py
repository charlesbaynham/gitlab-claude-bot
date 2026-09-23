import logging
import os
import signal
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import feeds, github_feeds, jobs
from .config import Config, ConfigError
from .forge import Forge, ForgeError, User
from .github import GitHub
from .gitlab import GitLab
from .health import Status, serve
from .runner import askpass_script, container_name
from .state import State
from .triggers import Trigger, acknowledge

log = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

Inbox = Callable[[Forge, User, frozenset[str]], tuple[list[Trigger], list[int]]]


@dataclass
class Account:
    forge: Forge
    inbox: Inbox
    allowed: frozenset[str]
    bot: User
    state: State
    state_path: Path

    @property
    def name(self) -> str:
        return self.forge.labels.name


def _config() -> Config:
    try:
        return Config.from_env(os.environ)
    except ConfigError as error:
        for problem in str(error).splitlines():
            log.error("configuration: %s", problem)
        raise SystemExit(2) from error


def _identify(forge: Forge) -> User:
    try:
        return forge.me()
    except (ForgeError, httpx.HTTPError) as error:
        log.error("cannot identify the %s bot account: %s", forge.labels.name, error)
        raise SystemExit(2) from error


def _accounts(cfg: Config) -> list[Account]:
    # GitLab keeps the original state file name, so upgrading does not re-baseline its MRs
    wanted: list[tuple[Forge, Inbox, frozenset[str], str]] = []
    if cfg.gitlab_token:
        gitlab = GitLab(cfg.gitlab_url, cfg.gitlab_token)
        wanted.append((gitlab, feeds.todo_triggers, cfg.gitlab_allowed_users, "state.json"))
    if cfg.github_token:
        github = GitHub(cfg.github_url, cfg.github_token)
        wanted.append((github, github_feeds.notification_triggers, cfg.github_allowed_users, "state-github.json"))

    accounts = []
    for forge, inbox, allowed, state_file in wanted:
        bot = _identify(forge)
        log.info("acting as @%s (id %d) on %s", bot.username, bot.id, forge.labels.name)
        path = cfg.state_dir / state_file
        accounts.append(Account(forge, inbox, allowed, bot, State.load(path), path))
    return accounts


def _ensure_agent_image(image: str) -> None:
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
        return
    log.info("pulling agent image %s", image)
    pull = subprocess.run(["docker", "pull", image], capture_output=True, text=True, errors="replace")
    if pull.returncode:
        log.error("cannot pull agent image %s: %s", image, pull.stderr.strip()[-500:])
        raise SystemExit(2)


def poll_account(account: Account, cfg: Config, status: Status, now: datetime) -> int:
    forge, bot, state = account.forge, account.bot, account.state
    ran = 0
    todo, ignored = account.inbox(forge, bot, account.allowed)
    for todo_id in ignored:
        forge.mark_todo_done(todo_id)
    for trigger in feeds.merge(todo, feeds.own_mr_triggers(forge, bot, state, account.allowed, now)):
        if not acknowledge(forge, bot.id, trigger):
            continue
        status.job_running = jobs.job_name(trigger)
        failed = not jobs.run_job(forge, cfg, bot, trigger, status.job_running)
        status.job_running = None
        status.jobs_done += 1
        status.jobs_failed += failed
        ran += 1
    state.save(account.state_path)
    return ran


def poll_once(accounts: list[Account], cfg: Config, status: Status, now: datetime) -> int:
    ran = 0
    errors = []
    for account in accounts:
        try:
            ran += poll_account(account, cfg, status, now)
        except (ForgeError, httpx.HTTPError) as error:
            log.error("%s poll failed: %s", account.name, error)
            errors.append(f"{account.name}: {error}")
    if errors:
        status.last_poll_error = "; ".join(errors)
        return ran
    status.last_poll_ok = now
    status.last_poll_error = None
    return ran


def run() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format=LOG_FORMAT)
    cfg = _config()
    accounts = _accounts(cfg)

    askpass_script(cfg.state_dir)
    _ensure_agent_image(cfg.agent_image)

    bots = ", ".join(f"{a.name} @{a.bot.username}" for a in accounts)
    status = Status(cfg.poll_interval, cfg.job_timeout, bot_username=bots)
    serve(cfg.health_port, status)

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    try:
        while not stop.is_set():
            poll_once(accounts, cfg, status, datetime.now(UTC))
            stop.wait(cfg.poll_interval)
    finally:
        # the job's container is a sibling of this process, so nothing else reaps it
        if status.job_running:
            subprocess.run(["docker", "rm", "-f", container_name(status.job_running)], capture_output=True, check=False)
    log.info("stopped after %d job(s), %d of them failed", status.jobs_done, status.jobs_failed)
