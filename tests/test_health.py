import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from gitlab_claude_bot.health import Server, Status, serve

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def status(**overrides: object) -> Status:
    return Status(poll_interval=30, job_timeout=600, **overrides)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("age_s", "job_running", "expected"),
    [
        (None, None, False),
        (None, "job-1", False),
        (0, None, True),
        (689, None, True),
        (690, None, False),
        (100_000, None, False),
        (100_000, "job-1", True),
    ],
)
def test_healthy_truth_table(age_s: int | None, job_running: str | None, expected: bool) -> None:
    last = NOW - timedelta(seconds=age_s) if age_s is not None else None
    assert status(last_poll_ok=last, job_running=job_running).healthy(NOW) is expected


def test_report_has_no_secrets_and_iso_dates() -> None:
    report = status(last_poll_ok=NOW, bot_username="gcb", last_poll_error="boom").report(NOW)
    assert report == {
        "healthy": True,
        "poll_interval": 30,
        "job_timeout": 600,
        "last_poll_ok": "2026-09-15T12:00:00+00:00",
        "last_poll_error": "boom",
        "job_running": None,
        "jobs_done": 0,
        "jobs_failed": 0,
        "bot_username": "gcb",
    }


@pytest.fixture
def server() -> Iterator[tuple[Server, Status]]:
    current = status()
    thread = serve(0, current)
    yield thread, current
    thread.http.shutdown()


def get(port: int, path: str = "/health") -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = error.read()
        return error.code, json.loads(body) if body else {}


def test_serve_binds_ephemeral_port(server: tuple[Server, Status]) -> None:
    thread, _ = server
    assert thread.port > 0
    assert thread.daemon
    assert thread.is_alive()


def test_503_before_first_poll(server: tuple[Server, Status]) -> None:
    thread, _ = server
    code, body = get(thread.port)
    assert code == 503
    assert body["healthy"] is False
    assert body["last_poll_ok"] is None


def test_200_after_poll(server: tuple[Server, Status]) -> None:
    thread, current = server
    current.last_poll_ok = datetime.now(UTC)
    current.jobs_done = 3
    code, body = get(thread.port)
    assert code == 200
    assert body["healthy"] is True
    assert body["jobs_done"] == 3
    assert body["last_poll_ok"].startswith("2")


def test_503_when_stale(server: tuple[Server, Status]) -> None:
    thread, current = server
    current.last_poll_ok = datetime.now(UTC) - timedelta(days=1)
    assert get(thread.port)[0] == 503


def test_200_during_long_job(server: tuple[Server, Status]) -> None:
    thread, current = server
    current.last_poll_ok = datetime.now(UTC) - timedelta(days=1)
    current.job_running = "issue-42"
    code, body = get(thread.port)
    assert code == 200
    assert body["job_running"] == "issue-42"


def test_404_elsewhere(server: tuple[Server, Status]) -> None:
    thread, _ = server
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(f"http://127.0.0.1:{thread.port}/", timeout=5)
    assert info.value.code == 404
