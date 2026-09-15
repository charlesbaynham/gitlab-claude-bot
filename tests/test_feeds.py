from datetime import UTC, datetime

from conftest import ALLOWED, BOT, FakeGitLab, fixture
from gitlab_claude_bot.feeds import merge, own_mr_triggers, todo_triggers
from gitlab_claude_bot.state import State
from gitlab_claude_bot.triggers import Target, Trigger

ISSUE = Target(42, "issues", 7)
MR = Target(42, "merge_requests", 3)
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def todo_gl(*todos: dict | list[dict]) -> FakeGitLab:
    flat = [t for group in todos for t in (group if isinstance(group, list) else [group])]
    return FakeGitLab(
        todos=flat,
        discussions={(42, "issues", 7): fixture("discussions_issue"), (42, "merge_requests", 3): fixture("discussions_mr")},
    )


def mr_gl(discussions: list[dict] | None = None) -> FakeGitLab:
    return FakeGitLab(
        discussions={(42, "merge_requests", 3): fixture("discussions_mr") if discussions is None else discussions},
        mrs=[{"id": 1000, "iid": 3, "project_id": 42, "title": "Refactor config loading", "author": {"id": BOT.id}}],
    )


def test_assigned_issue() -> None:
    gl = todo_gl(fixture("todo_assigned_issue"))
    triggers, ignored = todo_triggers(gl, BOT, ALLOWED)
    assert ignored == []
    assert triggers == [Trigger(ISSUE, "assigned", "alice", 1, (), None, (501,))]
    assert not any(c[0] == "discussions" for c in gl.calls)


def test_note_mention_resolves_discussion() -> None:
    triggers, _ = todo_triggers(todo_gl(fixture("todo_note_mention_issue")), BOT, ALLOWED)
    assert triggers == [Trigger(ISSUE, "mentioned", "alice", 1, (102,), "d-bbb", (502,))]


def test_directly_addressed_is_a_mention() -> None:
    triggers, _ = todo_triggers(todo_gl(fixture("todo_directly_addressed")), BOT, ALLOWED)
    assert triggers == [Trigger(ISSUE, "mentioned", "bob", 2, (103,), "d-bbb", (504,))]


def test_description_mention_on_mr_has_no_notes() -> None:
    gl = todo_gl(fixture("todo_description_mention_mr"))
    triggers, _ = todo_triggers(gl, BOT, ALLOWED)
    assert triggers == [Trigger(MR, "mentioned", "alice", 1, (), None, (506,))]
    assert not any(c[0] == "discussions" for c in gl.calls)


def test_two_mentions_in_one_thread_collapse() -> None:
    gl = todo_gl(fixture("todo_two_mentions_one_thread"))
    triggers, ignored = todo_triggers(gl, BOT, ALLOWED)
    assert ignored == []
    assert triggers == [Trigger(ISSUE, "mentioned", "bob", 2, (102, 103), "d-bbb", (502, 504))]
    assert sum(c[0] == "discussions" for c in gl.calls) == 1


def test_review_requested_and_non_allowed_are_ignored() -> None:
    gl = todo_gl(fixture("todo_review_requested"), fixture("todo_not_allowed"))
    triggers, ignored = todo_triggers(gl, BOT, ALLOWED)
    assert triggers == []
    assert ignored == [507, 508]


def test_bots_own_todo_is_ignored() -> None:
    own = {**fixture("todo_note_mention_issue"), "id": 509, "author": {"id": BOT.id, "username": BOT.username}}
    other_bot = {**fixture("todo_note_mention_issue"), "id": 510, "author": {"id": 77, "username": "alice", "bot": True}}
    _, ignored = todo_triggers(todo_gl(own, other_bot), BOT, ALLOWED)
    assert ignored == [509, 510]


def test_unknown_target_type_is_ignored() -> None:
    epic = {**fixture("todo_note_mention_issue"), "id": 511, "target_type": "Epic"}
    _, ignored = todo_triggers(todo_gl(epic), BOT, ALLOWED)
    assert ignored == [511]


def test_deleted_note_falls_back_to_description_mention() -> None:
    gone = {**fixture("todo_note_mention_issue"), "target_url": "https://gitlab.example.com/group/proj/-/issues/7#note_999"}
    triggers, _ = todo_triggers(todo_gl(gone), BOT, ALLOWED)
    assert triggers == [Trigger(ISSUE, "mentioned", "alice", 1, (), None, (502,))]


def test_assignment_and_description_mention_collapse_as_assigned() -> None:
    mention = {**fixture("todo_assigned_issue"), "id": 512, "action_name": "mentioned", "created_at": "2026-09-15T09:00:00.000Z"}
    triggers, _ = todo_triggers(todo_gl(fixture("todo_assigned_issue"), mention), BOT, ALLOWED)
    assert triggers == [Trigger(ISSUE, "assigned", "alice", 1, (), None, (512, 501))]


def test_todo_triggers_ordered_oldest_first() -> None:
    gl = todo_gl(fixture("todo_description_mention_mr"), fixture("todo_assigned_issue"))
    triggers, _ = todo_triggers(gl, BOT, ALLOWED)
    assert [t.todo_ids for t in triggers] == [(501,), (506,)]


def test_first_sight_mr_sets_cursor_without_trigger() -> None:
    state = State()
    assert own_mr_triggers(mr_gl(), BOT, state, ALLOWED, NOW) == []
    assert state.mr_cursors == {"42!3": 205}
    assert state.last_mr_poll == "2026-09-15T12:00:00+00:00"


def test_first_run_polls_all_open_mrs() -> None:
    gl = mr_gl()
    own_mr_triggers(gl, BOT, State(), ALLOWED, NOW)
    assert gl.calls[0] == ("bot_open_mrs", BOT.id, None)


def test_later_runs_still_poll_all_open_mrs() -> None:
    gl = mr_gl()
    own_mr_triggers(gl, BOT, State(last_mr_poll="2026-09-15T11:30:00+00:00"), ALLOWED, NOW)
    assert gl.calls[0] == ("bot_open_mrs", BOT.id, None)


def test_three_notes_across_two_discussions_make_one_trigger() -> None:
    state = State(mr_cursors={"42!3": 199})
    triggers = own_mr_triggers(mr_gl(), BOT, state, ALLOWED, NOW)
    assert triggers == [Trigger(MR, "own_mr_comment", "alice", 1, (200, 203, 205), "m-2", ())]
    assert state.mr_cursors["42!3"] == 205


def test_only_notes_after_cursor_count() -> None:
    state = State(mr_cursors={"42!3": 203})
    triggers = own_mr_triggers(mr_gl(), BOT, state, ALLOWED, NOW)
    assert triggers == [Trigger(MR, "own_mr_comment", "alice", 1, (205,), "m-2", ())]


def test_nothing_new_yields_nothing() -> None:
    state = State(mr_cursors={"42!3": 205})
    assert own_mr_triggers(mr_gl(), BOT, state, ALLOWED, NOW) == []
    assert state.mr_cursors["42!3"] == 205


def test_system_bot_and_stranger_notes_skipped_but_advance_cursor() -> None:
    quiet = [
        {"id": "q", "individual_note": False, "notes": [
            {"id": 300, "body": "Done.", "author": {"id": BOT.id, "username": BOT.username, "bot": True}, "system": False, "created_at": "2026-09-15T11:00:00.000Z"},
            {"id": 301, "body": "added 1 commit", "author": {"id": 1, "username": "alice", "bot": False}, "system": True, "created_at": "2026-09-15T11:01:00.000Z"},
            {"id": 302, "body": "hi", "author": {"id": 3, "username": "mallory", "bot": False}, "system": False, "created_at": "2026-09-15T11:02:00.000Z"},
        ]},
    ]
    state = State(mr_cursors={"42!3": 299})
    assert own_mr_triggers(mr_gl(quiet), BOT, state, ALLOWED, NOW) == []
    assert state.mr_cursors["42!3"] == 302


def test_cursor_never_moves_backwards() -> None:
    state = State(mr_cursors={"42!3": 900})
    own_mr_triggers(mr_gl(), BOT, state, ALLOWED, NOW)
    assert state.mr_cursors["42!3"] == 900


def test_mr_with_no_notes_gets_zero_cursor() -> None:
    state = State()
    own_mr_triggers(mr_gl([]), BOT, state, ALLOWED, NOW)
    assert state.mr_cursors == {"42!3": 0}


def test_merge_prefers_todo_variant_for_shared_note() -> None:
    todo = Trigger(MR, "mentioned", "alice", 1, (205,), "m-2", (600,))
    mr = Trigger(MR, "own_mr_comment", "alice", 1, (205,), "m-2", ())
    assert merge([todo], [mr]) == [todo]


def test_merge_strips_shared_notes_from_mr_trigger() -> None:
    todo = Trigger(MR, "mentioned", "alice", 1, (205,), "m-2", (600,))
    mr = Trigger(MR, "own_mr_comment", "alice", 1, (200, 203, 205), "m-2", ())
    assert merge([todo], [mr]) == [todo, Trigger(MR, "own_mr_comment", "alice", 1, (200, 203), "m-2", ())]


def test_merge_orders_todo_first_and_keeps_unrelated() -> None:
    todo = Trigger(ISSUE, "assigned", "alice", 1, (), None, (501,))
    mr = Trigger(MR, "own_mr_comment", "bob", 2, (203,), "m-2", ())
    assert merge([todo], [mr]) == [todo, mr]
    assert merge([], [mr]) == [mr]
