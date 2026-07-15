# CalDAV Complete for Home Assistant

The built-in `caldav` integration reads calendars and creates events, but it cannot edit or
delete them, so a Nextcloud calendar added through it can only be filled, never corrected.
This integration does the full set: create, edit and delete, for single events and repeating
series alike, plus to-do lists for Nextcloud Tasks. The edit and delete controls then appear
in the normal Home Assistant calendar view.

## Features

- Everything the built-in integration does, plus editing and deleting events.
- Repeating events: change or remove a single occurrence, an occurrence and everything after
  it, or the whole series.
- To-do lists for VTODO items (Nextcloud Tasks), with due dates and descriptions.
- Pick which calendars to include, how often to poll, and whether they are read-only.
- Re-authentication when the password changes.

## Requirements

Home Assistant 2026.3 or newer, and a CalDAV account with write access.

## Installation

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ivenos&repository=ha_caldav&category=integration)

Or add `https://github.com/ivenos/ha_caldav` in HACS as a custom repository with category
**Integration**. Restart Home Assistant afterwards, then:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ha_caldav)

Or go to **Settings > Devices & services > Add integration** and search for **CalDAV
Complete**. For Nextcloud the URL usually looks like
`https://cloud.example.com/remote.php/dav`.

Each account becomes a device holding a `calendar.<account>_<calendar>` entity and a
`todo.<account>_<calendar>` list per calendar. The options dialog covers calendar selection,
the poll interval (15 minutes by default), and read-only mode.

If the built-in `caldav` integration is set up for the same account, remove it to avoid
duplicate entities. This integration covers everything it does.

## Usage

Open the calendar panel or a calendar card, select an event and use edit or delete. For a
repeating event, Home Assistant asks whether the change applies to that occurrence, that one
and all following, or the whole series. Changes are written to the server immediately;
changes made elsewhere appear on the next poll.

## Compatibility

Tested against Nextcloud 32, 33 and 34. Other CalDAV servers such as Radicale and Baikal
speak the same protocol and are expected to work, but are not tested.

## License

[GPL-3.0](LICENSE)
