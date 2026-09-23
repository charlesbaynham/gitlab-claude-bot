import logging
import re
from dataclasses import dataclass
from typing import Literal

from .forge import Forge, Kind

log = logging.getLogger(__name__)

Action = Literal["assigned", "mentioned", "own_mr_comment"]

_NOTE_ANCHOR = re.compile(r"#note_(\d+)$")


@dataclass(frozen=True)
class Target:
    project_id: int
    kind: Kind
    iid: int

    @property
    def key(self) -> str:
        return f"{self.project_id}!{self.iid}"


@dataclass(frozen=True)
class Trigger:
    target: Target
    action: Action
    author: str
    author_id: int
    note_ids: tuple[int, ...] = ()
    discussion_id: str | None = None
    todo_ids: tuple[int, ...] = ()


def note_id_from_url(target_url: str) -> int | None:
    match = _NOTE_ANCHOR.search(target_url)
    return int(match.group(1)) if match else None


def acknowledge(gl: Forge, bot_id: int, trigger: Trigger) -> bool:
    t = trigger.target
    fresh = 0
    for note_id in trigger.note_ids or (None,):
        if gl.has_eyes(t.project_id, t.kind, t.iid, note_id, bot_id):
            continue
        fresh += gl.award_eyes(t.project_id, t.kind, t.iid, note_id)
    for todo_id in trigger.todo_ids:
        gl.mark_todo_done(todo_id)
    # an award on the target itself outlives the trigger, so only note awards can mean "seen"
    seen = not fresh and bool(trigger.note_ids)
    where = f"{t.key} notes {list(trigger.note_ids) or 'target'}"
    if seen:
        log.info("already acknowledged %s, skipping", where)
    else:
        log.info("acknowledged %s (%s by @%s)", where, trigger.action, trigger.author)
    return not seen
