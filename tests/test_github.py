import json
from collections.abc import Callable

import httpx
import pytest

from gitlab_claude_bot import github as github_module
from gitlab_claude_bot.github import GitHub, GitHubError

Handler = Callable[[httpx.Request], httpx.Response]
API = "https://api.github.example"
REPO = {"id": 42, "full_name": "acme/app", "default_branch": "main", "clone_url": "https://github.example/acme/app.git"}
ALICE = {"id": 1, "login": "alice", "type": "User"}
BOT = {"id": 9, "login": "CharlesBot2000", "type": "User"}


def client(routes: dict[tuple[str, str], object | Handler]) -> tuple[GitHub, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        route = routes[(request.method, request.url.path)]
        if callable(route):
            return route(request)
        return httpx.Response(200, json=route)

    return GitHub(API, "ghp-secret", transport=httpx.MockTransport(handler)), seen


def with_repo(routes: dict) -> dict:
    return {("GET", "/repositories/42"): REPO, **routes}


def test_auth_headers_and_base_url() -> None:
    gh, seen = client({("GET", "/user"): {**BOT, "name": None, "email": None}})
    bot = gh.me()
    assert seen[0].headers["Authorization"] == "Bearer ghp-secret"
    assert seen[0].headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert str(seen[0].url) == f"{API}/user"
    assert (bot.id, bot.username, bot.name) == (9, "CharlesBot2000", "CharlesBot2000")
    assert bot.email == "9+CharlesBot2000@users.noreply.github.com"


def test_paginate_follows_the_link_header() -> None:
    def notifications(request: httpx.Request) -> httpx.Response:
        if "page" not in request.url.params:
            assert request.url.params["per_page"] == "100"
            return httpx.Response(
                200, json=[{"id": "1"}], headers={"Link": f'<{API}/notifications?page=2>; rel="next"'}
            )
        return httpx.Response(200, json=[{"id": "2"}])

    gh, seen = client({("GET", "/notifications"): notifications})
    assert gh.unread_notifications() == [{"id": "1"}, {"id": "2"}]
    assert len(seen) == 2


def test_a_secondary_rate_limit_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[int] = []
    monkeypatch.setattr(github_module, "_sleep", slept.append)
    responses = iter([httpx.Response(403, headers={"Retry-After": "5"}), httpx.Response(200, json=BOT)])
    gh, _ = client({("GET", "/user"): lambda request: next(responses)})
    assert gh.me().id == 9
    assert slept == [5]


def test_a_plain_403_is_not_retried() -> None:
    gh, seen = client({("GET", "/user"): lambda request: httpx.Response(403, json={"message": "nope"})})
    with pytest.raises(GitHubError) as info:
        gh.me()
    assert info.value.status == 403
    assert len(seen) == 1


def test_mark_done_marks_the_thread_read() -> None:
    gh, seen = client({("PATCH", "/notifications/threads/77"): lambda request: httpx.Response(205)})
    gh.mark_todo_done(77)
    assert [(r.method, r.url.path) for r in seen] == [("PATCH", "/notifications/threads/77")]


def test_comments_become_one_discussion_each() -> None:
    comment = {"id": 500, "user": ALICE, "body": "@CharlesBot2000 hi", "created_at": "2026-09-23T10:00:00Z"}
    gh, _ = client(with_repo({("GET", "/repos/acme/app/issues/7/comments"): [comment]}))
    assert gh.discussions(42, "merge_requests", 7) == [
        {
            "id": "500",
            "notes": [
                {
                    "id": 500,
                    "author": {"id": 1, "username": "alice", "bot": False},
                    "body": "@CharlesBot2000 hi",
                    "created_at": "2026-09-23T10:00:00Z",
                    "system": False,
                }
            ],
        }
    ]


def pull(head_repo: dict | None) -> dict:
    return {"number": 3, "title": "T", "body": None, "user": BOT, "head": {"ref": "bot/issue-7", "repo": head_repo}}


def test_a_pull_request_reads_like_a_merge_request() -> None:
    gh, _ = client(with_repo({("GET", "/repos/acme/app/pulls/3"): pull(REPO)}))
    mr = gh.merge_request(42, 3)
    assert (mr["iid"], mr["source_branch"], mr["source_project_id"], mr["description"]) == (3, "bot/issue-7", 42, "")


def test_a_deleted_fork_is_a_fork() -> None:
    gh, _ = client(with_repo({("GET", "/repos/acme/app/pulls/3"): pull(None)}))
    assert gh.merge_request(42, 3)["source_project_id"] != 42


def test_create_mr_assigns_by_login() -> None:
    def create(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"title": "T", "head": "bot/issue-7", "base": "main", "body": "Closes #7"}
        return httpx.Response(201, json=pull(REPO))

    def assign(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"assignees": ["alice"]}
        return httpx.Response(201, json={})

    gh, _ = client(
        with_repo(
            {
                ("POST", "/repos/acme/app/pulls"): create,
                ("GET", "/user/1"): ALICE,
                ("POST", "/repos/acme/app/issues/3/assignees"): assign,
            }
        )
    )
    assert gh.create_mr(42, "bot/issue-7", "main", "T", "Closes #7", 1)["iid"] == 3


@pytest.mark.parametrize(("status", "fresh"), [(201, True), (200, False)])
def test_award_eyes_reports_whether_it_was_new(status: int, fresh: bool) -> None:
    def react(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"content": "eyes"}
        return httpx.Response(status, json={})

    gh, _ = client(with_repo({("POST", "/repos/acme/app/issues/comments/500/reactions"): react}))
    assert gh.award_eyes(42, "issues", 7, 500) is fresh


def test_has_eyes_on_the_issue_itself() -> None:
    def reactions(request: httpx.Request) -> httpx.Response:
        assert request.url.params["content"] == "eyes"
        return httpx.Response(200, json=[{"content": "eyes", "user": ALICE}, {"content": "eyes", "user": BOT}])

    gh, _ = client(with_repo({("GET", "/repos/acme/app/issues/7/reactions"): reactions}))
    assert gh.has_eyes(42, "issues", 7, None, 9)
    assert not gh.has_eyes(42, "issues", 7, None, 2)


def test_bot_open_mrs_searches_by_login_and_resolves_repo_ids() -> None:
    items = [{"number": 3, "title": "T", "repository_url": f"{API}/repos/acme/app"}]

    def search(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "is:pr is:open archived:false author:CharlesBot2000"
        return httpx.Response(200, json={"total_count": 1, "items": items})

    gh, seen = client({("GET", "/user"): BOT, ("GET", "/search/issues"): search, ("GET", "/repos/acme/app"): REPO})
    bot = gh.me()
    assert gh.bot_open_mrs(bot.id) == [{"iid": 3, "project_id": 42, "title": "T"}]
    gh.bot_open_mrs(bot.id)
    assert sum(r.url.path == "/repos/acme/app" for r in seen) == 1
