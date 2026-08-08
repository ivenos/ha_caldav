"""Tests that services.yaml, the schemas and the strings stay in step.

The selector option lists in services.yaml duplicate constants from const.py as
plain YAML, and nothing at runtime would notice the two drifting apart.
"""

import json
from pathlib import Path
from unittest.mock import Mock

from homeassistant.helpers import config_validation as cv
import pytest
import voluptuous as vol
import yaml

from custom_components.ha_caldav import const, services as service_module

COMPONENT = Path(const.__file__).parent
DESCRIPTIONS = yaml.safe_load((COMPONENT / "services.yaml").read_text())
STRINGS = json.loads((COMPONENT / "strings.json").read_text())


def _registered() -> dict[str, dict]:
    """Return service name -> schema, from the registration calls themselves.

    A hand-written table would keep passing for a service nobody described.
    """
    platform = Mock()
    service_module.async_register_entity_services(platform)
    schemas = {
        call.args[0]: call.args[1]
        for call in platform.async_register_entity_service.call_args_list
    }
    hass = Mock()
    with pytest.MonkeyPatch.context() as patch:
        admin: list[tuple] = []
        patch.setattr(
            service_module,
            "async_register_admin_service",
            lambda _hass, _domain, name, _func, schema: admin.append((name, schema)),
        )
        service_module.async_register_services(hass)
    return {name: _markers(schema) for name, schema in (schemas | dict(admin)).items()}


def _markers(schema: object) -> dict:
    """Return the field markers of a schema, however it arrives.

    A service whose fields constrain each other is registered as a vol.All
    around an entity-service schema, and that adds the target keys the
    description puts under `target` rather than under `fields`.
    """
    if isinstance(schema, dict):
        fields = schema
    elif isinstance(schema, vol.Schema):
        fields = _markers(schema.schema)
    elif isinstance(schema, vol.All):
        found = [_markers(part) for part in schema.validators]
        fields = next((part for part in found if part), {})
    else:
        fields = {}
    # What the entity-service base adds is a target or a stripped key, never a
    # field of this service: the targets by name, `metadata` by being a Remove.
    targets = {str(marker) for marker in cv.ENTITY_SERVICE_FIELDS}
    return {
        marker: validator
        for marker, validator in fields.items()
        if isinstance(marker, vol.Required | vol.Optional)
        and str(marker) not in targets
    }


SCHEMAS = _registered()


def _fields(service: str) -> dict:
    """Return the described fields, lifted out of any collapsible section."""
    described = DESCRIPTIONS[service].get("fields") or {}
    flat = {}
    for name, spec in described.items():
        if spec and "fields" in spec:
            flat.update(spec["fields"])
        else:
            flat[name] = spec
    return flat


def _sections(service: str) -> set[str]:
    return {
        name
        for name, spec in (DESCRIPTIONS[service].get("fields") or {}).items()
        if spec and "fields" in spec
    }


def test_every_service_is_described() -> None:
    declared = {
        value
        for name, value in vars(const).items()
        if name.startswith("SERVICE_") and isinstance(value, str)
    }

    assert set(SCHEMAS) == declared
    assert set(DESCRIPTIONS) == declared
    assert set(STRINGS["services"]) == declared


@pytest.mark.parametrize("service", sorted(SCHEMAS))
def test_the_described_fields_are_the_validated_fields(service: str) -> None:
    described = set(_fields(service))
    validated = {str(marker) for marker in SCHEMAS[service]}

    assert described == validated
    assert set(STRINGS["services"][service].get("fields") or {}) == validated


@pytest.mark.parametrize("service", sorted(SCHEMAS))
def test_every_section_is_named_in_the_strings(service: str) -> None:
    assert set(STRINGS["services"][service].get("sections") or {}) == _sections(service)


@pytest.mark.parametrize("service", sorted(SCHEMAS))
def test_required_is_declared_wherever_it_is_enforced(service: str) -> None:
    required = {
        str(marker) for marker in SCHEMAS[service] if isinstance(marker, vol.Required)
    }
    declared = {
        name for name, spec in _fields(service).items() if (spec or {}).get("required")
    }

    assert declared == required


@pytest.mark.parametrize("service", sorted(SCHEMAS))
def test_declared_defaults_match_the_schema(service: str) -> None:
    enforced = {
        str(marker): marker.default()
        for marker in SCHEMAS[service]
        if isinstance(marker, vol.Marker) and marker.default is not vol.UNDEFINED
    }
    declared = {
        name: spec["default"]
        for name, spec in _fields(service).items()
        if spec and "default" in spec
    }

    assert declared == enforced


@pytest.mark.parametrize(
    ("service", "field", "options"),
    [
        (const.SERVICE_SEARCH_EVENTS, "field", const.SEARCH_FIELDS),
        (const.SERVICE_CREATE_EVENT, "status", const.EVENT_STATUSES),
        (const.SERVICE_CREATE_EVENT, "transparency", const.EVENT_TRANSPARENCIES),
        (const.SERVICE_CREATE_EVENT, "classification", const.EVENT_CLASSIFICATIONS),
        (const.SERVICE_UPDATE_EVENT, "status", const.EVENT_STATUSES),
        (const.SERVICE_UPDATE_EVENT, "transparency", const.EVENT_TRANSPARENCIES),
        (const.SERVICE_UPDATE_EVENT, "classification", const.EVENT_CLASSIFICATIONS),
        (
            const.SERVICE_UPDATE_EVENT,
            "recurrence_range",
            (const.RANGE_THIS_AND_FUTURE,),
        ),
        (
            const.SERVICE_RESPOND_TO_INVITATION,
            "response",
            tuple(const.PARTSTAT_BY_RESPONSE),
        ),
        (
            const.SERVICE_CREATE_CALENDAR,
            "components",
            (const.COMPONENT_EVENT, const.COMPONENT_TODO),
        ),
    ],
)
def test_selector_options_match_the_constants(service, field, options) -> None:
    """The picker offers the RFC names lowercased: hassfest holds a selector's
    option keys to [a-z0-9-_]+, so the wire form cannot be the RFC value
    itself. The schema takes either spelling and hands the write path the RFC
    one; what must not drift is which values exist."""
    selector = _fields(service)[field]["selector"]["select"]

    assert selector["options"] == [value.lower() for value in options]


@pytest.mark.parametrize("service", sorted(SCHEMAS))
def test_every_translated_selector_has_its_options_named(service: str) -> None:
    for name, spec in _fields(service).items():
        select = ((spec or {}).get("selector") or {}).get("select") or {}
        if (key := select.get("translation_key")) is None:
            continue
        named = STRINGS["selector"][key]["options"]

        assert set(named) == set(select["options"]), f"{service}.{name}"


@pytest.mark.parametrize("service", sorted(SCHEMAS))
def test_every_example_validates_against_the_schema(service: str) -> None:
    validators = {
        str(marker): validator for marker, validator in SCHEMAS[service].items()
    }
    for name, spec in _fields(service).items():
        if (example := (spec or {}).get("example")) is None:
            continue
        text = str(example)
        value = yaml.safe_load(text) if text.startswith(("[", "{")) else example
        # A wrong example is a copy-and-paste trap in the UI.
        vol.Schema(validators[name])(value)


LANGUAGES = ("en", "de", "es", "fr", "it")


def _flatten(value: dict, prefix: str = "") -> dict[str, str]:
    """Return every leaf of a nested strings file, keyed by its dotted path."""
    flat: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(item, dict):
            flat.update(_flatten(item, f"{prefix}.{key}"))
        else:
            flat[f"{prefix}.{key}"] = item
    return flat


def _translation(language: str) -> dict[str, str]:
    path = COMPONENT / "translations" / f"{language}.json"
    return _flatten(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_translation_carries_exactly_the_declared_keys(language: str) -> None:
    """CONTRIBUTING asks for this and nothing else checked it.

    A key missing from one language shows the user a raw translation key, and
    one left behind after a rename is dead weight nobody notices.
    """
    reference = _flatten(STRINGS)
    translated = _translation(language)

    assert set(translated) - set(reference) == set()
    assert set(reference) - set(translated) == set()


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_translation_keeps_the_same_placeholders(language: str) -> None:
    # A placeholder dropped in translation renders the message without the
    # value it exists to name; an invented one raises at format time.
    import re

    reference = _flatten(STRINGS)
    translated = _translation(language)

    for key, text in reference.items():
        assert sorted(re.findall(r"\{(\w+)\}", text)) == sorted(
            re.findall(r"\{(\w+)\}", translated[key])
        ), key


def test_the_english_translation_matches_the_source_strings() -> None:
    # en.json is a copy of strings.json; a fix made in one has to reach both.
    assert _translation("en") == _flatten(STRINGS)


def test_every_service_has_an_icon() -> None:
    # A service with no icon renders as a blank tile in the action picker.
    icons = json.loads((COMPONENT / "icons.json").read_text())["services"]

    assert set(icons) == set(DESCRIPTIONS)
