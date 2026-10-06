from datetime import date
from unittest.mock import Mock, patch

from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import vobject
import voluptuous as vol

from custom_components.ha_caldav.capability import Capability
from custom_components.ha_caldav.const import CONF_READ_ONLY, DOMAIN

ENTRY_DATA = {
    CONF_URL: "https://cloud.example.com/remote.php/dav",
    CONF_USERNAME: "iven",
    CONF_PASSWORD: "secret",
    CONF_VERIFY_SSL: True,
}

ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VTODO\r\nUID:uid-1\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:Milk\r\n"
    "END:VTODO\r\nEND:VCALENDAR\r\n"
)


def _item(uid: str, summary: str, *lines: str) -> Mock:
    item = Mock()
    item.vobject_instance = vobject.readOne(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        f"BEGIN:VTODO\r\nUID:{uid}\r\nDTSTAMP:20260101T000000Z\r\n"
        f"SUMMARY:{summary}\r\n"
        + "".join(f"{line}\r\n" for line in lines)
        + "END:VTODO\r\nEND:VCALENDAR\r\n"
    )
    item.props = {}
    return item


def _todo_list(name: str, *items: Mock) -> Mock:
    calendar = Mock()
    calendar.name = name
    calendar.url = f"https://cloud.example.com/remote.php/dav/{name}"
    calendar.search.side_effect = lambda **kind: list(items) if kind.get("todo") else []
    return calendar


async def _setup(
    hass: HomeAssistant,
    calendars: list[Mock],
    options: dict | None = None,
    todos_only: tuple[str, ...] = (),
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="iven",
        data=ENTRY_DATA,
        options=options or {},
        unique_id="x",
    )
    entry.add_to_hass(hass)
    capabilities = {
        f"/remote.php/dav/{name}": Capability(frozenset({"VTODO"}), True)
        for name in todos_only
    }
    with (
        patch("custom_components.ha_caldav.caldav.DAVClient") as client,
        patch(
            "custom_components.ha_caldav.fetch_capabilities", return_value=capabilities
        ),
    ):
        client.return_value.principal.return_value.calendars.return_value = calendars
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _groceries() -> Mock:
    return _todo_list(
        "Personal",
        _item("g1", "Groceries", "X-APPLE-SORT-ORDER:1"),
        _item(
            "m1",
            "Milk",
            "RELATED-TO:g1",
            "STATUS:COMPLETED",
            "COMPLETED:20260101T120000Z",
            "X-APPLE-SORT-ORDER:2",
        ),
        _item(
            "l1",
            "Laundry",
            "STATUS:IN-PROCESS",
            "PERCENT-COMPLETE:40",
            "DUE;VALUE=DATE:20260710",
            "CATEGORIES:home",
            "X-APPLE-SORT-ORDER:3",
        ),
    )


async def test_create_todo_forwards_its_fields_as_the_write_names_them(
    hass: HomeAssistant,
) -> None:
    calendar = _groceries()
    await _setup(hass, [calendar])

    with patch("custom_components.ha_caldav.todo.create_todo") as create:
        await hass.services.async_call(
            DOMAIN,
            "create_todo",
            {
                "entity_id": "todo.personal",
                "item": "Eggs",
                "due_date": "2026-08-06",
                "start_date": "2026-08-05",
                "parent": "Groceries",
                "status": "in_process",
                "percent_complete": 40,
                "priority": 1,
                "categories": ["home"],
                "alarms": [15],
            },
            blocking=True,
        )

    assert create.call_args.args[0] is calendar
    assert create.call_args.args[1] == {
        "summary": "Eggs",
        "due": date(2026, 8, 6),
        "start": date(2026, 8, 5),
        "parent_uid": "g1",
        "status": "IN-PROCESS",
        "percent_complete": 40,
        "priority": 1,
        "categories": ["home"],
        "alarms": [15],
    }
    # Not core's item, whose status knows two states.
    assert create.call_args.args[3] is False


async def test_a_due_time_is_taken_in_home_assistants_own_zone(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_groceries()])

    with patch("custom_components.ha_caldav.todo.create_todo") as create:
        await hass.services.async_call(
            DOMAIN,
            "create_todo",
            {
                "entity_id": "todo.personal",
                "item": "Call back",
                "due_datetime": "2026-08-06 18:00:00",
            },
            blocking=True,
        )

    due = create.call_args.args[1]["due"]
    assert due.tzinfo is not None
    assert dt_util.as_local(due).hour == 18


async def test_update_todo_finds_the_item_by_name_and_forwards_only_what_was_named(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_groceries()])

    with patch("custom_components.ha_caldav.todo.update_todo") as update:
        await hass.services.async_call(
            DOMAIN,
            "update_todo",
            {"entity_id": "todo.personal", "item": "Laundry", "rename": "Washing"},
            blocking=True,
        )

    assert update.call_args.args[1] == "l1"
    assert update.call_args.args[2] == {"summary": "Washing"}
    assert update.call_args.kwargs == {"as_shown": False}


async def test_a_field_given_empty_is_cleared(hass: HomeAssistant) -> None:
    await _setup(hass, [_groceries()])

    with patch("custom_components.ha_caldav.todo.update_todo") as update:
        await hass.services.async_call(
            DOMAIN,
            "update_todo",
            {
                "entity_id": "todo.personal",
                "item": "m1",
                "due_date": None,
                "description": "",
                "parent": "",
                "rrule": "",
                "url": "",
            },
            blocking=True,
        )

    assert update.call_args.args[1] == "m1"
    assert update.call_args.args[2] == {
        "due": None,
        "description": None,
        "parent_uid": None,
        "rrule": None,
        "url": None,
    }


async def test_an_action_is_not_held_to_the_etag_the_list_cached(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_groceries()])
    entity = hass.data["entity_components"]["todo"].get_entity("todo.personal")
    entity.coordinator.todo_etags = {"l1": '"cached"'}

    with patch("custom_components.ha_caldav.todo.update_todo") as update:
        await hass.services.async_call(
            DOMAIN,
            "update_todo",
            {"entity_id": "todo.personal", "item": "l1", "priority": 1},
            blocking=True,
        )

    assert update.call_args.args[3] is None
    assert entity.coordinator.todo_etags == {}


async def test_a_name_the_list_does_not_show_is_taken_for_a_uid(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_groceries()])

    with patch("custom_components.ha_caldav.todo.update_todo") as update:
        await hass.services.async_call(
            DOMAIN,
            "update_todo",
            {"entity_id": "todo.personal", "item": "just-created", "priority": 1},
            blocking=True,
        )

    assert update.call_args.args[1] == "just-created"


@pytest.mark.parametrize(
    ("given", "written"),
    [
        ("needs_action", "NEEDS-ACTION"),
        ("IN-PROCESS", "IN-PROCESS"),
        ("Completed", "COMPLETED"),
        ("cancelled", "CANCELLED"),
    ],
)
async def test_a_status_is_taken_as_core_or_as_rfc_5545_spells_it(
    hass: HomeAssistant, given: str, written: str
) -> None:
    await _setup(hass, [_groceries()])

    with patch("custom_components.ha_caldav.todo.update_todo") as update:
        await hass.services.async_call(
            DOMAIN,
            "update_todo",
            {"entity_id": "todo.personal", "item": "l1", "status": given},
            blocking=True,
        )

    assert update.call_args.args[2] == {"status": written}


@pytest.mark.parametrize(
    "data",
    [
        {"item": "l1"},
        {"item": "l1", "due_date": "2026-08-06", "due_datetime": "2026-08-06 18:00"},
        {"item": "l1", "start_date": "2026-08-06 18:00:00"},
        {"item": "l1", "status": "paused"},
        {"item": "l1", "percent_complete": 101},
        {"item": "", "priority": 1},
    ],
)
async def test_an_update_the_schema_cannot_place_is_refused(
    hass: HomeAssistant, data: dict
) -> None:
    await _setup(hass, [_groceries()])

    with (
        patch("custom_components.ha_caldav.todo.update_todo") as update,
        pytest.raises(vol.Invalid),
    ):
        await hass.services.async_call(
            DOMAIN, "update_todo", {"entity_id": "todo.personal", **data}, blocking=True
        )

    update.assert_not_called()


async def _search(hass: HomeAssistant, **data) -> list[dict]:
    result = await hass.services.async_call(
        DOMAIN,
        "search_todos",
        {"entity_id": "todo.personal", **data},
        blocking=True,
        return_response=True,
    )
    return result["todo.personal"]["todos"]


async def test_search_returns_every_item_with_what_is_stored_on_it(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, [_groceries()])

    todos = await _search(hass)

    assert todos[0] == {"uid": "g1", "summary": "Groceries", "status": "needs_action"}
    assert todos[1]["parent_uid"] == "g1"
    assert todos[1]["status"] == "completed"
    assert todos[1]["completed"].startswith("2026-01-01T")
    assert todos[2] == {
        "uid": "l1",
        "summary": "Laundry",
        "status": "in_process",
        "due": "2026-07-10",
        "percent_complete": 40,
        "categories": ["home"],
    }


@pytest.mark.parametrize(
    ("criteria", "found"),
    [
        ({"text": "MILK"}, ["Milk"]),
        ({"text": "home", "field": "category"}, ["Laundry"]),
        ({"text": "g1", "field": "uid"}, ["Groceries"]),
        ({"text": "g", "field": "uid"}, []),
        ({"status": ["in_process", "completed"]}, ["Milk", "Laundry"]),
        ({"status": "needs_action", "text": "o"}, ["Groceries"]),
    ],
)
async def test_search_filters_by_text_and_by_status(
    hass: HomeAssistant, criteria: dict, found: list[str]
) -> None:
    await _setup(hass, [_groceries()])

    todos = await _search(hass, **criteria)

    assert [todo["summary"] for todo in todos] == found


async def test_move_todo_writes_to_the_target_list_and_refreshes_it(
    hass: HomeAssistant,
) -> None:
    source, target = _groceries(), _todo_list("Work")
    entry = await _setup(hass, [source, target])
    there = next(item for item in entry.runtime_data.calendars if item.name == "Work")

    with (
        patch("custom_components.ha_caldav.services.move_todo") as move,
        patch.object(there.coordinator, "async_refresh") as refresh,
    ):
        await hass.services.async_call(
            DOMAIN,
            "move_todo",
            {
                "entity_id": "todo.personal",
                "item": "Groceries",
                "target_entity_id": "todo.work",
            },
            blocking=True,
        )

    assert move.call_args.args == (source, target, "g1", False)
    refresh.assert_called_once()


@pytest.mark.parametrize(
    ("target", "key"),
    [
        ("todo.personal", "same_todo_list"),
        ("calendar.work", "unknown_todo_list"),
        ("todo.elsewhere", "unknown_todo_list"),
    ],
)
async def test_a_move_to_anything_but_another_list_of_ours_is_refused(
    hass: HomeAssistant, target: str, key: str
) -> None:
    await _setup(hass, [_groceries(), _todo_list("Work")])

    with (
        patch("custom_components.ha_caldav.services.move_todo") as move,
        pytest.raises(ServiceValidationError) as refusal,
    ):
        await hass.services.async_call(
            DOMAIN,
            "move_todo",
            {"entity_id": "todo.personal", "item": "g1", "target_entity_id": target},
            blocking=True,
        )

    assert refusal.value.translation_key == key
    move.assert_not_called()


async def test_a_read_only_list_refuses_every_write_action(hass: HomeAssistant) -> None:
    await _setup(
        hass, [_groceries(), _todo_list("Work")], options={CONF_READ_ONLY: True}
    )

    for service, data in (
        ("create_todo", {"item": "x"}),
        ("update_todo", {"item": "g1", "priority": 1}),
        ("move_todo", {"item": "g1", "target_entity_id": "todo.work"}),
    ):
        with pytest.raises(ServiceValidationError) as refusal:
            await hass.services.async_call(
                DOMAIN, service, {"entity_id": "todo.personal", **data}, blocking=True
            )
        assert refusal.value.translation_key == "read_only", service


async def test_a_list_that_holds_no_events_is_exported_through_its_todo_entity(
    hass: HomeAssistant,
) -> None:
    tasks = _todo_list("Tasks")
    await _setup(hass, [tasks], todos_only=("Tasks",))
    assert hass.states.get("calendar.tasks") is None

    with patch(
        "custom_components.ha_caldav.services.export_ics", return_value=ICS
    ) as export:
        result = await hass.services.async_call(
            DOMAIN,
            "export_ics",
            {"entity_id": "todo.tasks"},
            blocking=True,
            return_response=True,
        )

    assert result["todo.tasks"]["ics"] == ICS
    assert export.call_args.args == (tasks, None)


async def test_a_list_that_holds_no_events_takes_an_import_and_a_color(
    hass: HomeAssistant,
) -> None:
    tasks = _todo_list("Tasks")
    entry = await _setup(hass, [tasks], todos_only=("Tasks",))

    with (
        patch("custom_components.ha_caldav.services.import_ics") as importer,
        patch("custom_components.ha_caldav.services.set_calendar_color") as setter,
        patch.object(entry.runtime_data.colors, "async_request_refresh") as refresh,
    ):
        await hass.services.async_call(
            DOMAIN, "import_ics", {"entity_id": "todo.tasks", "ics": ICS}, blocking=True
        )
        await hass.services.async_call(
            DOMAIN,
            "set_calendar_color",
            {"entity_id": "todo.tasks", "color": "#00679e"},
            blocking=True,
        )

    assert importer.call_args.args == (tasks, ICS)
    assert setter.call_args.args == (tasks, "#00679e")
    refresh.assert_called_once()


async def test_a_calendar_that_holds_both_is_exported_once(hass: HomeAssistant) -> None:
    """A call naming an area or a label reaches both entities of one collection."""
    await _setup(hass, [_groceries()])
    assert hass.states.get("calendar.personal") is not None

    with patch(
        "custom_components.ha_caldav.services.export_ics", return_value=ICS
    ) as export:
        result = await hass.services.async_call(
            DOMAIN,
            "export_ics",
            {"entity_id": ["calendar.personal", "todo.personal"]},
            blocking=True,
            return_response=True,
        )

    assert list(result) == ["calendar.personal"]
    export.assert_called_once()


async def test_the_todo_actions_exist_while_the_account_retries(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="iven", data=ENTRY_DATA, unique_id="x")
    entry.add_to_hass(hass)
    with patch(
        "custom_components.ha_caldav.caldav.DAVClient", side_effect=OSError("down")
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    for service in ("create_todo", "update_todo", "search_todos", "move_todo"):
        assert hass.services.has_service(DOMAIN, service), service
