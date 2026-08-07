"""Tests for the CalDAV to-do list entity."""

from datetime import UTC, date, datetime
from unittest.mock import Mock, patch

from caldav.lib.error import DAVError
from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntityFeature,
)
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_component import EntityComponent
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_caldav.const import CONF_READ_ONLY, DOMAIN

ENTRY_DATA = {
    CONF_URL: "https://cloud.example.com/remote.php/dav",
    CONF_USERNAME: "iven",
    CONF_PASSWORD: "secret",
    CONF_VERIFY_SSL: True,
}


def _calendar(name: str) -> Mock:
    calendar = Mock()
    calendar.name = name
    calendar.url = f"https://cloud.example.com/remote.php/dav/{name}"
    calendar.search.return_value = []
    return calendar


async def _setup(hass: HomeAssistant, options: dict | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iven",
        data=ENTRY_DATA,
        options=options or {},
        unique_id="x",
    )
    entry.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = [
            _calendar("Personal")
        ]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _entity(hass: HomeAssistant):
    component: EntityComponent = hass.data["entity_components"]["todo"]
    return component.get_entity("todo.iven_personal")


async def test_todo_list_per_calendar_with_full_crud(hass: HomeAssistant) -> None:
    await _setup(hass)

    state = hass.states.get("todo.iven_personal")
    assert state is not None

    features = state.attributes["supported_features"]
    assert features & TodoListEntityFeature.CREATE_TODO_ITEM
    assert features & TodoListEntityFeature.UPDATE_TODO_ITEM
    assert features & TodoListEntityFeature.DELETE_TODO_ITEM
    assert features & TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
    assert features & TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM


async def test_read_only_hides_write_features(hass: HomeAssistant) -> None:
    await _setup(hass, options={CONF_READ_ONLY: True})

    assert hass.states.get("todo.iven_personal").attributes["supported_features"] == 0


async def test_create_forwards_mapped_status(hass: HomeAssistant) -> None:
    await _setup(hass)
    entity = _entity(hass)

    with patch("custom_components.ha_caldav.todo.create_todo") as create:
        await entity.async_create_todo_item(
            TodoItem(summary="Buy milk", status=TodoItemStatus.COMPLETED)
        )

    assert create.call_args.args[1]["summary"] == "Buy milk"
    assert create.call_args.args[1]["status"] == "COMPLETED"


async def test_delete_forwards_every_uid(hass: HomeAssistant) -> None:
    await _setup(hass)
    entity = _entity(hass)

    with patch("custom_components.ha_caldav.todo.delete_todos") as delete:
        await entity.async_delete_todo_items(["uid-1", "uid-2"])

    # One call for the whole selection: a lookup per uid is a whole-collection
    # download per item on a server that refuses the uid filter.
    delete.assert_called_once()
    assert delete.call_args.args[1] == ["uid-1", "uid-2"]


async def test_server_error_becomes_home_assistant_error(hass: HomeAssistant) -> None:
    await _setup(hass)
    entity = _entity(hass)

    with (
        patch(
            "custom_components.ha_caldav.todo.delete_todos",
            side_effect=DAVError(
                "500 Server Error at 'https://cloud.example.com/dav/iven/personal/'"
            ),
        ),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await entity.async_delete_todo_items(["uid-1"])

    assert "cloud.example.com" not in str(raised.value)


def _dav_todo(body: str) -> Mock:
    """Build a caldav-like to-do whose component is a real icalendar VTODO."""
    from icalendar import Calendar as ICalCalendar

    instance = ICalCalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        f"BEGIN:VTODO\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\n{body}\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    )
    todo = Mock()
    todo.icalendar_instance = instance
    todo.icalendar_component = next(iter(instance.walk("VTODO")))
    return todo


def test_completing_stamps_the_completion_properties() -> None:
    from custom_components.ha_caldav.api import update_todo

    calendar = Mock()
    todo = _dav_todo("SUMMARY:Buy milk\r\nSTATUS:NEEDS-ACTION")
    with patch("custom_components.ha_caldav.api.object_by_uid", return_value=todo):
        update_todo(calendar, "uid-1", {"summary": "Buy milk", "status": "COMPLETED"})

    component = todo.icalendar_component
    assert str(component["STATUS"]) == "COMPLETED"
    assert int(component["PERCENT-COMPLETE"]) == 100
    assert "COMPLETED" in component


def test_reopening_clears_the_completion_properties() -> None:
    from custom_components.ha_caldav.api import update_todo

    calendar = Mock()
    todo = _dav_todo(
        "SUMMARY:Buy milk\r\nSTATUS:COMPLETED\r\n"
        "COMPLETED:20260101T120000Z\r\nPERCENT-COMPLETE:100"
    )
    with patch("custom_components.ha_caldav.api.object_by_uid", return_value=todo):
        update_todo(
            calendar, "uid-1", {"summary": "Buy milk", "status": "NEEDS-ACTION"}
        )

    component = todo.icalendar_component
    assert str(component["STATUS"]) == "NEEDS-ACTION"
    assert "COMPLETED" not in component
    assert "PERCENT-COMPLETE" not in component


def test_completion_time_is_read_back_onto_the_item() -> None:
    import vobject

    from custom_components.ha_caldav.coordinator import to_todo

    vtodo = vobject.readOne(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VTODO\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:Done\r\n"
        "STATUS:COMPLETED\r\nCOMPLETED:20260101T120000Z\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    ).vtodo

    item = to_todo(vtodo)
    assert item.status == TodoItemStatus.COMPLETED
    assert item.completed is not None
    assert item.completed.astimezone(UTC) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_sort_order_puts_unordered_items_last() -> None:
    import vobject

    from custom_components.ha_caldav.coordinator import sort_order

    def vtodo(body: str):
        return vobject.readOne(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
            f"BEGIN:VTODO\r\nUID:x\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:x\r\n"
            f"{body}\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
        ).vtodo

    ordered = sort_order(vtodo("X-APPLE-SORT-ORDER:2"))
    unordered = sort_order(vtodo("LOCATION:here"))
    unparseable = sort_order(vtodo("X-APPLE-SORT-ORDER:nonsense"))

    assert ordered < unordered
    assert unparseable == unordered


def test_reorder_writes_nothing_when_the_order_already_holds() -> None:
    """The positions need not be dense, only in the requested order."""
    from custom_components.ha_caldav.api import reorder_todos

    todos = {
        "a": _dav_todo("SUMMARY:a\r\nX-APPLE-SORT-ORDER:0"),
        "b": _dav_todo("SUMMARY:b\r\nX-APPLE-SORT-ORDER:7"),
    }
    for uid, todo in todos.items():
        todo.icalendar_component["UID"] = uid
    calendar = Mock()
    calendar.search.return_value = list(todos.values())

    reorder_todos(calendar, ["a", "b"])

    todos["a"].save.assert_not_called()
    todos["b"].save.assert_not_called()


async def test_move_reorders_around_the_previous_item(hass: HomeAssistant) -> None:
    await _setup(hass)
    entity = _entity(hass)
    entity.coordinator.data.todos = [
        TodoItem(uid="a", summary="a"),
        TodoItem(uid="b", summary="b"),
        TodoItem(uid="c", summary="c"),
    ]

    with patch("custom_components.ha_caldav.todo.reorder_todos") as reorder:
        await entity.async_move_todo_item("c", previous_uid="a")

    assert reorder.call_args.args[1] == ["a", "c", "b"]


async def test_move_without_a_previous_item_goes_to_the_front(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)
    entity = _entity(hass)
    entity.coordinator.data.todos = [
        TodoItem(uid="a", summary="a"),
        TodoItem(uid="b", summary="b"),
    ]

    with patch("custom_components.ha_caldav.todo.reorder_todos") as reorder:
        await entity.async_move_todo_item("b")

    assert reorder.call_args.args[1] == ["b", "a"]


async def test_moving_an_item_that_is_not_on_the_list_is_refused(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)
    entity = _entity(hass)
    entity.coordinator.data.todos = [
        TodoItem(uid="a", summary="a"),
        TodoItem(uid="b", summary="b"),
    ]

    with pytest.raises(ServiceValidationError) as refusal:
        await entity.async_move_todo_item("nope")

    assert refusal.value.translation_key == "unknown_todo_item"


async def test_moving_behind_an_item_that_is_gone_is_refused(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)
    entity = _entity(hass)
    entity.coordinator.data.todos = [
        TodoItem(uid="a", summary="a"),
        TodoItem(uid="b", summary="b"),
    ]

    with pytest.raises(ServiceValidationError) as refusal:
        await entity.async_move_todo_item("a", previous_uid="deleted")

    assert refusal.value.translation_key == "unknown_todo_item"


async def test_dropping_an_item_where_it_already_is_writes_nothing(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)
    entity = _entity(hass)
    entity.coordinator.data.todos = [
        TodoItem(uid="a", summary="a"),
        TodoItem(uid="b", summary="b"),
    ]

    with patch("custom_components.ha_caldav.todo.reorder_todos") as reorder:
        await entity.async_move_todo_item("b", previous_uid="b")

    reorder.assert_not_called()


async def test_move_is_offered_as_a_feature(hass: HomeAssistant) -> None:
    await _setup(hass)

    features = hass.states.get("todo.iven_personal").attributes["supported_features"]
    assert features & TodoListEntityFeature.MOVE_TODO_ITEM


async def test_the_stored_order_survives_a_poll(hass: HomeAssistant) -> None:
    import vobject

    def resource(summary: str, position: str | None) -> Mock:
        order = f"X-APPLE-SORT-ORDER:{position}\r\n" if position else ""
        item = Mock()
        item.vobject_instance = vobject.readOne(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
            f"BEGIN:VTODO\r\nUID:{summary}\r\nDTSTAMP:20260101T000000Z\r\n"
            f"SUMMARY:{summary}\r\n{order}END:VTODO\r\nEND:VCALENDAR\r\n"
        )
        item.props = {}
        return item

    calendar = _calendar("Personal")
    # Returned in the wrong order, with one item never reordered at all.
    calendar.search.side_effect = lambda **kwargs: (
        [resource("second", "1"), resource("unsorted", None), resource("first", "0")]
        if kwargs.get("todo")
        else []
    )
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = [calendar]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    summaries = [item.summary for item in _entity(hass).todo_items]
    assert summaries == ["first", "second", "unsorted"]


async def test_updating_an_item_forwards_its_etag_and_clears_it(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)
    entity = _entity(hass)
    entity.coordinator.todo_etags = {"uid-1": '"e"'}

    with patch("custom_components.ha_caldav.todo.update_todo") as update:
        await entity.async_update_todo_item(
            TodoItem(uid="uid-1", summary="Buy milk", status=TodoItemStatus.COMPLETED)
        )

    assert update.call_args.args[1] == "uid-1"
    assert update.call_args.args[2]["status"] == "COMPLETED"
    assert update.call_args.args[3] == '"e"'
    assert entity.coordinator.todo_etags == {}


async def test_deleting_an_item_forwards_its_etag(hass: HomeAssistant) -> None:
    await _setup(hass)
    entity = _entity(hass)
    entity.coordinator.todo_etags = {"uid-1": '"e"'}

    with patch("custom_components.ha_caldav.todo.delete_todos") as delete:
        await entity.async_delete_todo_items(["uid-1"])

    assert delete.call_args.args[2] == {"uid-1": '"e"'}


async def test_the_poll_caches_an_etag_for_every_item(hass: HomeAssistant) -> None:
    calendar = _calendar("Personal")
    from caldav.elements import dav
    import vobject

    resource = Mock()
    resource.vobject_instance = vobject.readOne(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VTODO\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:Buy milk\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    )
    resource.props = {dav.GetEtag.tag: '"etag-1"'}
    calendar.search.side_effect = lambda **kwargs: (
        [resource] if kwargs.get("todo") else []
    )

    await _setup_with(hass, calendar)

    # Without this the conflict check never fires for a to-do in practice, and
    # no test setting the cache by hand would notice.
    assert _entity(hass).coordinator.todo_etags == {"uid-1": '"etag-1"'}


async def _setup_with(hass: HomeAssistant, calendar: Mock):
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    with patch("custom_components.ha_caldav.caldav.DAVClient") as client:
        client.return_value.principal.return_value.calendars.return_value = [calendar]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_completing_a_recurring_task_keeps_a_rename_from_the_same_call(
    hass: HomeAssistant,
) -> None:
    from custom_components.ha_caldav.api import update_todo

    todo = _vtodo_resource(
        "UID:uid-1\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:Water plants\r\n"
        "DUE;VALUE=DATE:20260710\r\nRRULE:FREQ=WEEKLY\r\n"
    )
    calendar = Mock()
    calendar.todo_by_uid.return_value = todo

    update_todo(
        calendar,
        "uid-1",
        {"summary": "Water the plants", "status": "COMPLETED", "due": None},
    )

    # The roll owns the due date; the rename in the same call is not its to drop.
    assert str(todo.icalendar_component["SUMMARY"]) == "Water the plants"
    assert todo.icalendar_component["DUE"].dt.isoformat() == "2026-07-17"


def _vtodo_resource(body: str) -> Mock:
    """Return a caldav-like resource carrying both shapes, as a real one does."""
    from icalendar import Calendar as ICalCalendar

    document = ICalCalendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        f"BEGIN:VTODO\r\n{body}END:VTODO\r\nEND:VCALENDAR\r\n"
    )
    todo = Mock()
    todo.icalendar_instance = document
    todo.icalendar_component = next(iter(document.walk("VTODO")))
    return todo


async def test_a_new_item_carries_its_due_date_and_description(
    hass: HomeAssistant,
) -> None:
    await _setup(hass)

    with patch("custom_components.ha_caldav.todo.create_todo") as create:
        await hass.services.async_call(
            "todo",
            "add_item",
            {
                "entity_id": "todo.iven_personal",
                "item": "Buy milk",
                "due_date": "2026-07-10",
                "description": "Two litres",
            },
            blocking=True,
        )

    data = create.call_args.args[1]
    assert data["summary"] == "Buy milk"
    assert data["due"] == date(2026, 7, 10)
    assert data["description"] == "Two litres"


async def test_deleting_an_item_clears_its_etag_after_the_write(
    hass: HomeAssistant,
) -> None:
    # Keeping it flags the next write against a reused uid as a conflict that
    # never happened. The calendar half asserts both; this one only asserted
    # that the etag went out.
    await _setup(hass)
    entity = _entity(hass)
    entity.coordinator.todo_etags = {"uid-1": '"e"'}

    with patch("custom_components.ha_caldav.todo.delete_todos"):
        await entity.async_delete_todo_items(["uid-1"])

    assert "uid-1" not in entity.coordinator.todo_etags


async def test_renaming_an_item_does_not_reopen_it(hass: HomeAssistant) -> None:
    # A rename names no status; sending one anyway resolves to NEEDS-ACTION
    # and quietly un-completes a finished task.
    await _setup(hass)
    entity = _entity(hass)

    with patch("custom_components.ha_caldav.todo.update_todo") as update:
        await entity.async_update_todo_item(TodoItem(uid="uid-1", summary="Renamed"))

    assert "status" not in update.call_args.args[2]


async def test_a_reorder_clears_the_etags_of_everything_it_writes(
    hass: HomeAssistant,
) -> None:
    """The moved item is written, and so is every other one whenever the list
    has to be renumbered, which is the first drag of any list the server never
    numbered. The refresh behind the write is debounced by ten seconds, so the
    next tick of one of them was refused as a conflict the user made
    themselves."""
    await _setup(hass)
    entity = _entity(hass)
    entity.coordinator.data.todos = [
        TodoItem(uid="a", summary="A"),
        TodoItem(uid="b", summary="B"),
        TodoItem(uid="c", summary="C"),
    ]
    entity.coordinator.todo_etags = {"a": '"e1"', "b": '"e2"', "c": '"e3"'}

    with patch("custom_components.ha_caldav.todo.reorder_todos"):
        await entity.async_move_todo_item("c", previous_uid=None)

    assert entity.coordinator.todo_etags == {}


async def test_the_etag_a_write_invalidates_is_dropped_under_the_lock(
    hass: HomeAssistant,
) -> None:
    """A poll merges into the same cache from an executor thread. Without the
    lock, a merge that read its etags before the PUT landed can put the stale
    one back after the pop, and the next edit of that object is refused over a
    conflict the user caused themselves seconds earlier."""
    import threading

    await _setup(hass)
    entity = _entity(hass)
    entity.coordinator.todo_etags = {"uid-1": '"e"'}
    held: list[bool] = []

    class Recording:
        def __init__(self) -> None:
            self._lock = threading.Lock()

        def __enter__(self):
            held.append(True)
            return self._lock.__enter__()

        def __exit__(self, *args):
            return self._lock.__exit__(*args)

    entity.coordinator.etag_lock = Recording()

    with patch("custom_components.ha_caldav.todo.update_todo"):
        await entity.async_update_todo_item(
            TodoItem(uid="uid-1", summary="Buy milk", status=TodoItemStatus.COMPLETED)
        )

    assert held, "the cache was edited without holding the lock a poll merges under"
    assert "uid-1" not in entity.coordinator.todo_etags
