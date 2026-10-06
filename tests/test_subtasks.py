"""Closing, reopening, deleting and moving a to-do reaches the to-dos around it,
the way Nextcloud Tasks has it."""

from typing import Any

import caldav
from caldav.elements import dav
from caldav.lib.error import NotFoundError, PutError
from caldav.lib.url import URL
from conftest import RecordingClient
from icalendar import Calendar as ICalCalendar
import pytest

from custom_components.ha_caldav.api import (
    create_todo,
    delete_todos,
    move_todo,
    update_todo,
)
from custom_components.ha_caldav.errors import Refused
from custom_components.ha_caldav.task import parent_uid


def _task(uid: str, *lines: str) -> str:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VTODO\r\n"
        f"UID:{uid}\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:{uid}\r\n"
        + "".join(f"{line}\r\n" for line in lines)
        + "END:VTODO\r\nEND:VCALENDAR\r\n"
    )


def _under(uid: str, parent: str, *lines: str) -> str:
    return _task(uid, f"RELATED-TO:{parent}", *lines)


def _vtodo(body: str) -> Any:
    return next(iter(ICalCalendar.from_ical(body).walk("VTODO")))


class TodoList:
    """A to-do list held by a RecordingClient, read back the way caldav reads one."""

    def __init__(
        self, *bodies: str, client: RecordingClient | None = None, name: str = "cal"
    ) -> None:
        self.client = client or RecordingClient()
        self.url = URL(f"https://dav.test/{name}/")
        self.searches = 0
        for body in bodies:
            uid = str(_vtodo(body)["UID"])
            self.client.stored[f"{self.url}{uid}.ics"] = (body, f'"{uid}-1"')

    def search(self, **kwargs) -> list[caldav.Todo]:
        self.searches += 1
        if not kwargs.get("todo"):
            return []
        found = []
        for url, (body, etag) in self.client.stored.items():
            if url.startswith(str(self.url)):
                item = caldav.Todo(client=self.client, data=body, url=url, parent=self)
                item.props[dav.GetEtag.tag] = etag
                found.append(item)
        return found

    def todo_by_uid(self, uid: str) -> caldav.Todo:
        for item in self.search(todo=True):
            if str(_vtodo(str(item.data))["UID"]) == uid:
                self.searches -= 1
                return item
        raise NotFoundError(uid)

    def event_by_uid(self, uid: str) -> caldav.Event:
        raise NotFoundError(uid)

    def held(self) -> dict[str, Any]:
        """Return the to-do stored under every uid."""
        return {
            str(vtodo["UID"]): vtodo
            for url, (body, _) in self.client.stored.items()
            if url.startswith(str(self.url))
            for vtodo in [_vtodo(body)]
        }

    def statuses(self) -> dict[str, str]:
        return {
            uid: str(vtodo.get("STATUS", "NEEDS-ACTION"))
            for uid, vtodo in self.held().items()
        }

    def written(self) -> list[str]:
        """Return the uids in the order they were written."""
        return [str(_vtodo(body)["UID"]) for _, body in self.client.puts]


def _complete(todos: TodoList, uid: str) -> None:
    update_todo(todos, uid, {"summary": uid, "status": "COMPLETED"})


def test_completing_a_parent_completes_its_open_subtasks_the_deepest_first() -> None:
    todos = TodoList(
        _task("shop"), _under("milk", "shop"), _under("oat", "milk"), _task("other")
    )

    _complete(todos, "shop")

    assert todos.written() == ["oat", "milk", "shop"]
    assert todos.statuses() == {
        "shop": "COMPLETED",
        "milk": "COMPLETED",
        "oat": "COMPLETED",
        "other": "NEEDS-ACTION",
    }
    assert "COMPLETED" in todos.held()["oat"]


def test_what_is_below_a_subtask_closed_already_is_left_as_it_is() -> None:
    todos = TodoList(
        _task("shop"), _under("milk", "shop", "STATUS:CANCELLED"), _under("oat", "milk")
    )

    _complete(todos, "shop")

    assert todos.written() == ["shop"]
    assert todos.statuses()["milk"] == "CANCELLED"
    assert todos.statuses()["oat"] == "NEEDS-ACTION"


def test_canceling_a_parent_cancels_its_open_subtasks() -> None:
    todos = TodoList(_task("shop"), _under("milk", "shop", "STATUS:IN-PROCESS"))

    update_todo(todos, "shop", {"status": "CANCELLED"}, as_shown=False)

    assert todos.statuses() == {"shop": "CANCELLED", "milk": "CANCELLED"}


def test_a_recurring_subtask_moves_on_to_its_next_date() -> None:
    todos = TodoList(
        _task("shop"),
        _under("milk", "shop", "DUE;VALUE=DATE:20260710", "RRULE:FREQ=WEEKLY"),
    )

    _complete(todos, "shop")

    milk = todos.held()["milk"]
    assert milk["DUE"].dt.isoformat() == "2026-07-17"
    assert str(milk["STATUS"]) == "NEEDS-ACTION"


def test_a_recurring_parent_closes_its_subtasks_and_moves_on() -> None:
    todos = TodoList(
        _task("shop", "DUE;VALUE=DATE:20260710", "RRULE:FREQ=WEEKLY"),
        _under("milk", "shop"),
    )

    _complete(todos, "shop")

    assert todos.statuses() == {"shop": "NEEDS-ACTION", "milk": "COMPLETED"}
    assert todos.held()["shop"]["DUE"].dt.isoformat() == "2026-07-17"


def test_a_parent_stays_open_when_a_subtask_could_not_be_closed() -> None:
    todos = TodoList(_task("shop"), _under("milk", "shop"))
    todos.client.fail_from = 0

    with pytest.raises(PutError):
        _complete(todos, "shop")

    assert todos.written() == ["milk"]
    assert todos.statuses() == {"shop": "NEEDS-ACTION", "milk": "NEEDS-ACTION"}


def test_a_subtask_is_written_over_the_version_the_list_was_read_at() -> None:
    todos = TodoList(_task("shop"), _under("milk", "shop"))

    _complete(todos, "shop")

    assert [headers.get("If-Match") for headers in todos.client.put_headers] == [
        '"milk-1"',
        '"shop-1"',
    ]


def test_reopening_a_subtask_reopens_the_closed_parents_above_it() -> None:
    done = ("STATUS:COMPLETED", "COMPLETED:20260101T120000Z", "PERCENT-COMPLETE:100")
    todos = TodoList(
        _task("trip"),
        _under("shop", "trip", *done),
        _under("milk", "shop", *done),
        _under("oat", "milk", *done),
    )

    update_todo(todos, "oat", {"summary": "oat", "status": "NEEDS-ACTION"})

    assert todos.written() == ["milk", "shop", "oat"]
    assert set(todos.statuses().values()) == {"NEEDS-ACTION"}
    assert "COMPLETED" not in todos.held()["shop"]


def test_reopening_stops_at_the_first_parent_that_is_open() -> None:
    done = ("STATUS:COMPLETED", "COMPLETED:20260101T120000Z")
    todos = TodoList(
        _task("trip", *done), _under("shop", "trip"), _under("milk", "shop", *done)
    )

    update_todo(todos, "milk", {"summary": "milk", "status": "NEEDS-ACTION"})

    assert todos.written() == ["milk"]
    assert todos.statuses()["trip"] == "COMPLETED"


def test_an_edit_that_changes_no_state_reads_the_item_alone() -> None:
    todos = TodoList(_task("shop"), _under("milk", "shop"))

    update_todo(todos, "shop", {"summary": "Groceries", "status": "NEEDS-ACTION"})

    assert todos.searches == 0
    assert todos.written() == ["shop"]


def test_an_edit_leaves_the_parent_of_a_subtask_alone() -> None:
    todos = TodoList(_task("shop"), _under("milk", "shop"))

    update_todo(todos, "milk", {"summary": "Oat milk", "status": "NEEDS-ACTION"})

    assert parent_uid(todos.held()["milk"]) == "shop"


def test_a_todo_put_under_a_completed_parent_is_completed_with_it() -> None:
    todos = TodoList(
        _task("shop", "STATUS:COMPLETED"), _task("milk"), _under("oat", "milk")
    )

    update_todo(todos, "milk", {"parent_uid": "shop"}, as_shown=False)

    assert parent_uid(todos.held()["milk"]) == "shop"
    assert todos.statuses() == {
        "shop": "COMPLETED",
        "milk": "COMPLETED",
        "oat": "COMPLETED",
    }


def test_a_todo_put_under_an_open_parent_keeps_its_state() -> None:
    todos = TodoList(_task("shop"), _task("milk"))

    update_todo(todos, "milk", {"parent_uid": "shop"}, as_shown=False)

    assert parent_uid(todos.held()["milk"]) == "shop"
    assert todos.written() == ["milk"]


@pytest.mark.parametrize("parent", ["shop", "milk", "oat"])
def test_a_todo_cannot_be_put_under_itself_or_what_is_below_it(parent: str) -> None:
    todos = TodoList(_task("shop"), _under("milk", "shop"), _under("oat", "milk"))

    with pytest.raises(Refused, match="parent_cycle"):
        update_todo(todos, "shop", {"parent_uid": parent}, as_shown=False)

    assert todos.written() == []


def test_a_parent_that_is_not_on_the_list_is_refused() -> None:
    todos = TodoList(_task("milk"))

    with pytest.raises(Refused, match="parent_not_found"):
        update_todo(todos, "milk", {"parent_uid": "shop"}, as_shown=False)
    with pytest.raises(Refused, match="parent_not_found"):
        create_todo(todos, {"summary": "Eggs", "parent_uid": "shop"}, as_shown=False)

    assert todos.written() == []


def test_a_subtask_taken_from_its_parent_is_a_top_level_item() -> None:
    todos = TodoList(_task("shop"), _under("milk", "shop"))

    update_todo(todos, "milk", {"parent_uid": None}, as_shown=False)

    assert parent_uid(todos.held()["milk"]) is None


def test_a_new_subtask_is_stored_under_its_parent() -> None:
    todos = TodoList(_task("shop"))

    create_todo(todos, {"summary": "Milk", "parent_uid": "shop"}, as_shown=False)

    created = next(
        vtodo for vtodo in todos.held().values() if str(vtodo["SUMMARY"]) == "Milk"
    )
    assert parent_uid(created) == "shop"
    assert todos.statuses()["shop"] == "NEEDS-ACTION"


def test_a_new_open_subtask_reopens_a_completed_parent() -> None:
    todos = TodoList(_task("shop", "STATUS:COMPLETED", "COMPLETED:20260101T120000Z"))

    create_todo(todos, {"summary": "Milk", "parent_uid": "shop"}, as_shown=False)

    assert todos.statuses()["shop"] == "NEEDS-ACTION"
    assert todos.written()[0] == "shop"


def test_a_new_subtask_that_is_done_leaves_a_completed_parent_closed() -> None:
    todos = TodoList(_task("shop", "STATUS:COMPLETED"))

    create_todo(
        todos,
        {"summary": "Milk", "parent_uid": "shop", "status": "COMPLETED"},
        as_shown=False,
    )

    assert todos.statuses()["shop"] == "COMPLETED"


def test_deleting_a_parent_deletes_its_subtasks_ahead_of_it() -> None:
    todos = TodoList(
        _task("shop"), _under("milk", "shop"), _under("oat", "milk"), _task("other")
    )

    delete_todos(todos, ["shop"], {})

    assert [url.rsplit("/", 1)[1] for url in todos.client.deletes] == [
        "oat.ics",
        "milk.ics",
        "shop.ics",
    ]
    assert set(todos.held()) == {"other"}


def test_a_subtask_named_beside_its_parent_is_deleted_once() -> None:
    todos = TodoList(_task("shop"), _under("milk", "shop"))

    delete_todos(todos, ["shop", "milk"], {})

    assert len(todos.client.deletes) == 2
    assert todos.held() == {}


def test_a_subtask_that_changed_on_the_server_keeps_its_parent_from_being_deleted() -> (
    None
):
    todos = TodoList(_task("shop"), _under("milk", "shop"))

    with pytest.raises(Refused, match="etag_conflict"):
        delete_todos(todos, ["shop"], {"shop": '"shop-1"', "milk": '"stale"'})

    assert todos.client.deletes == []


def test_a_cycle_another_client_stored_is_deleted_as_a_whole() -> None:
    todos = TodoList(_under("a", "b"), _under("b", "a"))

    delete_todos(todos, ["a"], {})

    assert todos.held() == {}


def test_deleting_an_item_that_is_gone_names_it() -> None:
    todos = TodoList(_task("shop"))

    with pytest.raises(NotFoundError, match="milk"):
        delete_todos(todos, ["shop", "milk"], {})

    assert todos.client.deletes == []


def _two_lists(*bodies: str) -> tuple[TodoList, TodoList]:
    source = TodoList(*bodies)
    return source, TodoList(client=source.client, name="other")


def test_a_move_takes_the_subtasks_along_and_leaves_the_parent_behind() -> None:
    source, target = _two_lists(
        _task("trip"), _under("shop", "trip"), _under("milk", "shop")
    )

    move_todo(source, target, "shop", keep_original=False)

    assert set(source.held()) == {"trip"}
    moved = target.held()
    assert set(moved) == {"shop", "milk"}
    assert parent_uid(moved["shop"]) is None
    assert parent_uid(moved["milk"]) == "shop"


def test_a_copy_leaves_the_originals_where_they_are() -> None:
    source, target = _two_lists(_task("shop"), _under("milk", "shop"))

    move_todo(source, target, "shop", keep_original=True)

    assert set(source.held()) == {"shop", "milk"}
    assert set(target.held()) == {"shop", "milk"}
    assert source.client.deletes == []


def test_a_move_onto_a_list_that_holds_one_of_the_uids_writes_nothing() -> None:
    source, target = _two_lists(_task("shop"), _under("milk", "shop"))
    target.client.stored[f"{target.url}milk.ics"] = (_task("milk"), '"1"')

    with pytest.raises(Refused, match="uid_clash"):
        move_todo(source, target, "shop", keep_original=False)

    assert source.client.puts == []
    assert set(source.held()) == {"shop", "milk"}


def test_a_move_that_cannot_write_every_copy_takes_them_all_back() -> None:
    source, target = _two_lists(_task("shop"), _under("milk", "shop"))
    source.client.fail_from = 1

    with pytest.raises(PutError):
        move_todo(source, target, "shop", keep_original=False)

    assert target.held() == {}
    assert set(source.held()) == {"shop", "milk"}


def test_moving_an_item_that_is_gone_is_reported() -> None:
    source, target = _two_lists(_task("shop"))

    with pytest.raises(NotFoundError):
        move_todo(source, target, "milk", keep_original=False)
