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

- 🔁 Change one occurrence of a series or all later ones
- ⏰ Reminders, attendees, attachments, meeting links, categories, time zones and free/busy
- 🔔 Reminders can trigger automations
- 📝 Subtasks, start dates, priority, progress and sorting for to-dos
- ✅ Recurring to-dos move to their next date when completed
- 📨 See and answer invitations
- 📓 Journal entries, shared and delegated calendars
- 🎨 Calendar colors and names in sync with the server
- 🔎 Actions to search, import, export, move and share
- 🛡️ Writes check for changes made elsewhere
- ⚡ Unchanged calendars are not reloaded on every poll

## Installation

Requires Home Assistant 2026.3 or newer, HACS and an account on a CalDAV server.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ivenos&repository=ha_caldav&category=integration)

Or add `https://github.com/ivenos/ha_caldav` to HACS as a custom repository of type **Integration**. Download it, restart Home Assistant and add the integration:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ha_caldav)

Or open **Settings → Devices & services → Add integration** and search for **CalDAV Complete**.

## Configuration

Setup asks for the server URL, username and password. A host name is usually enough, and under **Certificates** a CA bundle and a client certificate and key can be set.

| Option | Default | Description |
|---|---|---|
| Calendars to include | `all` | The calendars that get entities. With every calendar ticked, one added on the server gets them at the next poll too |
| Minutes between polls | `15` | How often to look for changes made outside Home Assistant, 1-1440 |
| Seconds a request may take | `30` | How long to wait for the server to answer, 5-120 |
| Days ahead | `7` | How far ahead to look for the upcoming event, 1-365 |
| Include all-day events | `on` | Whether an all-day event can be the upcoming event |
| Read-only | `off` | Hides the create, edit and delete controls, and the actions refuse to write |

The last three options can also be set per calendar.

## Actions

Called as `ha_caldav.<action>` on a calendar or to-do list, `create_calendar` and `delete_calendar` on the account.

| Action | What it does |
|---|---|
| `create_event`, `update_event`, `delete_event` | Create, change or delete an event, a single occurrence of a series or an occurrence and everything after it |
| `search_events` | Search by summary, description, location, category, status or UID, or list a time span. Returns `events`, one per occurrence within a start and end |
| `get_free_busy` | Returns the busy `periods` in a time window, and those of other people as `attendees` |
| `get_invitations`, `respond_to_invitation` | Returns the `invitations` that wait for an answer, and accepts, declines or tentatively accepts one or a single occurrence of it |
| `create_todo`, `update_todo` | Create or change a to-do or a subtask, found by its name or UID, with the fields of `todo.add_item` and `todo.update_item` |
| `search_todos` | Returns the `todos` of a list with all their properties and `parent_uid`, optionally filtered by text or status |
| `create_journal`, `update_journal`, `delete_journal` | Create, change or delete a journal entry |
| `search_journals` | Returns the `journals` of a calendar, optionally filtered by text or a start and end |
| `move_event`, `move_todo` | Move or copy an event to another calendar, or a to-do and its subtasks to another list |
| `import_ics`, `export_ics` | Import iCalendar data, or return it as `ics` |
| `set_calendar_color` | Set the calendar color on the server |
| `share_calendar`, `unshare_calendar`, `get_calendar_shares` | Share a calendar with another account on the server for reading or writing, or stop sharing it, administrators only. Returns its `shares` |
| `create_calendar`, `delete_calendar` | Create a calendar for events, to-dos or journal entries, or delete one, administrators only |

An alarm of an event within the days ahead or of an open to-do fires the event `ha_caldav_reminder` with `entity_id`, `uid`, `recurrence_id`, `summary`, `description`, `location`, `start`, `end` or `due`, and the `alarm`. Completing, canceling or deleting a to-do does the same to its subtasks, and reopening one reopens the to-dos above it.

## Compatibility

| | Nextcloud | Baikal | Radicale | Xandikos | SOGo |
|---|:-:|:-:|:-:|:-:|:-:|
| Events, to-dos, calendar names and colors | ✅ | ✅ | ✅ | ✅ | ✅ |
| Journal entries | ✅ | ✅ | ✅ | ✅ | ❌ |
| Invitations | ✅ | ✅ | ❌ | ❌ | ✅ |
| Free/busy of other people | ✅ | 🟡 | ❌ | ❌ | ✅ |
| Sharing a calendar | ✅ | ✅ | ❌ | ❌ | ❌ |
| Delegated calendars | ✅ | ✅ | ❌ | ❌ | ✅ |

✅ is tested, 🟡 is expected to work but not tested, ❌ is not available on the server. Other servers are not tested, iCloud is in use with the integration. Not supported: signing in with OAuth, and publishing a calendar under a public link.

## License

Copyright © Iven Schlösser. CalDAV Complete is free software, licensed under the [GNU General Public License v3.0 only](LICENSE). You may use, modify and redistribute it. Anyone distributing a modified version must release it under the same license and make its source code available.

CalDAV Complete builds on caldav, licensed under the GNU General Public License v3.0 or later or the Apache License 2.0, icalendar, licensed under the BSD 2-Clause License, vobject, licensed under the Apache License 2.0, and python-dateutil, licensed under the Apache License 2.0 and the BSD 3-Clause License.

CalDAV Complete is not affiliated with or endorsed by the Open Home Foundation. "Home Assistant" and the "Home Assistant" logo are trademarks of the Open Home Foundation.
