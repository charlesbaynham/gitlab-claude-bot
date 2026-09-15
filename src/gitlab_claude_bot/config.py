from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


CLAUDE_CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
REQUIRED = ("GITLAB_TOKEN", "ALLOWED_USERS", "AGENT_IMAGE", "STATE_DIR", "WORK_DIR")


@dataclass(frozen=True)
class Config:
    gitlab_url: str
    gitlab_token: str
    claude_env: dict[str, str]
    allowed_users: frozenset[str]
    agent_image: str
    state_dir: Path
    work_dir: Path
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

        gitlab_url = (value("GITLAB_URL") or "https://gitlab.com").rstrip("/")
        gitlab_token = required("GITLAB_TOKEN")

        claude_env = {n: v for n in CLAUDE_CREDENTIALS if (v := value(n))}
        if len(claude_env) != 1:
            problems.append(f"exactly one of {' / '.join(CLAUDE_CREDENTIALS)} is required")

        allowed_raw = required("ALLOWED_USERS")
        allowed_users = frozenset(u.strip().lower() for u in allowed_raw.split(",") if u.strip())
        if allowed_raw and not allowed_users:
            problems.append("ALLOWED_USERS has no usernames")

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
            allowed_users=allowed_users,
            agent_image=agent_image,
            state_dir=state_dir,
            work_dir=work_dir,
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
