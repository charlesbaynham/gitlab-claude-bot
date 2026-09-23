import pytest
from conftest import ALLOWED, BOT, FakeGitLab
from gitlab_claude_bot.forge import GITHUB
from gitlab_claude_bot.github_feeds import mentions, notification_triggers
from gitlab_claude_bot.triggers import Target, Trigger

ISSUE = Target(42, "issues", 7)
ALICE = {"id": 1, "login": "alice", "type": "User"}
MALLORY = {"id": 66, "login": "mallory", "type": "User"}
READ_AT = "2026-09-23T09:00:00Z"


class FakeGitHub(FakeGitLab):
    labels = GITHUB

    def __init__(self, notifications: list[dict], events: list[dict] | None = None, **kwargs: object):
        super().__init__(**kwargs)
        self.notifications = notifications
        self.events = events or []

    def unread_notifications(self) -> list[dict]:
        return list(self.notifications)

    def issue_events(self, project_id: int, iid: int) -> list[dict]:
        return list(self.events)


def thread(reason: str, kind: str = "Issue", number: int = 7, last_read_at: str | None = None, id: str = "1") -> dict:
    return {
        "id": id,
        "reason": reason,
        "updated_at": "2026-09-23T10:00:00Z",
        "last_read_at": last_read_at,
        "repository": {"id": 42, "full_name": "acme/app"},
        "subject": {"type": kind, "url": f"https://api.github.com/repos/acme/app/issues/{number}"},
    }


def event(kind: str, actor: dict = ALICE, at: str = "2026-09-23T10:00:00Z") -> dict:
    return {"event": kind, "actor": actor, "assignee": {"id": BOT.id, "login": BOT.username}, "created_at": at}


def note(id: int, body: str, author: dict = ALICE, at: str = "2026-09-23T10:00:00Z") -> dict:
    who = {"id": author["id"], "username": author["login"], "bot": False}
    return {"id": str(id), "notes": [{"id": id, "author": who, "body": body, "created_at": at, "system": False}]}


def issue(description: str = "", author: dict = ALICE) -> dict:
    return {"title": "T", "description": description, "author": {"id": author["id"], "username": author["login"]}}


def gh(
    notifications: list[dict], events: list[dict] | None = None, notes: list[dict] | None = None, body: str = ""
) -> FakeGitHub:
    return FakeGitHub(
        notifications, events, discussions={(42, "issues", 7): notes or []}, issues={(42, 7): issue(body)}
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("@claude-bot please", True),
        ("hey @Claude-Bot, look", True),
        ("(@claude-bot)", True),
        ("@claude-bot-two", False),
        ("mail claude-bot@example.com", False),
        ("see acme/@claude-bot", False),
        ("claude-bot", False),
    ],
)
def test_mentions(body: str, expected: bool) -> None:
    assert mentions(body, "claude-bot") is expected


def test_an_assignment_by_an_allowed_user() -> None:
    triggers, ignored = notification_triggers(gh([thread("assign")], [event("assigned")]), BOT, ALLOWED)
    assert ignored == []
    assert triggers == [Trigger(ISSUE, "assigned", "alice", 1, todo_ids=(1,))]


def test_an_assignment_later_undone_is_ignored() -> None:
    fake = gh([thread("assign")], [event("assigned"), event("unassigned", at="2026-09-23T10:01:00Z")])
    assert notification_triggers(fake, BOT, ALLOWED) == ([], [1])


def test_an_assignment_by_a_stranger_is_ignored() -> None:
    assert notification_triggers(gh([thread("assign")], [event("assigned", MALLORY)]), BOT, ALLOWED) == ([], [1])


def test_only_mentions_since_the_thread_was_last_read_count() -> None:
    notes = [
        note(500, "@claude-bot old", at="2026-09-23T08:00:00Z"),
        note(501, "unrelated"),
        note(502, "@claude-bot new"),
        note(503, "@claude-bot from a stranger", MALLORY),
    ]
    fake = gh([thread("mention", last_read_at=READ_AT)], notes=notes)
    triggers, _ = notification_triggers(fake, BOT, ALLOWED)
    assert triggers == [Trigger(ISSUE, "mentioned", "alice", 1, (502,), "502", (1,))]


def test_a_description_mention() -> None:
    triggers, _ = notification_triggers(gh([thread("mention")], body="Over to you @claude-bot"), BOT, ALLOWED)
    assert triggers == [Trigger(ISSUE, "mentioned", "alice", 1, todo_ids=(1,))]


def test_a_mention_found_nowhere_is_ignored() -> None:
    assert notification_triggers(gh([thread("mention")]), BOT, ALLOWED) == ([], [1])


def test_an_assignment_with_a_new_mention_carries_the_note() -> None:
    fake = gh([thread("mention")], [event("assigned")], [note(502, "@claude-bot also this")])
    triggers, _ = notification_triggers(fake, BOT, ALLOWED)
    assert triggers == [Trigger(ISSUE, "assigned", "alice", 1, (502,), "502", (1,))]


def test_a_pull_request_is_a_merge_request_target() -> None:
    fake = gh([thread("assign", kind="PullRequest", number=3)], [event("assigned")])
    triggers, _ = notification_triggers(fake, BOT, ALLOWED)
    assert triggers[0].target == Target(42, "merge_requests", 3)


@pytest.mark.parametrize(
    "unwanted",
    [thread("author"), thread("comment"), thread("review_requested"), thread("mention", kind="Release")],
)
def test_other_notifications_are_marked_read(unwanted: dict) -> None:
    assert notification_triggers(gh([unwanted]), BOT, ALLOWED) == ([], [1])
