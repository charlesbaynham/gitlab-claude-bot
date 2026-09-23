import logging
import re

from .forge import Kind, User
from .github import GitHub
from .triggers import Target, Trigger

log = logging.getLogger(__name__)

SUBJECT_KINDS: dict[str, Kind] = {"Issue": "issues", "PullRequest": "merge_requests"}


def mentions(body: str, login: str) -> bool:
    return re.search(rf"(?<![\w/@.-])@{re.escape(login)}(?![\w-])", body, re.IGNORECASE) is not None


def _allowed(author: dict, bot: User, allowed: frozenset[str]) -> bool:
    return author["id"] != bot.id and not author.get("bot") and author["username"].lower() in allowed


def _newer(stamp: str, since: str | None) -> bool:
    return since is None or stamp > since


def _assigner(gh: GitHub, bot: User, target: Target, since: str | None) -> dict | None:
    last = None
    for event in gh.issue_events(target.project_id, target.iid):
        if event["event"] in ("assigned", "unassigned") and (event.get("assignee") or {}).get("id") == bot.id:
            last = event
    if last is None or last["event"] == "unassigned" or not _newer(last["created_at"], since):
        return None
    actor = last["actor"]
    return {"id": actor["id"], "username": actor["login"], "bot": actor.get("type") == "Bot"}


def _thread_trigger(
    gh: GitHub, bot: User, allowed: frozenset[str], target: Target, thread_id: int, since: str | None
) -> Trigger | None:
    assigner = _assigner(gh, bot, target, since)
    if assigner is not None and not _allowed(assigner, bot, allowed):
        assigner = None
    action = "assigned" if assigner else "mentioned"
    notes = [
        n
        for d in gh.discussions(target.project_id, target.kind, target.iid)
        for n in d["notes"]
        if _newer(n["created_at"], since) and mentions(n["body"], bot.username) and _allowed(n["author"], bot, allowed)
    ]
    if notes:
        newest = notes[-1]["author"]
        return Trigger(
            target,
            action,
            newest["username"].lower(),
            newest["id"],
            note_ids=tuple(n["id"] for n in notes),
            discussion_id=str(notes[-1]["id"]),
            todo_ids=(thread_id,),
        )
    if assigner:
        return Trigger(target, action, assigner["username"].lower(), assigner["id"], todo_ids=(thread_id,))
    issue = gh.issue(target.project_id, target.iid)
    if mentions(issue["description"], bot.username) and _allowed(issue["author"], bot, allowed):
        author = issue["author"]
        return Trigger(target, action, author["username"].lower(), author["id"], todo_ids=(thread_id,))
    return None


def notification_triggers(gh: GitHub, bot: User, allowed: frozenset[str]) -> tuple[list[Trigger], list[int]]:
    triggers: list[Trigger] = []
    ignored: list[int] = []
    for thread in sorted(gh.unread_notifications(), key=lambda t: t["updated_at"]):
        thread_id = int(thread["id"])
        subject = thread["subject"]
        kind = SUBJECT_KINDS.get(subject["type"])
        trigger = None
        if kind is not None and subject.get("url") and thread["reason"] in ("assign", "mention"):
            target = Target(thread["repository"]["id"], kind, int(subject["url"].rsplit("/", 1)[1]))
            trigger = _thread_trigger(gh, bot, allowed, target, thread_id, thread.get("last_read_at"))
        if trigger is None:
            ignored.append(thread_id)
        else:
            triggers.append(trigger)
    log.info("notifications: %d trigger(s), %d ignored", len(triggers), len(ignored))
    return triggers, ignored
