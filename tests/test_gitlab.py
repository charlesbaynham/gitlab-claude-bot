import json
from collections.abc import Callable

import httpx
import pytest

from gitlab_claude_bot import gitlab as gitlab_module
from gitlab_claude_bot.gitlab import GitLab, GitLabError

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, handler: Handler):
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def client(handler: Handler) -> tuple[GitLab, Recorder]:
    recorder = Recorder(handler)
    return GitLab("https://gitlab.example.com", "secret-token", transport=httpx.MockTransport(recorder)), recorder


def ok(payload: object, status: int = 200, headers: dict[str, str] | None = None) -> Handler:
    return lambda request: httpx.Response(status, json=payload, headers=headers)


def test_auth_header_and_base_url() -> None:
    api, recorder = client(ok({"id": 1}))
    api.get("/user")
    request = recorder.requests[0]
    assert request.headers["PRIVATE-TOKEN"] == "secret-token"
    assert str(request.url) == "https://gitlab.example.com/api/v4/user"


def test_paginate_follows_next_page_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        assert request.url.params["per_page"] == "100"
        if page == "1":
            return httpx.Response(200, json=[{"id": 1}], headers={"x-next-page": "2"})
        return httpx.Response(200, json=[{"id": 2}], headers={"x-next-page": ""})

    api, recorder = client(handler)
    assert list(api.paginate("/todos", state="pending")) == [{"id": 1}, {"id": 2}]
    assert len(recorder.requests) == 2
    assert all(r.url.params["state"] == "pending" for r in recorder.requests)


def test_429_retried_once_after_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[int] = []
    monkeypatch.setattr(gitlab_module, "_sleep", slept.append)
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"id": 1}),
        ]
    )
    api, recorder = client(lambda request: next(responses))
    assert api.get("/user") == {"id": 1}
    assert slept == [7]
    assert len(recorder.requests) == 2


def test_429_twice_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitlab_module, "_sleep", lambda _: None)
    api, _ = client(ok({"message": "slow down"}, status=429, headers={"Retry-After": "999"}))
    with pytest.raises(GitLabError) as info:
        api.get("/user")
    assert info.value.status == 429


def test_non_2xx_raises_with_status_and_body() -> None:
    api, _ = client(ok({"message": "403 Forbidden"}, status=403))
    with pytest.raises(GitLabError) as info:
        api.get("/projects/1")
    assert info.value.status == 403
    assert "Forbidden" in info.value.body


def test_me_prefers_commit_email() -> None:
    api, _ = client(ok({"id": 5, "username": "bot", "name": "Bot", "email": "a@x", "commit_email": "c@x", "bot": True}))
    user = api.me()
    assert (user.id, user.username, user.email, user.bot) == (5, "bot", "c@x", True)


def test_me_falls_back_to_email() -> None:
    api, _ = client(ok({"id": 5, "username": "bot", "name": "Bot", "email": "a@x", "commit_email": None}))
    user = api.me()
    assert user.email == "a@x"
    assert user.bot is False


def test_project_cached() -> None:
    api, recorder = client(
        ok({"id": 9, "path_with_namespace": "g/p", "default_branch": "main", "http_url_to_repo": "https://x/g/p.git"})
    )
    first = api.project(9)
    second = api.project(9)
    assert first == second
    assert first.path_with_namespace == "g/p"
    assert len(recorder.requests) == 1


def test_pending_todos_and_mark_done() -> None:
    api, recorder = client(ok([{"id": 3}]))
    assert api.pending_todos() == [{"id": 3}]
    assert recorder.requests[0].url.params["state"] == "pending"
    api.mark_todo_done(3)
    assert recorder.requests[1].method == "POST"
    assert recorder.requests[1].url.path == "/api/v4/todos/3/mark_as_done"


def test_bot_open_mrs_params() -> None:
    api, recorder = client(ok([]))
    api.bot_open_mrs(7, updated_after="2026-09-15T00:00:00Z")
    params = recorder.requests[0].url.params
    assert params["scope"] == "all"
    assert params["state"] == "opened"
    assert params["author_id"] == "7"
    assert params["updated_after"] == "2026-09-15T00:00:00Z"
    api.bot_open_mrs(7)
    assert "updated_after" not in recorder.requests[1].url.params


def test_project_open_mrs_by() -> None:
    api, recorder = client(ok([]))
    api.project_open_mrs_by(4, 7)
    request = recorder.requests[0]
    assert request.url.path == "/api/v4/projects/4/merge_requests"
    assert request.url.params["author_id"] == "7"
    assert request.url.params["state"] == "opened"


def test_create_mr_payload() -> None:
    api, recorder = client(ok({"iid": 12}, status=201))
    api.create_mr(4, "bot/fix", "main", "Fix it", "Body", assignee_id=8)
    request = recorder.requests[0]
    assert request.url.path == "/api/v4/projects/4/merge_requests"
    payload = json.loads(request.content)
    assert payload["remove_source_branch"] is True
    assert payload["assignee_ids"] == [8]
    assert payload["source_branch"] == "bot/fix"
    assert payload["target_branch"] == "main"


def test_award_eyes_true_on_201() -> None:
    api, recorder = client(ok({"id": 1}, status=201))
    assert api.award_eyes(4, "merge_requests", 2, note_id=99) is True
    request = recorder.requests[0]
    assert request.url.path == "/api/v4/projects/4/merge_requests/2/notes/99/award_emoji"
    assert request.url.params["name"] == "eyes"


def test_award_eyes_on_target_without_note() -> None:
    api, recorder = client(ok({"id": 1}, status=201))
    assert api.award_eyes(4, "issues", 2) is True
    assert recorder.requests[0].url.path == "/api/v4/projects/4/issues/2/award_emoji"


def test_award_eyes_false_on_404() -> None:
    api, _ = client(ok({"message": "404 Not found"}, status=404))
    assert api.award_eyes(4, "issues", 2) is False


def test_has_eyes_true_and_false() -> None:
    awards = [{"name": "thumbsup", "user": {"id": 5}}, {"name": "eyes", "user": {"id": 6}}]
    api, _ = client(ok(awards))
    assert api.has_eyes(4, "issues", 2, None, 6) is True
    assert api.has_eyes(4, "issues", 2, None, 5) is False


@pytest.mark.parametrize("kind", ["issues", "merge_requests"])
def test_reply_and_comment_paths(kind: str) -> None:
    api, recorder = client(ok({"id": 1}, status=201))
    api.reply(4, kind, 2, "abc123", "hello")
    api.comment(4, kind, 2, "hi")
    reply, comment = recorder.requests
    assert reply.url.path == f"/api/v4/projects/4/{kind}/2/discussions/abc123/notes"
    assert json.loads(reply.content) == {"body": "hello"}
    assert comment.url.path == f"/api/v4/projects/4/{kind}/2/notes"
    assert json.loads(comment.content) == {"body": "hi"}


def test_discussions_paginates() -> None:
    api, recorder = client(ok([{"id": "d1"}]))
    assert api.discussions(4, "merge_requests", 2) == [{"id": "d1"}]
    assert recorder.requests[0].url.path == "/api/v4/projects/4/merge_requests/2/discussions"
