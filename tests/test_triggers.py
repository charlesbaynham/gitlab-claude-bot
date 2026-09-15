import pytest

from conftest import BOT, FakeGitLab
from gitlab_claude_bot.triggers import Target, Trigger, acknowledge, note_id_from_url

ISSUE = Target(42, "issues", 7)


def trigger(note_ids: tuple[int, ...] = (), todo_ids: tuple[int, ...] = (501,)) -> Trigger:
    return Trigger(ISSUE, "mentioned", "alice", 1, note_ids, "d-bbb" if note_ids else None, todo_ids)


def test_target_key() -> None:
    assert ISSUE.key == "42!7"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://gitlab.example.com/g/p/-/issues/7#note_102", 102),
        ("https://gitlab.example.com/g/p/-/merge_requests/3#note_5", 5),
        ("https://gitlab.example.com/g/p/-/issues/7", None),
        ("https://gitlab.example.com/g/p/-/issues/7#note_", None),
    ],
)
def test_note_id_from_url(url: str, expected: int | None) -> None:
    assert note_id_from_url(url) == expected


def test_fresh_notes_get_eyes_and_todos_marked_done() -> None:
    gl = FakeGitLab()
    assert acknowledge(gl, BOT.id, trigger((102, 103), (501, 502))) is True
    assert gl.eyes == {(42, "issues", 7, 102, BOT.id), (42, "issues", 7, 103, BOT.id)}
    assert gl.done == [501, 502]


def test_one_unseen_note_is_enough() -> None:
    gl = FakeGitLab()
    gl.eyes.add((42, "issues", 7, 102, BOT.id))
    assert acknowledge(gl, BOT.id, trigger((102, 103))) is True
    assert ("award_eyes", 42, "issues", 7, 103) in gl.calls
    assert ("award_eyes", 42, "issues", 7, 102) not in gl.calls


def test_all_seen_returns_false_but_still_marks_todos_done() -> None:
    gl = FakeGitLab()
    gl.eyes |= {(42, "issues", 7, 102, BOT.id), (42, "issues", 7, 103, BOT.id)}
    assert acknowledge(gl, BOT.id, trigger((102, 103), (501,))) is False
    assert gl.done == [501]
    assert not any(c[0] == "award_eyes" for c in gl.calls)


def test_someone_elses_eyes_do_not_count() -> None:
    gl = FakeGitLab()
    gl.eyes.add((42, "issues", 7, 102, 1))
    assert acknowledge(gl, BOT.id, trigger((102,))) is True


def test_no_notes_acknowledges_the_target_itself() -> None:
    gl = FakeGitLab()
    assert acknowledge(gl, BOT.id, trigger()) is True
    assert gl.eyes == {(42, "issues", 7, None, BOT.id)}
    assert acknowledge(gl, BOT.id, trigger()) is False


def test_lost_award_race_counts_as_seen() -> None:
    class Racy(FakeGitLab):
        def has_eyes(self, *args: object) -> bool:
            return False

    gl = Racy()
    gl.eyes.add((42, "issues", 7, 102, BOT.id))
    assert acknowledge(gl, BOT.id, trigger((102,))) is False
