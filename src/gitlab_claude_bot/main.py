import logging
import os
import signal
import subprocess
import threading
from datetime import UTC, datetime

import httpx

from . import feeds, jobs
from .config import Config, ConfigError
from .gitlab import GitLab, GitLabError, User
from .health import Status, serve
from .runner import askpass_script, container_name
from .state import State
from .triggers import acknowledge

log = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _config() -> Config:
    try:
        return Config.from_env(os.environ)
    except ConfigError as error:
        for problem in str(error).splitlines():
            log.error("configuration: %s", problem)
        raise SystemExit(2) from error


def _identify(gl: GitLab) -> User:
    try:
        return gl.me()
    except (GitLabError, httpx.HTTPError) as error:
        log.error("cannot identify the bot account: %s", error)
        raise SystemExit(2) from error


def _ensure_agent_image(image: str) -> None:
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
        return
    log.info("pulling agent image %s", image)
    pull = subprocess.run(["docker", "pull", image], capture_output=True, text=True, errors="replace")
    if pull.returncode:
        log.error("cannot pull agent image %s: %s", image, pull.stderr.strip()[-500:])
        raise SystemExit(2)


def poll_once(gl: GitLab, cfg: Config, bot: User, state: State, status: Status, now: datetime) -> int:
    ran = 0
    try:
        todo, ignored = feeds.todo_triggers(gl, bot, cfg.allowed_users)
        for todo_id in ignored:
            gl.mark_todo_done(todo_id)
        for trigger in feeds.merge(todo, feeds.own_mr_triggers(gl, bot, state, cfg.allowed_users, now)):
            if not acknowledge(gl, bot.id, trigger):
                continue
            status.job_running = jobs.job_name(trigger)
            failed = not jobs.run_job(gl, cfg, bot, trigger, status.job_running)
            status.job_running = None
            status.jobs_done += 1
            status.jobs_failed += failed
            ran += 1
        state.save(cfg.state_dir / "state.json")
    except (GitLabError, httpx.HTTPError) as error:
        log.error("poll failed: %s", error)
        status.last_poll_error = str(error)
        return 0
    status.last_poll_ok = now
    status.last_poll_error = None
    return ran


def run() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format=LOG_FORMAT)
    cfg = _config()
    gl = GitLab(cfg.gitlab_url, cfg.gitlab_token)
    bot = _identify(gl)
    log.info("acting as @%s (id %d) on %s", bot.username, bot.id, cfg.gitlab_url)

    askpass_script(cfg.state_dir)
    state = State.load(cfg.state_dir / "state.json")
    _ensure_agent_image(cfg.agent_image)

    status = Status(cfg.poll_interval, cfg.job_timeout, bot_username=bot.username)
    serve(cfg.health_port, status)

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    try:
        while not stop.is_set():
            poll_once(gl, cfg, bot, state, status, datetime.now(UTC))
            stop.wait(cfg.poll_interval)
    finally:
        # the job's container is a sibling of this process, so nothing else reaps it
        if status.job_running:
            subprocess.run(["docker", "rm", "-f", container_name(status.job_running)], capture_output=True, check=False)
    log.info("stopped after %d job(s), %d of them failed", status.jobs_done, status.jobs_failed)
