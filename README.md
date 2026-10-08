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
- ⏰ Reminders, attendees, attachments, meeting links, categories, priority, time zones and free/busy
- 🔔 A reminder that comes due fires an event in Home Assistant
- 📝 Subtasks, start dates, priority, progress and reminders on to-dos
- ✅ Recurring to-dos move to their next due date when completed, and to-dos can be reordered
- 📨 Invitations can be listed and answered, for a whole series or one occurrence
- 📓 Journal entries, and calendars shared with or delegated by other accounts
- 🎨 Calendar colors from the server, unless set in Home Assistant
- 🔎 Actions to search, import, export and move events and to-dos and to manage and share calendars
- 🛡️ Writes check for changes made on the server in the meantime, and read-only calendars stay read-only
- ⚡ On servers that report changes, a calendar is fetched in full when something changed and otherwise once an hour

## Installation

Requires Home Assistant 2026.3 or newer, HACS and an account on a CalDAV server.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ivenos&repository=ha_caldav&category=integration)

Or add `https://github.com/ivenos/ha_caldav` to HACS as a custom repository of type **Integration**. Download it, restart Home Assistant and add the integration:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ha_caldav)

Or open **Settings → Devices & services → Add integration** and search for **CalDAV Complete**.

## Configuration

Setup asks for the server URL, username and password. The host name is usually enough, the CalDAV address is found through `/.well-known/caldav`. Certificate verification can be turned off, and under **Certificates** a CA bundle and a client certificate and key can be set.

Each calendar gets a `calendar.<calendar>` entity for its events and a `todo.<calendar>` entity for its to-dos, depending on what it holds. A calendar that holds nothing but journal entries gets a calendar entity that shows the dated ones. Both are named after the calendar on the server, which is also the name Assist uses for them. The calendars of an account that made this one its delegate are listed too, with the name of their owner. A calendar renamed on the server is renamed in Home Assistant at the next poll, one deleted there loses its entities after two more polls, and renaming one of its entities in Home Assistant renames the calendar on the server, unless it is read-only. The last three options can also be set per calendar.

| Option | Default | Description |
|---|---|---|
| Calendars to include | `all` | The calendars that get entities. With every calendar ticked, one added on the server gets them at the next poll too |
| Minutes between polls | `15` | How often to look for changes made outside Home Assistant, 1-1440 |
| Seconds a request may take | `30` | How long to wait for the server to answer, 5-120 |
| Days ahead | `7` | How far ahead to look for the upcoming event, 1-365 |
| Include all-day events | `on` | Whether an all-day event can be the upcoming event |
| Read-only | `off` | Hides the create, edit and delete controls, and the actions refuse to write |

## Actions

The event actions target a calendar entity and the to-do actions a to-do list entity. `import_ics`, `export_ics`, `set_calendar_color` and the journal and sharing actions target the calendar entity, or the to-do list entity of a calendar that has none. `create_calendar` and `delete_calendar` take the account.

| Action | What it does |
|---|---|
| `ha_caldav.create_event` / `update_event` | Create or change an event with a time zone, alarms, attendees, organizer, attachments, URL, categories, status, transparency, classification and priority |
| `ha_caldav.delete_event` | Delete an event, a single occurrence of a series or an occurrence and everything after it |
| `ha_caldav.search_events` | Search by summary, description, location, category, status or uid, or list everything without a text, returns `events`, one per occurrence within a start and end |
| `ha_caldav.get_free_busy` | Busy periods in a time window, returns `periods`, and those of other people under `attendees` |
| `ha_caldav.move_event` | Move or copy an event to another calendar |
| `ha_caldav.create_todo` / `update_todo` | Create or change a to-do with a parent, start, due date, recurrence, status, percent complete, priority, categories, location, URL, classification, attendees and alarms. Both take the fields of `todo.add_item` and `todo.update_item` and find an item by its name or UID |
| `ha_caldav.search_todos` | The to-dos of a list with all of these properties and `parent_uid`, the UID of the to-do each is a subtask of, optionally filtered by text or status, returns `todos` |
| `ha_caldav.move_todo` | Move or copy a to-do and its subtasks to another to-do list |
| `ha_caldav.import_ics` / `export_ics` | Import or export iCalendar data, the export returns `ics` |
| `ha_caldav.set_calendar_color` | Set the calendar color on the server |
| `ha_caldav.get_invitations` | The events that still wait for an answer, returns `invitations` |
| `ha_caldav.respond_to_invitation` | Accept, decline or tentatively accept an invitation, or one occurrence of it |
| `ha_caldav.create_journal` / `update_journal` / `delete_journal` | Create, change or delete a journal entry with a date, status, classification, categories, URL and attachments |
| `ha_caldav.search_journals` | The journal entries of a calendar, optionally filtered by text or a start and end, returns `journals` |
| `ha_caldav.share_calendar` / `unshare_calendar` | Share a calendar with another account on the server for reading or writing, or stop sharing it, administrators only |
| `ha_caldav.get_calendar_shares` | The accounts a calendar is shared with, returns `shares` |
| `ha_caldav.create_calendar` / `delete_calendar` | Create a calendar for events, to-dos or journal entries, or delete one, administrators only |

The upcoming event has the attributes `url`, `status`, `transparency`, `classification`, `priority`, `categories`, `attendees`, `organizer`, `alarms` and `attachments`, which are not recorded in the history.

An alarm is a number of minutes before the start, or an object with `minutes_before` or a fixed time `at`, an `action` of `display`, `audio` or `email`, and `related: end` to count from the end. An attachment is a link, or an object with `url`, `name` and `media_type`, or with the `path` of a file to embed from a folder Home Assistant may read.

When an alarm of an event within the days ahead or of an open to-do comes due, the event `ha_caldav_reminder` fires with `entity_id`, `uid`, `recurrence_id`, `summary`, `description`, `location`, `start`, `end` or `due`, and the `alarm`.

A to-do list shows subtasks as items of their own. Completing or canceling a to-do does the same to its open subtasks, reopening one reopens the completed or canceled to-dos it is a subtask of, and deleting one deletes its subtasks. A to-do that is in progress shows as open and a canceled one as completed.

## Compatibility

Tested with Nextcloud, Radicale, Xandikos, Baikal and SOGo. Sharing a calendar works with Nextcloud and with servers built on sabre/dav such as Baikal, delegated calendars with those and SOGo, and journal entries with every one of them but SOGo. Not supported: signing in with OAuth, and publishing a calendar under a public link.

## License

Copyright © Iven Schlösser. CalDAV Complete is free software, licensed under the [GNU General Public License v3.0 only](LICENSE). You may use, modify and redistribute it. Anyone distributing a modified version must release it under the same license and make its source code available.

CalDAV Complete builds on caldav, licensed under the GNU General Public License v3.0 or later or the Apache License 2.0, icalendar, licensed under the BSD 2-Clause License, vobject, licensed under the Apache License 2.0, and python-dateutil, licensed under the Apache License 2.0 and the BSD 3-Clause License.

CalDAV Complete is not affiliated with or endorsed by the Open Home Foundation. "Home Assistant" and the "Home Assistant" logo are trademarks of the Open Home Foundation.
