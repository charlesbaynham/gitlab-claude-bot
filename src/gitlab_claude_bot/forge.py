"""What jobs, triggers and feeds need from a forge. GitLab and GitHub both implement it.

Both clients speak GitLab's vocabulary: an MR/PR is a "merge_requests" target, a comment is a
note inside a discussion ({"id", "notes": [{"id", "author": {"id", "username", "bot"}, "body",
"created_at", "system"}]}), and an issue or MR is a dict carrying "title" and "description".
"""

from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

Kind = Literal["issues", "merge_requests"]


class ForgeError(Exception):
    def __init__(self, forge: str, status: int, body: str):
        super().__init__(f"{forge} returned {status}: {body[:200]}")
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


@dataclass(frozen=True)
class Labels:
    name: str
    mr: str
    mr_sigil: str

    @property
    def tag(self) -> str:
        return self.name.lower()


GITLAB = Labels("GitLab", "merge request", "!")
GITHUB = Labels("GitHub", "pull request", "#")


def retry_after(response: httpx.Response) -> int:
    try:
        return min(int(response.headers.get("Retry-After", 1)), 60)
    except ValueError:
        return 60


class Forge(Protocol):
    labels: Labels
    token: str
    git_user: str

    def me(self) -> User: ...
    def mark_todo_done(self, todo_id: int) -> None: ...
    def project(self, project_id: int) -> Project: ...
    def issue(self, project_id: int, iid: int) -> dict: ...
    def merge_request(self, project_id: int, iid: int) -> dict: ...
    def discussions(self, project_id: int, kind: Kind, iid: int) -> list[dict]: ...
    def bot_open_mrs(self, author_id: int) -> list[dict]: ...
    def project_open_mrs_by(self, project_id: int, author_id: int) -> list[dict]: ...
    def create_mr(
        self, project_id: int, source: str, target: str, title: str, description: str, assignee_id: int | None
    ) -> dict: ...
    def award_eyes(self, project_id: int, kind: Kind, iid: int, note_id: int | None = None) -> bool: ...
    def has_eyes(self, project_id: int, kind: Kind, iid: int, note_id: int | None, user_id: int) -> bool: ...
    def reply(self, project_id: int, kind: Kind, iid: int, discussion_id: str, body: str) -> dict: ...
    def comment(self, project_id: int, kind: Kind, iid: int, body: str) -> dict: ...
