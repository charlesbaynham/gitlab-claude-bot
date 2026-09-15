# GitLab's target_url for an issue points at .../work_items/<iid>, not .../issues/<iid>.
from conftest import FakeGitLab, fixture
from gitlab_claude_bot.feeds import todo_triggers
from gitlab_claude_bot.gitlab import User
from gitlab_claude_bot.triggers import Target, Trigger, note_id_from_url

BOT = User(id=34622077, username="charlesbot2000", name="Charles Bot", email=None, bot=True)
ALLOWED = frozenset({"charlesbaynham"})

ISSUE1 = Target(86502915, "issues", 1)
ISSUE2 = Target(86502915, "issues", 2)
ISSUE3 = Target(86502915, "issues", 3)
MR1 = Target(86502915, "merge_requests", 1)

REVIEW_REQUESTED_TODO_ID = 764430323


def real_gl() -> FakeGitLab:
    return FakeGitLab(
        todos=fixture("real_todos"),
        discussions={(86502915, "issues", 3): fixture("real_discussions_issue3")},
    )


def test_review_requested_is_ignored_and_four_triggers_result() -> None:
    triggers, ignored = todo_triggers(real_gl(), BOT, ALLOWED)
    assert REVIEW_REQUESTED_TODO_ID in ignored
    assert len(triggers) == 4


def test_issue_1_assigned() -> None:
    triggers, _ = todo_triggers(real_gl(), BOT, ALLOWED)
    trigger = next(t for t in triggers if t.target == ISSUE1)
    assert trigger.action == "assigned"
    assert trigger.note_ids == ()


def test_issue_2_description_mention_has_no_notes() -> None:
    triggers, _ = todo_triggers(real_gl(), BOT, ALLOWED)
    trigger = next(t for t in triggers if t.target == ISSUE2)
    assert trigger.action == "mentioned"
    assert trigger.note_ids == ()


def test_issue_3_mention_resolves_to_its_discussion() -> None:
    triggers, _ = todo_triggers(real_gl(), BOT, ALLOWED)
    trigger = next(t for t in triggers if t.target == ISSUE3)
    assert trigger.action == "mentioned"
    assert trigger.note_ids == (3837883709,)
    assert trigger.discussion_id == "cfd71500a0afad2da797b2966de93a2cdc3af71a"


def test_mr_1_mention_degrades_to_empty_notes_when_discussions_come_back_empty() -> None:
    triggers, _ = todo_triggers(real_gl(), BOT, ALLOWED)
    trigger = next(t for t in triggers if t.target == MR1)
    assert trigger.action == "mentioned"
    assert trigger.note_ids == ()
    assert trigger.discussion_id is None


def test_note_id_from_url_handles_a_real_work_items_url() -> None:
    assert note_id_from_url("https://gitlab.com/charlesbaynham/gcb-scratch/-/work_items/3#note_3837883709") == 3837883709
