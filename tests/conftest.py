import json
from pathlib import Path
from typing import Any

from gitlab_claude_bot.gitlab import Kind, Project, User

FIXTURES = Path(__file__).parent / "fixtures"

BOT = User(id=9, username="claude-bot", name="Claude Bot", email="bot@example.com", bot=True)
ALLOWED = frozenset({"alice", "bob"})


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeGitLab:
    def __init__(
        self,
        todos: list[dict] | None = None,
        discussions: dict[tuple[int, Kind, int], list[dict]] | None = None,
        mrs: list[dict] | None = None,
        user_id: int = BOT.id,
        projects: dict[int, Project] | None = None,
        issues: dict[tuple[int, int], dict] | None = None,
    ):
        self.todos = todos or []
        self.threads = discussions or {}
        self.mrs = mrs or []
        self.user_id = user_id
        self.projects = projects or {}
        self.issues = issues or {}
        self.eyes: set[tuple[int, Kind, int, int | None, int]] = set()
        self.done: list[int] = []
        self.calls: list[tuple] = []
        self.created_mrs: list[dict] = []
        self.notes: list[tuple[str, int, Kind, int, str | None, str]] = []

    def pending_todos(self) -> list[dict]:
        self.calls.append(("pending_todos",))
        return list(self.todos)

    def mark_todo_done(self, todo_id: int) -> None:
        self.done.append(todo_id)

    def discussions(self, project_id: int, kind: Kind, iid: int) -> list[dict]:
        self.calls.append(("discussions", project_id, kind, iid))
        return self.threads.get((project_id, kind, iid), [])

    def bot_open_mrs(self, author_id: int, updated_after: str | None = None) -> list[dict]:
        self.calls.append(("bot_open_mrs", author_id, updated_after))
        return [mr for mr in self.mrs if mr["author"]["id"] == author_id]

    def award_eyes(self, project_id: int, kind: Kind, iid: int, note_id: int | None = None) -> bool:
        self.calls.append(("award_eyes", project_id, kind, iid, note_id))
        key = (project_id, kind, iid, note_id, self.user_id)
        if key in self.eyes:
            return False
        self.eyes.add(key)
        return True

    def has_eyes(self, project_id: int, kind: Kind, iid: int, note_id: int | None, user_id: int) -> bool:
        self.calls.append(("has_eyes", project_id, kind, iid, note_id, user_id))
        return (project_id, kind, iid, note_id, user_id) in self.eyes

    def project(self, project_id: int) -> Project:
        return self.projects[project_id]

    def issue(self, project_id: int, iid: int) -> dict:
        return self.issues[(project_id, iid)]

    def merge_request(self, project_id: int, iid: int) -> dict:
        return next(mr for mr in self.mrs if mr["project_id"] == project_id and mr["iid"] == iid)

    def project_open_mrs_by(self, project_id: int, author_id: int) -> list[dict]:
        return [mr for mr in self.mrs if mr["project_id"] == project_id and mr["author"]["id"] == author_id]

    def create_mr(
        self, project_id: int, source: str, target: str, title: str, description: str, assignee_id: int | None
    ) -> dict:
        mr = {
            "id": 5000 + len(self.created_mrs),
            "iid": 50 + len(self.created_mrs),
            "project_id": project_id,
            "source_project_id": project_id,
            "source_branch": source,
            "target_branch": target,
            "title": title,
            "description": description,
            "assignee_ids": [assignee_id] if assignee_id is not None else [],
            "author": {"id": self.user_id},
        }
        self.created_mrs.append(mr)
        self.mrs.append(mr)
        return mr

    def reply(self, project_id: int, kind: Kind, iid: int, discussion_id: str, body: str) -> dict:
        self.notes.append(("reply", project_id, kind, iid, discussion_id, body))
        return {"id": 900 + len(self.notes), "body": body}

    def comment(self, project_id: int, kind: Kind, iid: int, body: str) -> dict:
        self.notes.append(("comment", project_id, kind, iid, None, body))
        return {"id": 900 + len(self.notes), "body": body}
