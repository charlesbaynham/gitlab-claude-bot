import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

import httpx

log = logging.getLogger(__name__)
_sleep = time.sleep

Kind = Literal["issues", "merge_requests"]


class GitLabError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"GitLab returned {status}: {body[:200]}")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class User:
    id: int
    username: str
    name: str
    email: str | None
    bot: bool = False


@dataclass(frozen=True)
class Project:
    id: int
    path_with_namespace: str
    default_branch: str
    http_url_to_repo: str


def _retry_after(response: httpx.Response) -> int:
    try:
        return min(int(response.headers.get("Retry-After", 1)), 60)
    except ValueError:
        return 60


def _target(project_id: int, kind: Kind, iid: int) -> str:
    return f"/projects/{project_id}/{kind}/{iid}"


class GitLab:
    def __init__(self, url: str, token: str, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            base_url=f"{url}/api/v4",
            headers={"PRIVATE-TOKEN": token},
            timeout=30,
            transport=transport,
        )
        self._projects: dict[int, Project] = {}

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._client.request(method, path, **kwargs)
        if response.status_code == 429:
            wait = _retry_after(response)
            log.debug("%s %s rate limited, retrying in %ss", method, path, wait)
            _sleep(wait)
            response = self._client.request(method, path, **kwargs)
        log.debug("%s %s -> %s", method, path, response.status_code)
        if not response.is_success:
            raise GitLabError(response.status_code, response.text)
        return response

    def get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params).json()

    def post(self, path: str, json: dict | None = None, **params: Any) -> Any:
        response = self._request("POST", path, json=json, params=params)
        return response.json() if response.content else None

    def paginate(self, path: str, **params: Any) -> Iterator[dict]:
        page = 1
        while page:
            response = self._request("GET", path, params={**params, "per_page": 100, "page": page})
            yield from response.json()
            page = int(response.headers.get("x-next-page") or 0)

    def me(self) -> User:
        data = self.get("/user")
        return User(
            id=data["id"],
            username=data["username"],
            name=data["name"],
            email=data.get("commit_email") or data.get("email"),
            bot=data.get("bot", False),
        )

    def pending_todos(self) -> list[dict]:
        return list(self.paginate("/todos", state="pending"))

    def mark_todo_done(self, todo_id: int) -> None:
        self.post(f"/todos/{todo_id}/mark_as_done")

    def project(self, project_id: int) -> Project:
        if project_id not in self._projects:
            data = self.get(f"/projects/{project_id}")
            self._projects[project_id] = Project(
                id=data["id"],
                path_with_namespace=data["path_with_namespace"],
                default_branch=data["default_branch"],
                http_url_to_repo=data["http_url_to_repo"],
            )
        return self._projects[project_id]

    def issue(self, project_id: int, iid: int) -> dict:
        return self.get(_target(project_id, "issues", iid))

    def merge_request(self, project_id: int, iid: int) -> dict:
        return self.get(_target(project_id, "merge_requests", iid))

    def discussions(self, project_id: int, kind: Kind, iid: int) -> list[dict]:
        return list(self.paginate(f"{_target(project_id, kind, iid)}/discussions"))

    def bot_open_mrs(self, author_id: int, updated_after: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"scope": "all", "state": "opened", "author_id": author_id}
        if updated_after:
            params["updated_after"] = updated_after
        return list(self.paginate("/merge_requests", **params))

    def project_open_mrs_by(self, project_id: int, author_id: int) -> list[dict]:
        return list(
            self.paginate(f"/projects/{project_id}/merge_requests", state="opened", author_id=author_id)
        )

    def create_mr(
        self,
        project_id: int,
        source: str,
        target: str,
        title: str,
        description: str,
        assignee_id: int | None,
    ) -> dict:
        payload: dict[str, Any] = {
            "source_branch": source,
            "target_branch": target,
            "title": title,
            "description": description,
            "remove_source_branch": True,
        }
        if assignee_id is not None:
            payload["assignee_ids"] = [assignee_id]
        return self.post(f"/projects/{project_id}/merge_requests", json=payload)

    def _award_path(self, project_id: int, kind: Kind, iid: int, note_id: int | None) -> str:
        note = f"/notes/{note_id}" if note_id is not None else ""
        return f"{_target(project_id, kind, iid)}{note}/award_emoji"

    def award_eyes(self, project_id: int, kind: Kind, iid: int, note_id: int | None = None) -> bool:
        try:
            self.post(self._award_path(project_id, kind, iid, note_id), name="eyes")
        except GitLabError as error:
            # GitLab answers 404 when this user has already awarded the emoji
            if error.status == 404:
                return False
            raise
        return True

    def has_eyes(self, project_id: int, kind: Kind, iid: int, note_id: int | None, user_id: int) -> bool:
        awards = self.paginate(self._award_path(project_id, kind, iid, note_id))
        return any(a["name"] == "eyes" and a["user"]["id"] == user_id for a in awards)

    def reply(self, project_id: int, kind: Kind, iid: int, discussion_id: str, body: str) -> dict:
        path = f"{_target(project_id, kind, iid)}/discussions/{discussion_id}/notes"
        return self.post(path, json={"body": body})

    def comment(self, project_id: int, kind: Kind, iid: int, body: str) -> dict:
        return self.post(f"{_target(project_id, kind, iid)}/notes", json={"body": body})
