from pathlib import Path

import pytest

from gitlab_claude_bot.config import Config, ConfigError

GOOD = {
    "GITLAB_TOKEN": "glpat-x",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-x",
    "ALLOWED_USERS": "alice,bob",
    "AGENT_IMAGE": "ghcr.io/x/agent:1",
    "STATE_DIR": "/var/lib/bot",
    "WORK_DIR": "/var/lib/bot/work",
}


def error_for(env: dict[str, str]) -> str:
    with pytest.raises(ConfigError) as info:
        Config.from_env(env)
    return str(info.value)


def test_all_required_present() -> None:
    cfg = Config.from_env(GOOD)
    assert cfg.gitlab_url == "https://gitlab.com"
    assert cfg.gitlab_token == "glpat-x"
    assert cfg.claude_env == {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-x"}
    assert cfg.gitlab_allowed_users == frozenset({"alice", "bob"})
    assert cfg.agent_image == "ghcr.io/x/agent:1"
    assert cfg.state_dir == Path("/var/lib/bot")
    assert cfg.work_dir == Path("/var/lib/bot/work")


def test_defaults() -> None:
    cfg = Config.from_env(GOOD)
    assert cfg.poll_interval == 30
    assert cfg.job_timeout == 1800
    assert cfg.max_budget_usd is None
    assert cfg.claude_model is None
    assert cfg.health_port == 8000
    assert cfg.clone_depth == 50


def test_optional_values_parsed() -> None:
    cfg = Config.from_env(
        GOOD
        | {
            "GITLAB_URL": "https://git.example.com/",
            "POLL_INTERVAL": "5",
            "JOB_TIMEOUT": "60",
            "MAX_BUDGET_USD": "2.5",
            "CLAUDE_MODEL": "claude-fable-5-1",
            "HEALTH_PORT": "9000",
            "CLONE_DEPTH": "1",
        }
    )
    assert cfg.gitlab_url == "https://git.example.com"
    assert cfg.poll_interval == 5
    assert cfg.job_timeout == 60
    assert cfg.max_budget_usd == 2.5
    assert cfg.claude_model == "claude-fable-5-1"
    assert cfg.health_port == 9000
    assert cfg.clone_depth == 1


@pytest.mark.parametrize("name", ["GITLAB_TOKEN", "ALLOWED_USERS", "AGENT_IMAGE", "STATE_DIR", "WORK_DIR"])
def test_each_required_missing(name: str) -> None:
    env = dict(GOOD)
    del env[name]
    assert name in error_for(env)


@pytest.mark.parametrize("name", ["GITLAB_TOKEN", "ALLOWED_USERS"])
def test_empty_counts_as_missing(name: str) -> None:
    assert name in error_for(GOOD | {name: "  "})


def test_api_key_alone_is_fine() -> None:
    env = dict(GOOD)
    del env["CLAUDE_CODE_OAUTH_TOKEN"]
    cfg = Config.from_env(env | {"ANTHROPIC_API_KEY": "sk-x"})
    assert cfg.claude_env == {"ANTHROPIC_API_KEY": "sk-x"}


def test_both_claude_credentials_rejected() -> None:
    assert "exactly one of" in error_for(GOOD | {"ANTHROPIC_API_KEY": "sk-x"})


def test_neither_claude_credential_rejected() -> None:
    env = dict(GOOD)
    del env["CLAUDE_CODE_OAUTH_TOKEN"]
    assert "exactly one of" in error_for(env)


def test_changeme_rejected() -> None:
    message = error_for(GOOD | {"GITLAB_TOKEN": "CHANGEME"})
    assert "GITLAB_TOKEN" in message and "CHANGEME" in message


def test_work_dir_under_tmp_rejected() -> None:
    assert "WORK_DIR" in error_for(GOOD | {"WORK_DIR": "/tmp/bot"})


def test_bad_integer_rejected() -> None:
    assert "POLL_INTERVAL" in error_for(GOOD | {"POLL_INTERVAL": "soon"})


def test_all_problems_reported_together() -> None:
    env = dict(GOOD)
    del env["GITLAB_TOKEN"]
    lines = error_for(env | {"WORK_DIR": "/tmp/x", "HEALTH_PORT": "abc"}).splitlines()
    assert len(lines) == 3
    assert any("GITLAB_TOKEN" in line for line in lines)
    assert any("WORK_DIR" in line for line in lines)
    assert any("HEALTH_PORT" in line for line in lines)


def test_allowed_users_normalised() -> None:
    cfg = Config.from_env(GOOD | {"ALLOWED_USERS": " Alice , BOB,, carol "})
    assert cfg.gitlab_allowed_users == frozenset({"alice", "bob", "carol"})


def test_allowed_users_only_separators_rejected() -> None:
    assert "ALLOWED_USERS" in error_for(GOOD | {"ALLOWED_USERS": " , ,"})


def test_bad_budget_rejected() -> None:
    assert "MAX_BUDGET_USD" in error_for(GOOD | {"MAX_BUDGET_USD": "lots"})


def test_github_only() -> None:
    env = {k: v for k, v in GOOD.items() if k != "GITLAB_TOKEN"}
    cfg = Config.from_env(env | {"GITHUB_TOKEN": "ghp-x"})
    assert (cfg.gitlab_token, cfg.github_token) == ("", "ghp-x")
    assert cfg.github_url == "https://api.github.com"
    assert cfg.github_allowed_users == frozenset({"alice", "bob"})


def test_neither_forge_token_rejected() -> None:
    env = {k: v for k, v in GOOD.items() if k != "GITLAB_TOKEN"}
    assert "GITLAB_TOKEN or GITHUB_TOKEN" in error_for(env)


def test_per_forge_allowed_users_override_the_shared_list() -> None:
    env = {k: v for k, v in GOOD.items() if k != "ALLOWED_USERS"}
    cfg = Config.from_env(
        env | {"GITHUB_TOKEN": "ghp-x", "GITLAB_ALLOWED_USERS": "alice", "GITHUB_ALLOWED_USERS": "Alice-GH"}
    )
    assert cfg.gitlab_allowed_users == frozenset({"alice"})
    assert cfg.github_allowed_users == frozenset({"alice-gh"})


def test_each_enabled_forge_needs_allowed_users() -> None:
    env = {k: v for k, v in GOOD.items() if k != "ALLOWED_USERS"}
    message = error_for(env | {"GITHUB_TOKEN": "ghp-x", "GITLAB_ALLOWED_USERS": "alice"})
    assert message == "ALLOWED_USERS or GITHUB_ALLOWED_USERS is required"
