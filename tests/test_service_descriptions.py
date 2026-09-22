"""The selector options in services.yaml duplicate constants from const.py."""

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
    registered: list[tuple] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            service_module,
            "async_register_platform_entity_service",
            lambda _hass, _domain, name, schema, **_: registered.append((name, schema)),
        )
        patch.setattr(
            service_module,
            "async_register_admin_service",
            lambda _hass, _domain, name, _func, schema: registered.append(
                (name, schema)
            ),
        )
        service_module.async_register_services(Mock())
    return {name: _markers(schema) for name, schema in registered}


def _markers(schema: object) -> dict:
    """A vol.All around an entity-service schema adds the target keys, which the
    description puts under `target` rather than under `fields`."""
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
            const.SERVICE_DELETE_EVENT,
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
    """hassfest holds a selector's option keys to [a-z0-9-_]+, so the picker offers
    the RFC names lowercased."""
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
        vol.Schema(validators[name])(value)


LANGUAGES = ("en", "de", "es", "fr", "it")


def _flatten(value: dict, prefix: str = "") -> dict[str, str]:
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
    reference = _flatten(STRINGS)
    translated = _translation(language)

    assert set(translated) - set(reference) == set()
    assert set(reference) - set(translated) == set()


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_translation_keeps_the_same_placeholders(language: str) -> None:
    # An invented placeholder raises at format time.
    import re

    reference = _flatten(STRINGS)
    translated = _translation(language)

    for key, text in reference.items():
        assert sorted(re.findall(r"\{(\w+)\}", text)) == sorted(
            re.findall(r"\{(\w+)\}", translated[key])
        ), key


def test_the_english_translation_matches_the_source_strings() -> None:
    assert _translation("en") == _flatten(STRINGS)


def test_every_service_has_an_icon() -> None:
    # A service with no icon renders as a blank tile in the action picker.
    icons = json.loads((COMPONENT / "icons.json").read_text())["services"]

    assert set(icons) == set(DESCRIPTIONS)
