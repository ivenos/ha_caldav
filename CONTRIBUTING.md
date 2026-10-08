# Contributing

## Build

Python 3.14 in a virtual environment:

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
```

- There is nothing to compile. Home Assistant loads `custom_components/ha_caldav/` from its own `custom_components` folder.

## Tests

```bash
.venv/bin/pytest
.venv/bin/ruff check
.venv/bin/ruff format --check
```

- CI runs the unit tests against the Home Assistant pinned in `requirements_test.txt` and against the oldest one `hacs.json` allows with the minimum versions from `manifest.json`, on changes and daily.
- The live tests run against a real CalDAV server in throwaway Docker containers, which are removed afterwards:

```bash
tests/live-test.sh nextcloud             # also baikal, radicale, sogo, xandikos
tests/live-test.sh nextcloud 31 -- -v    # an image tag, then pytest arguments
```

- CI runs the live tests against the current and previous Nextcloud major, Radicale, Xandikos, Baikal and SOGo, on changes and weekly.
- `.github/scripts/screenshots.sh` rebuilds the README screenshots from a throwaway Home Assistant and Radicale with sample calendars. Run it when a change alters what the calendar or the to-do list shows.

## Code style

- Python 3.14, formatted with ruff, line length 88.
- Docstrings on public functions. Private helpers only get one when the behavior is not obvious.
- A comment only earns its place when it records something the code cannot, such as an RFC 5545 rule or a library pitfall. One or two lines.

## Adding an option

- The default in `const.py`, the field in the options flow in `config_flow.py`, and where it is read, usually `options.py`.
- The label and description in `strings.json` and in every file under `translations/`.
- A row in the README table.
- An option that is not in the README does not exist.

## Config entries

- A change to the shape of the account key or an entity `unique_id` raises `MINOR_VERSION` in `config_flow.py` and gets a migration in `async_migrate_entry`.

## Translations

- `strings.json` is the source. Every key in it exists in all five files under `translations/`, `en.json` included, with the same `{placeholders}`.

## Commits

- Conventional Commits (https://www.conventionalcommits.org/en/v1.0.0/) with a short imperative subject.
- A commit that does more than one thing lists its changes in the body, under the changelog section names as plain words. Changes to CI and tests are listed there too:

```
Added
- Journal entries can be created and searched

Changed
- CI tests sharing with a second account

Fixed
- A time with a UTC offset was stored with a broken time zone
```

## Changelog

- `CHANGELOG.md` follows Keep a Changelog (https://keepachangelog.com/en/1.1.0/), with `Breaking changes` as the first section and `Dependencies` as the last.
- Every commit that changes something for users adds its entry under `Unreleased`. CI, tests and refactoring stay out.
- An entry is one short sentence in plain words that anyone understands. Details belong in the README.
- What users have to act on after an update goes under `Breaking changes`.
- An entry that goes back to an issue or a pull request ends with its number: `(#12)`.

## Releases

- Run the Release workflow with the new version, such as `1.5.0`. It turns `Unreleased` into that version, tags the commit and builds a draft release with `ha_caldav.zip` and the notes from `CHANGELOG.md`.
- A tag pushed by hand builds the draft too, from the section of its version.
- The workflow sets the version in `manifest.json`. The one in the repository is a placeholder.
- HACS shows the README of the latest release.
- Rebuild the README screenshots before every release.

## Dependencies

- GitHub Actions stay on version tags, never commit SHAs. hassfest has no tagged release and runs from `master`.
- Renovate opens the bumps and adds each one to the changelog. The release adds the numbers of their pull requests. Other PRs leave dependencies alone.
- `caldav`, `icalendar` and `vobject` follow Home Assistant core. `manifest.json` lists them as minimum versions, the ones the oldest supported Home Assistant pins. `requirements_test.txt` pins the versions the newest Home Assistant pins, its beta included. Both move by hand, with `.github/scripts/changelog.sh dependency NAME FROM TO` for the changelog.
- `python-dateutil` is not listed in `manifest.json` or `requirements_test.txt`.

## Pull requests

- One concern per PR, with tests and a changelog entry for behavior changes.
- Only the PR author and the maintainer commit to it.
- `pytest`, `ruff check` and `ruff format --check` must pass.
- The test plan names the CalDAV server and what you exercised: the calendar, the to-do list or which actions.
- Fill in the PR template, including the CLA checkbox.
