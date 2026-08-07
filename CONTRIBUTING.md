# Contributing

## Setup

Python 3.14:

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
```

## Tests

Unit tests, from the repo root:

```bash
.venv/bin/pytest
```

Lint and format:

```bash
.venv/bin/ruff check
.venv/bin/ruff format --check
```

Live tests exercise a real CalDAV server in a throwaway Docker container and are
excluded unless you ask for them. Bring one up and run them with:

```bash
.github/scripts/live-test.sh nextcloud    # also: baikal, radicale, sogo, xandikos
.github/scripts/live-test.sh nextcloud 31 -- -v    # a given tag, then pytest args
```

The containers are removed afterwards, on failure included. Baikal and SOGo
need more than a start: the script seeds Baikal's config file and database in
place of its install wizard, and gives SOGo a MariaDB container plus the SQL
view it authenticates against. CI runs the same script against Nextcloud (the
current major and the one behind), Radicale, Xandikos, Baikal and SOGo.

Test doubles stand in for `caldav` objects, and a forgiving one hides real
defects: a `save()` that records nothing hid a double SEQUENCE bump, and a
resource exposing only `icalendar_component` hid an edit landing on the wrong
subcomponent. Model what the library actually does, or let caldav's own code
run against `RecordingClient` from `tests/conftest.py`, which captures the PUT
instead of sending it.

## Config entry versions

Changing the shape of the account key or of an entity `unique_id` needs
`MINOR_VERSION` on the config flow moved as well. Home Assistant compares the
stored version against the handler's and returns before loading the component
when they agree, so a migration written for entries that already carry the
current number never runs at all.

## Code style

- A comment only earns its place when it records something the code cannot: an
  RFC 5545 rule, a library pitfall, the reason for a non-obvious flag. One or
  two lines. Never restate what the code already says.
- Docstrings on public functions. Private helpers stay bare unless the behaviour
  is non-obvious.
- Python 3.14, ruff-formatted, line length 88.

## Commits

Conventional Commits (https://www.conventionalcommits.org/en/v1.0.0/), with a
short imperative subject.

## Releases

The changelog lives in the GitHub release notes, in Keep a Changelog style
(https://keepachangelog.com/en/1.1.0/). There is no CHANGELOG.md file.

## Dependencies

`caldav`, `icalendar` and `vobject` are pinned to the versions Home Assistant
core ships, not the latest on PyPI, so the integration runs in the same
environment as the built-in `caldav` integration. Do not bump them on their own.

`dateutil` is imported directly but not listed: `icalendar` and `vobject` both
require it, and since those two are pinned to an exact version, so is what they
pull in. Declaring it here could only conflict with them.

## Translations

`strings.json` is the source. Every key in it has to exist in all five files
under `translations/`, `en.json` included, with the same `{placeholders}`.
Adding a user-facing string therefore means touching six files.

## Pull requests

- One concern per PR. Add tests for behaviour changes.
- Tests and `ruff` must pass.
- Fill in the PR template, including the test plan (which server, which
  operations) and the CLA checkbox.