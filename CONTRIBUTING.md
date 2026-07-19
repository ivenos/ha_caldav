# Contributing

## Setup

Python 3.14:

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
```

## Tests

Unit tests, from the repo root (live tests are excluded by default):

```bash
pytest
```

Lint and format:

```bash
ruff check
ruff format --check
```

Live tests exercise a real CalDAV server in a throwaway Docker container and are
excluded unless you ask for them. Bring one up and run them with:

```bash
.github/scripts/live-test.sh nextcloud    # also: radicale, xandikos
```

The container is removed afterwards, on failure included. CI runs the same
script against Nextcloud (the current major and the one behind), Radicale and
Xandikos.

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

## Pull requests

- One concern per PR. Add tests for behaviour changes.
- Tests and `ruff` must pass.
- Fill in the PR template, including the test plan (which server, which
  operations) and the CLA checkbox.