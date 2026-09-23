from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


CLAUDE_CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


@dataclass(frozen=True)
class Config:
    gitlab_url: str
    gitlab_token: str
    claude_env: dict[str, str]
    gitlab_allowed_users: frozenset[str]
    agent_image: str
    state_dir: Path
    work_dir: Path
    github_url: str = "https://api.github.com"
    github_token: str = ""
    github_allowed_users: frozenset[str] = frozenset()
    poll_interval: int = 30
    job_timeout: int = 1800
    max_budget_usd: float | None = None
    claude_model: str | None = None
    health_port: int = 8000
    clone_depth: int = 50

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        problems: list[str] = []

        def value(name: str) -> str:
            raw = env.get(name, "").strip()
            if raw == "CHANGEME":
                problems.append(f"{name} is still CHANGEME")
                return ""
            return raw

        def required(name: str) -> str:
            if not env.get(name, "").strip():
                problems.append(f"{name} is required")
            return value(name)

        def number(name: str, default: int) -> int:
            raw = value(name)
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                problems.append(f"{name} must be an integer, got {raw!r}")
                return default

        def given(*names: str) -> bool:
            return any(env.get(n, "").strip() for n in names)

        def users(name: str) -> frozenset[str]:
            raw = value(name)
            names = frozenset(u.strip().lower() for u in raw.split(",") if u.strip())
            if raw and not names:
                problems.append(f"{name} has no usernames")
            return names

        gitlab_url = (value("GITLAB_URL") or "https://gitlab.com").rstrip("/")
        gitlab_token = value("GITLAB_TOKEN")
        github_url = (value("GITHUB_URL") or "https://api.github.com").rstrip("/")
        github_token = value("GITHUB_TOKEN")
        if not given("GITLAB_TOKEN", "GITHUB_TOKEN"):
            problems.append("GITLAB_TOKEN or GITHUB_TOKEN is required")

        claude_env = {n: v for n in CLAUDE_CREDENTIALS if (v := value(n))}
        if len(claude_env) != 1:
            problems.append(f"exactly one of {' / '.join(CLAUDE_CREDENTIALS)} is required")

        shared_users = users("ALLOWED_USERS")
        gitlab_allowed_users = users("GITLAB_ALLOWED_USERS") or shared_users
        github_allowed_users = users("GITHUB_ALLOWED_USERS") or shared_users
        for token, forge in (("GITLAB_TOKEN", "GITLAB"), ("GITHUB_TOKEN", "GITHUB")):
            if given(token) and not given("ALLOWED_USERS", f"{forge}_ALLOWED_USERS"):
                problems.append(f"ALLOWED_USERS or {forge}_ALLOWED_USERS is required")

        agent_image = required("AGENT_IMAGE")
        state_dir = Path(required("STATE_DIR"))
        work_dir = Path(required("WORK_DIR"))
        # docker cannot see a systemd unit's PrivateTmp, so bind mounts from there are empty
        if work_dir.is_relative_to("/tmp"):
            problems.append("WORK_DIR must not be under /tmp")

        max_budget_raw = value("MAX_BUDGET_USD")
        max_budget_usd: float | None = None
        if max_budget_raw:
            try:
                max_budget_usd = float(max_budget_raw)
            except ValueError:
                problems.append(f"MAX_BUDGET_USD must be a number, got {max_budget_raw!r}")

        config = cls(
            gitlab_url=gitlab_url,
            gitlab_token=gitlab_token,
            claude_env=claude_env,
            gitlab_allowed_users=gitlab_allowed_users,
            agent_image=agent_image,
            state_dir=state_dir,
            work_dir=work_dir,
            github_url=github_url,
            github_token=github_token,
            github_allowed_users=github_allowed_users,
            poll_interval=number("POLL_INTERVAL", 30),
            job_timeout=number("JOB_TIMEOUT", 1800),
            max_budget_usd=max_budget_usd,
            claude_model=value("CLAUDE_MODEL") or None,
            health_port=number("HEALTH_PORT", 8000),
            clone_depth=number("CLONE_DEPTH", 50),
        )
        if problems:
            raise ConfigError("\n".join(problems))
        return config
