from datetime import UTC, date, datetime

from icalendar import Calendar as ICalCalendar
import pytest
import vobject

from custom_components.ha_caldav.errors import Refused
from custom_components.ha_caldav.task import (
    Tree,
    apply_fields,
    parent_of,
    parent_uid,
    read_todo_extras,
    set_parent,
    set_status,
    status_of,
)


def _document(*lines: str) -> str:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VTODO\r\n"
        "UID:milk\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:Milk\r\n"
        + "".join(f"{line}\r\n" for line in lines)
        + "END:VTODO\r\nEND:VCALENDAR\r\n"
    )


def _written(*lines: str):
    return next(iter(ICalCalendar.from_ical(_document(*lines)).walk("VTODO")))


def _read(*lines: str):
    return vobject.readOne(_document(*lines)).vtodo


def _stored(vtodo) -> str:
    return vtodo.to_ical().decode("utf-8")


@pytest.mark.parametrize("line", ["RELATED-TO:shop", "RELATED-TO;RELTYPE=PARENT:shop"])
def test_a_relation_without_a_type_names_the_parent_too(line: str) -> None:
    """RFC 5545 3.2.15 makes PARENT the default, which Nextcloud Tasks relies on."""
    assert parent_of(_read(line)) == "shop"
    assert parent_uid(_written(line)) == "shop"


def test_a_sibling_or_a_child_is_not_taken_for_the_parent() -> None:
    lines = ("RELATED-TO;RELTYPE=SIBLING:eggs", "RELATED-TO;RELTYPE=CHILD:oat")

    assert parent_of(_read(*lines)) is None
    assert parent_uid(_written(*lines)) is None


def test_a_new_parent_replaces_the_old_one_and_leaves_the_other_relations() -> None:
    vtodo = _written(
        "RELATED-TO;RELTYPE=SIBLING:eggs", "RELATED-TO;RELTYPE=PARENT:shop"
    )

    set_parent(vtodo, "market")

    stored = _stored(vtodo)
    assert "RELATED-TO;RELTYPE=PARENT:market" in stored
    assert "RELATED-TO;RELTYPE=SIBLING:eggs" in stored
    assert "shop" not in stored


def test_a_first_parent_is_written_the_way_nextcloud_tasks_writes_one() -> None:
    vtodo = _written()

    set_parent(vtodo, "shop")

    assert "RELATED-TO:shop" in _stored(vtodo)


def test_taking_the_parent_away_leaves_a_top_level_item() -> None:
    vtodo = _written("RELATED-TO:shop", "RELATED-TO;RELTYPE=SIBLING:eggs")

    set_parent(vtodo, None)

    assert parent_uid(vtodo) is None
    assert "RELATED-TO;RELTYPE=SIBLING:eggs" in _stored(vtodo)


def test_a_status_the_server_does_not_know_reads_as_not_started() -> None:
    assert status_of(_read()) == "NEEDS-ACTION"
    assert status_of(_read("STATUS:in-process")) == "IN-PROCESS"
    assert status_of(_read("STATUS:PAUSED")) == "NEEDS-ACTION"


def test_starting_a_completed_item_takes_its_completion_back() -> None:
    vtodo = _written(
        "STATUS:COMPLETED", "COMPLETED:20260101T120000Z", "PERCENT-COMPLETE:100"
    )

    set_status(vtodo, "IN-PROCESS")

    assert str(vtodo["STATUS"]) == "IN-PROCESS"
    assert "COMPLETED" not in vtodo
    assert "PERCENT-COMPLETE" not in vtodo


def test_canceling_an_item_keeps_how_far_it_got() -> None:
    vtodo = _written("STATUS:IN-PROCESS", "PERCENT-COMPLETE:40")

    set_status(vtodo, "CANCELLED")

    assert str(vtodo["STATUS"]) == "CANCELLED"
    assert int(vtodo["PERCENT-COMPLETE"]) == 40


def test_the_two_states_core_has_do_not_overwrite_the_four_stored() -> None:
    started = _written("STATUS:IN-PROCESS", "PERCENT-COMPLETE:40")
    canceled = _written("STATUS:CANCELLED")

    set_status(started, "NEEDS-ACTION", folded=True)
    set_status(canceled, "COMPLETED", folded=True)

    assert str(started["STATUS"]) == "IN-PROCESS"
    assert str(canceled["STATUS"]) == "CANCELLED"
    assert "COMPLETED" not in canceled


@pytest.mark.parametrize(
    ("percent", "status", "kept"),
    [(100, "COMPLETED", 100), (40, "IN-PROCESS", 40), (0, "NEEDS-ACTION", None)],
)
def test_a_percentage_sets_the_status_that_goes_with_it(percent, status, kept) -> None:
    vtodo = _written("STATUS:IN-PROCESS", "PERCENT-COMPLETE:10")

    apply_fields(vtodo, {"percent_complete": percent})

    assert str(vtodo["STATUS"]) == status
    assert ("COMPLETED" in vtodo) == (status == "COMPLETED")
    if kept is None:
        assert "PERCENT-COMPLETE" not in vtodo
    else:
        assert int(vtodo["PERCENT-COMPLETE"]) == kept


def test_a_status_given_beside_a_percentage_wins() -> None:
    vtodo = _written()

    apply_fields(vtodo, {"percent_complete": 40, "status": "CANCELLED"})

    assert str(vtodo["STATUS"]) == "CANCELLED"
    assert int(vtodo["PERCENT-COMPLETE"]) == 40


def test_only_the_named_fields_are_touched() -> None:
    vtodo = _written(
        "DUE;VALUE=DATE:20260710", "DESCRIPTION:notes", "LOCATION:Desk", "PRIORITY:1"
    )

    apply_fields(vtodo, {"location": None, "priority": 5})

    assert vtodo["DUE"].dt == date(2026, 7, 10)
    assert str(vtodo["DESCRIPTION"]) == "notes"
    assert "LOCATION" not in vtodo
    assert int(vtodo["PRIORITY"]) == 5


def test_a_start_of_another_kind_than_the_due_date_is_refused() -> None:
    """RFC 5545 3.8.2.3 has DUE share the value type of DTSTART."""
    vtodo = _written("DUE;VALUE=DATE:20260710")

    with pytest.raises(Refused, match="mixed_time_types"):
        apply_fields(vtodo, {"start": datetime(2026, 7, 6, 9, 0, tzinfo=UTC)})


def test_a_start_after_the_due_date_is_refused() -> None:
    vtodo = _written("DUE;VALUE=DATE:20260710")

    with pytest.raises(Refused, match="end_before_start"):
        apply_fields(vtodo, {"start": date(2026, 7, 11)})


def test_a_rule_is_counted_from_the_due_date_where_there_is_no_start() -> None:
    vtodo = _written("DUE;VALUE=DATE:20260710")

    apply_fields(vtodo, {"rrule": "FREQ=WEEKLY;UNTIL=20260801T000000Z"})

    assert "RRULE:FREQ=WEEKLY;UNTIL=20260801" in _stored(vtodo).replace("\r\n ", "")


def test_a_rule_on_an_item_without_any_date_is_refused() -> None:
    with pytest.raises(Refused, match="rrule_needs_date"):
        apply_fields(_written(), {"rrule": "FREQ=WEEKLY"})


def test_an_empty_rule_takes_the_recurrence_away() -> None:
    vtodo = _written("DUE;VALUE=DATE:20260710", "RRULE:FREQ=WEEKLY")

    apply_fields(vtodo, {"rrule": None})

    assert "RRULE" not in vtodo


def test_a_reminder_on_an_item_with_only_a_due_date_counts_from_it() -> None:
    """RFC 5545 3.8.6.3: without RELATED=END the offset is from DTSTART, which a
    to-do need not have."""
    vtodo = _written("DUE:20260710T150000Z")

    apply_fields(vtodo, {"alarms": [15]})

    assert "TRIGGER;RELATED=END:-PT15M" in _stored(vtodo)


def test_a_reminder_on_an_item_with_a_start_counts_from_the_start() -> None:
    vtodo = _written("DTSTART:20260710T090000Z", "DUE:20260710T150000Z")

    apply_fields(vtodo, {"alarms": [15]})

    assert "TRIGGER:-PT15M" in _stored(vtodo)


def test_a_reminder_on_an_item_without_any_date_is_refused() -> None:
    with pytest.raises(Refused, match="alarm_needs_date"):
        apply_fields(_written(), {"alarms": [15]})


def test_an_alarm_at_a_fixed_time_needs_no_date_to_count_from() -> None:
    vtodo = _written()

    apply_fields(vtodo, {"alarms": [{"at": datetime(2026, 7, 6, 8, 0, tzinfo=UTC)}]})

    assert "TRIGGER;VALUE=DATE-TIME:20260706T080000Z" in _stored(vtodo)


def test_a_to_do_takes_attachments_and_reports_them_back() -> None:
    vtodo = _written()

    apply_fields(vtodo, {"attachments": ["https://example.com/recipe.pdf"]})

    assert "ATTACH:https://example.com/recipe.pdf" in _stored(vtodo)
    document = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        f"{_stored(vtodo)}END:VCALENDAR\r\n"
    )
    assert read_todo_extras(vobject.readOne(document).vtodo)["attachments"] == [
        {"url": "https://example.com/recipe.pdf"}
    ]


def test_assigning_a_todo_names_the_account_as_its_organizer() -> None:
    vtodo = _written()

    apply_fields(vtodo, {"attendees": ["ann@example.com"]}, "me@example.com")

    stored = _stored(vtodo).replace("\r\n ", "")
    assert "ORGANIZER:mailto:me@example.com" in stored
    assert "mailto:ann@example.com" in stored


def test_the_extras_of_a_todo_are_read_back() -> None:
    extras = read_todo_extras(
        _read(
            "RELATED-TO:shop",
            "DTSTART;VALUE=DATE:20260706",
            "PERCENT-COMPLETE:40",
            "LOCATION:Desk",
            "RRULE:FREQ=WEEKLY",
            "PRIORITY:1",
            "CATEGORIES:home,money",
            "CLASS:PRIVATE",
            "URL:https://example.com/x",
        )
    )

    assert extras == {
        "parent_uid": "shop",
        "start": "2026-07-06",
        "percent_complete": 40,
        "location": "Desk",
        "rrule": "FREQ=WEEKLY",
        "priority": 1,
        "categories": ["home", "money"],
        "classification": "PRIVATE",
        "url": "https://example.com/x",
    }


def test_a_plain_todo_has_no_extras() -> None:
    assert read_todo_extras(_read("STATUS:COMPLETED")) == {}


def test_everything_below_an_item_comes_with_each_parent_ahead_of_its_subtasks() -> (
    None
):
    tree = Tree({"a": None, "b": "a", "c": "b", "d": "a", "e": None})

    assert tree.descendants("a") == ["b", "d", "c"]
    assert tree.descendants("e") == []
    assert tree.ancestors("c") == ["b", "a"]


def test_a_stored_cycle_is_walked_once() -> None:
    tree = Tree({"a": "b", "b": "a"})

    assert tree.descendants("a") == ["b"]
    assert tree.ancestors("a") == ["b"]


def test_a_parent_that_is_not_on_the_list_is_no_ancestor() -> None:
    assert Tree({"a": "gone"}).ancestors("a") == []
