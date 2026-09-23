import logging
import time
from collections.abc import Iterator
from typing import Any

import httpx

from .forge import GITHUB, ForgeError, Kind, Project, User, retry_after

log = logging.getLogger(__name__)
_sleep = time.sleep


class GitHubError(ForgeError):
    def __init__(self, status: int, body: str):
        super().__init__("GitHub", status, body)


def _author(user: dict | None) -> dict:
    user = user or {"id": 0, "login": "ghost", "type": "User"}
    return {"id": user["id"], "username": user["login"], "bot": user.get("type") == "Bot"}


def _note(comment: dict) -> dict:
    return {
        "id": comment["id"],
        "author": _author(comment.get("user")),
        "body": comment.get("body") or "",
        "created_at": comment["created_at"],
        "system": False,
    }


def _rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    return response.status_code == 403 and (
        "Retry-After" in response.headers or response.headers.get("x-ratelimit-remaining") == "0"
    )


class GitHub:
    labels = GITHUB
    git_user = "x-access-token"

    def __init__(self, url: str, token: str, transport: httpx.BaseTransport | None = None):
        self.token = token
        self._client = httpx.Client(
            base_url=url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
            transport=transport,
        )
        self._projects: dict[int, Project] = {}
        self._logins: dict[int, str] = {}
        self._repo_ids: dict[str, int] = {}

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._client.request(method, path, **kwargs)
        if _rate_limited(response):
            wait = retry_after(response)
            log.debug("%s %s rate limited, retrying in %ss", method, path, wait)
            _sleep(wait)
            response = self._client.request(method, path, **kwargs)
        log.debug("%s %s -> %s", method, path, response.status_code)
        if not response.is_success:
            raise GitHubError(response.status_code, response.text)
        return response

    def get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params).json()

    def post(self, path: str, json: dict | None = None) -> Any:
        response = self._request("POST", path, json=json)
        return response.json() if response.content else None

    def paginate(self, path: str, **params: Any) -> Iterator[dict]:
        url: str | None = path
        query: dict[str, Any] | None = {**params, "per_page": 100}
        while url:
            response = self._request("GET", url, params=query)
            data = response.json()
            yield from data["items"] if isinstance(data, dict) else data
            url = response.links.get("next", {}).get("url")
            query = None

    def _repo(self, project_id: int) -> str:
        return f"/repos/{self.project(project_id).path_with_namespace}"

    def me(self) -> User:
        data = self.get("/user")
        self._logins[data["id"]] = data["login"]
        return User(
            id=data["id"],
            username=data["login"],
            name=data.get("name") or data["login"],
            email=data.get("email") or f"{data['id']}+{data['login']}@users.noreply.github.com",
            bot=data.get("type") == "Bot",
        )

    def login(self, user_id: int) -> str:
        if user_id not in self._logins:
            self._logins[user_id] = self.get(f"/user/{user_id}")["login"]
        return self._logins[user_id]

    def unread_notifications(self) -> list[dict]:
        return list(self.paginate("/notifications"))

    def mark_todo_done(self, todo_id: int) -> None:
        self._request("PATCH", f"/notifications/threads/{todo_id}")

    def _remember(self, repo: dict) -> Project:
        project = Project(repo["id"], repo["full_name"], repo["default_branch"], repo["clone_url"])
        self._projects[project.id] = project
        return project

    def project(self, project_id: int) -> Project:
        if project_id not in self._projects:
            self._remember(self.get(f"/repositories/{project_id}"))
        return self._projects[project_id]

    def issue(self, project_id: int, iid: int) -> dict:
        data = self.get(f"{self._repo(project_id)}/issues/{iid}")
        return {
            "iid": iid,
            "title": data["title"],
            "description": data.get("body") or "",
            "author": _author(data["user"]),
        }

    def merge_request(self, project_id: int, iid: int) -> dict:
        data = self.get(f"{self._repo(project_id)}/pulls/{iid}")
        return self._mr(project_id, data)

    def _mr(self, project_id: int, pull: dict) -> dict:
        head_repo = pull["head"].get("repo")
        return {
            "iid": pull["number"],
            "project_id": project_id,
            "title": pull["title"],
            "description": pull.get("body") or "",
            "source_branch": pull["head"]["ref"],
            # a deleted fork leaves no head repo, and there is nothing left to clone
            "source_project_id": head_repo["id"] if head_repo else -1,
            "author": _author(pull["user"]),
        }

    def issue_events(self, project_id: int, iid: int) -> list[dict]:
        return list(self.paginate(f"{self._repo(project_id)}/issues/{iid}/events"))

    def discussions(self, project_id: int, kind: Kind, iid: int) -> list[dict]:
        # PR comments live on the issue endpoint too; each one is its own flat "discussion"
        comments = self.paginate(f"{self._repo(project_id)}/issues/{iid}/comments")
        return [{"id": str(c["id"]), "notes": [_note(c)]} for c in comments]

    def bot_open_mrs(self, author_id: int) -> list[dict]:
        query = f"is:pr is:open archived:false author:{self.login(author_id)}"
        mrs = []
        for item in self.paginate("/search/issues", q=query):
            url = item["repository_url"]
            if url not in self._repo_ids:
                self._repo_ids[url] = self._remember(self.get(url)).id
            mrs.append({"iid": item["number"], "project_id": self._repo_ids[url], "title": item["title"]})
        return mrs

    def project_open_mrs_by(self, project_id: int, author_id: int) -> list[dict]:
        pulls = self.paginate(f"{self._repo(project_id)}/pulls", state="open")
        return [self._mr(project_id, p) for p in pulls if p["user"]["id"] == author_id]

    def create_mr(
        self, project_id: int, source: str, target: str, title: str, description: str, assignee_id: int | None
    ) -> dict:
        repo = self._repo(project_id)
        pull = self.post(f"{repo}/pulls", json={"title": title, "head": source, "base": target, "body": description})
        if assignee_id is not None:
            self.post(f"{repo}/issues/{pull['number']}/assignees", json={"assignees": [self.login(assignee_id)]})
        return self._mr(project_id, pull)

    def _reactions_path(self, project_id: int, iid: int, note_id: int | None) -> str:
        repo = self._repo(project_id)
        return (
            f"{repo}/issues/comments/{note_id}/reactions" if note_id is not None else f"{repo}/issues/{iid}/reactions"
        )

    def award_eyes(self, project_id: int, kind: Kind, iid: int, note_id: int | None = None) -> bool:
        response = self._request("POST", self._reactions_path(project_id, iid, note_id), json={"content": "eyes"})
        # 200 means this user's reaction was already there
        return response.status_code == 201

    def has_eyes(self, project_id: int, kind: Kind, iid: int, note_id: int | None, user_id: int) -> bool:
        reactions = self.paginate(self._reactions_path(project_id, iid, note_id), content="eyes")
        return any(r["user"]["id"] == user_id for r in reactions)

    def reply(self, project_id: int, kind: Kind, iid: int, discussion_id: str, body: str) -> dict:
        return self.comment(project_id, kind, iid, body)

    def comment(self, project_id: int, kind: Kind, iid: int, body: str) -> dict:
        return self.post(f"{self._repo(project_id)}/issues/{iid}/comments", json={"body": body})
