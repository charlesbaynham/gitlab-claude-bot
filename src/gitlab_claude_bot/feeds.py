import logging
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from operator import itemgetter

from .gitlab import GitLab, Kind, User
from .state import State
from .triggers import Action, Target, Trigger, note_id_from_url

log = logging.getLogger(__name__)

TODO_ACTIONS: dict[str, Action] = {"assigned": "assigned", "mentioned": "mentioned", "directly_addressed": "mentioned"}
TARGET_KINDS: dict[str, Kind] = {"Issue": "issues", "MergeRequest": "merge_requests"}
MR_POLL_OVERLAP = timedelta(minutes=5)


def _from_bot(author: dict, bot: User) -> bool:
    return author["id"] == bot.id or bool(author.get("bot"))


def _discussion_of(discussions: list[dict], note_id: int) -> str | None:
    return next((d["id"] for d in discussions if any(n["id"] == note_id for n in d["notes"])), None)


def todo_triggers(gl: GitLab, bot: User, allowed: frozenset[str]) -> tuple[list[Trigger], list[int]]:
    ignored: list[int] = []
    groups: dict[tuple[Target, str | None], list[tuple[dict, int | None]]] = defaultdict(list)
    threads: dict[Target, list[dict]] = {}

    for todo in sorted(gl.pending_todos(), key=itemgetter("created_at")):
        kind = TARGET_KINDS.get(todo["target_type"])
        author = todo["author"]
        if (
            todo["action_name"] not in TODO_ACTIONS
            or kind is None
            or _from_bot(author, bot)
            or author["username"].lower() not in allowed
        ):
            ignored.append(todo["id"])
            continue
        target = Target(todo["project"]["id"], kind, todo["target"]["iid"])
        note_id = note_id_from_url(todo["target_url"])
        discussion_id = None
        if note_id is not None:
            if target not in threads:
                threads[target] = gl.discussions(target.project_id, target.kind, target.iid)
            discussion_id = _discussion_of(threads[target], note_id)
            if discussion_id is None:
                log.info("to-do %s: note %s is gone, treating as a description mention", todo["id"], note_id)
                note_id = None
        groups[(target, discussion_id)].append((todo, note_id))

    triggers = []
    for (target, discussion_id), members in groups.items():
        todos = [t for t, _ in members]
        newest = todos[-1]["author"]
        triggers.append(
            Trigger(
                target=target,
                action="assigned" if any(t["action_name"] == "assigned" for t in todos) else "mentioned",
                author=newest["username"].lower(),
                author_id=newest["id"],
                note_ids=tuple(n for _, n in members if n is not None),
                discussion_id=discussion_id,
                todo_ids=tuple(t["id"] for t in todos),
            )
        )
    log.info("to-dos: %d trigger(s), %d ignored", len(triggers), len(ignored))
    return triggers, ignored


def own_mr_triggers(
    gl: GitLab, bot: User, state: State, allowed: frozenset[str], now: datetime
) -> list[Trigger]:
    updated_after = None
    if state.last_mr_poll:
        updated_after = (datetime.fromisoformat(state.last_mr_poll) - MR_POLL_OVERLAP).isoformat()

    triggers = []
    for mr in gl.bot_open_mrs(bot.id, updated_after):
        target = Target(mr["project_id"], "merge_requests", mr["iid"])
        notes = sorted(
            ((n, d["id"]) for d in gl.discussions(target.project_id, target.kind, target.iid) for n in d["notes"]),
            key=lambda pair: pair[0]["id"],
        )
        newest_id = notes[-1][0]["id"] if notes else 0
        cursor = state.mr_cursors.get(target.key)
        state.mr_cursors[target.key] = max(cursor or 0, newest_id)
        if cursor is None:
            log.info("MR %s seen for the first time, cursor at %s", target.key, newest_id)
            continue
        actionable = [
            (n, did)
            for n, did in notes
            if n["id"] > cursor
            and not n.get("system")
            and not _from_bot(n["author"], bot)
            and n["author"]["username"].lower() in allowed
        ]
        if not actionable:
            continue
        newest, discussion_id = actionable[-1]
        triggers.append(
            Trigger(
                target=target,
                action="own_mr_comment",
                author=newest["author"]["username"].lower(),
                author_id=newest["author"]["id"],
                note_ids=tuple(n["id"] for n, _ in actionable),
                discussion_id=discussion_id,
            )
        )
    state.last_mr_poll = now.astimezone(UTC).isoformat(timespec="seconds")
    log.info("own MRs: %d trigger(s)", len(triggers))
    return triggers


def merge(todo: list[Trigger], mr: list[Trigger]) -> list[Trigger]:
    claimed = {n for t in todo for n in t.note_ids}
    kept = []
    for t in mr:
        remaining = tuple(n for n in t.note_ids if n not in claimed)
        if remaining:
            kept.append(t if remaining == t.note_ids else replace(t, note_ids=remaining))
    return [*todo, *kept]
