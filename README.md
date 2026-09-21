<div align="center">

<img alt="CalDAV Complete" src="https://raw.githubusercontent.com/ivenos/ha_caldav/main/.github/assets/ha_caldav-logo-auto.svg" width="50%">

<a href="#installation"><img alt="Home Assistant compatibility" src="https://shieldcn.dev/badge/Home_Assistant-2026.3%2B.svg?variant=secondary&amp;mode=dark&amp;logo=homeassistant"></a>
<a href="https://github.com/ivenos/ha_caldav/releases"><img alt="Downloads" src="https://shieldcn.dev/github/downloads/ivenos/ha_caldav.svg?variant=secondary&amp;mode=dark"></a>
<a href="https://github.com/ivenos/ha_caldav/blob/main/LICENSE"><img alt="License" src="https://shieldcn.dev/badge/license-GPL_3.0.svg?variant=secondary&amp;mode=dark"></a>

CalDAV Complete is a Home Assistant integration that connects calendars and to-do lists on CalDAV servers such as Nextcloud, including editing and deleting events and changing recurring series, which the built-in CalDAV integration cannot do.

<img alt="Updating one occurrence of a recurring event, over a month of calendars in their server colors" src="https://raw.githubusercontent.com/ivenos/ha_caldav/main/.github/assets/event-editor.png" width="48%">
<img alt="Two to-do lists with due dates and descriptions next to the events of the week" src="https://raw.githubusercontent.com/ivenos/ha_caldav/main/.github/assets/todo-list.png" width="48%">

</div>

---

## Features

- 🔁 Change a single occurrence, all following ones or the whole series
- ⏰ Reminders, attendees, meeting links, categories, priority and free/busy
- ✅ Recurring to-dos move to their next due date when completed, and to-dos can be reordered
- 🎨 Calendar colors from the server, unless set in Home Assistant
- 🔎 Actions to search, import, export and move events and to manage calendars
- 🛡️ Writes check for changes made on the server in the meantime, and read-only calendars stay read-only
- ⚡ On servers that report changes, calendars are only fetched in full when something changed

## Installation

Requires Home Assistant 2026.3 or newer, HACS and an account on a CalDAV server.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ivenos&repository=ha_caldav&category=integration)

Or add `https://github.com/ivenos/ha_caldav` to HACS as a custom repository of type **Integration**. Download it, restart Home Assistant and add the integration:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ha_caldav)

Or open **Settings → Devices & services → Add integration** and search for **CalDAV Complete**.

## Configuration

Setup asks for the server URL, username and password. The host name is usually enough, the CalDAV address is found through `/.well-known/caldav`. Certificate verification can be turned off, and under **Certificates** a CA bundle and a client certificate and key can be set.

Each calendar gets a `calendar.<calendar>` entity for its events and a `todo.<calendar>` entity for its to-dos, depending on what it holds. Both are named after the calendar on the server, which is also the name Assist uses for them. A calendar renamed on the server is renamed in Home Assistant at the next poll, and renaming one of its entities in Home Assistant renames the calendar on the server, unless it is read-only. The last three options can also be set per calendar.

| Option | Default | Description |
|---|---|---|
| Calendars to include | `all` | The calendars that get entities. With every calendar ticked, one added on the server gets them at the next poll too |
| Minutes between polls | `15` | How often to look for changes made outside Home Assistant, 1-1440 |
| Seconds a request may take | `30` | How long to wait for the server to answer, 5-120 |
| Days ahead | `7` | How far ahead to look for the upcoming event, 1-365 |
| Include all-day events | `on` | Whether an all-day event can be the upcoming event |
| Read-only | `off` | Hides the create, edit and delete controls |

## Actions

All actions except `create_calendar` and `delete_calendar` target a calendar entity.

| Action | What it does |
|---|---|
| `ha_caldav.create_event` / `update_event` | Create or change an event with alarms, attendees, organizer, URL, categories, status, transparency, classification and priority |
| `ha_caldav.delete_event` | Delete an event, a single occurrence of a series or an occurrence and everything after it |
| `ha_caldav.search_events` | Search by summary, description, location, category, status or uid, returns `events`, one per occurrence within a start and end |
| `ha_caldav.get_free_busy` | Busy periods in a time window, returns `periods` |
| `ha_caldav.move_event` | Move or copy an event to another calendar |
| `ha_caldav.import_ics` / `export_ics` | Import or export iCalendar data, the export returns `ics` |
| `ha_caldav.set_calendar_color` | Set the calendar color on the server |
| `ha_caldav.respond_to_invitation` | Accept, decline or tentatively accept an invitation |
| `ha_caldav.create_calendar` / `delete_calendar` | Create or delete a calendar, administrators only |

The upcoming event has the attributes `url`, `status`, `transparency`, `classification`, `priority`, `categories`, `attendees`, `organizer` and `alarms`, which are not recorded in the history.

## Compatibility

Tested with Nextcloud, Radicale, Xandikos, Baikal and SOGo. Not supported: email alarms, attachments, journal entries, switching a recurring event between all-day and timed, and splitting a series at a date added by hand.

## License

Copyright © Iven Schlösser. CalDAV Complete is free software, licensed under the [GNU General Public License v3.0 only](LICENSE). You may use, modify and redistribute it. Anyone distributing a modified version must release it under the same license and make its source code available.

CalDAV Complete builds on caldav, licensed under the GNU General Public License v3.0 or later or the Apache License 2.0, icalendar, licensed under the BSD 2-Clause License, vobject, licensed under the Apache License 2.0, and python-dateutil, licensed under the Apache License 2.0 and the BSD 3-Clause License.

CalDAV Complete is not affiliated with or endorsed by the Open Home Foundation. "Home Assistant" and the "Home Assistant" logo are trademarks of the Open Home Foundation.
