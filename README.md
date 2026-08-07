# 📅 CalDAV Complete

Home Assistant already has a built-in [CalDAV integration](https://www.home-assistant.io/integrations/caldav/). It's fine for showing your calendars and adding the odd event, but that's about where the calendar half stops: no editing, no deleting, and forget about changing a recurring series. Put something on your Nextcloud calendar through it and you're stuck with it until you go fix it somewhere else.

This one closes that gap. Same servers, same underlying CalDAV library, with the write support the built-in version never got.

## Features

- ✏️ Edit and delete events, not only create them.
- 🔁 Recurring events: change or remove a single occurrence, that one and everything after it, or the whole series.
- ⏰ Reminders, attendees, meeting links, categories, priority, free/busy. Everything the iCalendar event carries, not a title and a time.
- ✅ Finish a repeating to-do and it rolls forward to the next due date instead of marking the whole series done. Completed items carry a proper timestamp, and you can drag them into the order you want.
- 🎨 Calendars come in the colour they have on the server and follow along when you change it there, unless you pick your own in Home Assistant. You can push a colour back the other way too.
- 🔎 Search, import and export, moving events between calendars, creating and deleting calendars: all of it available as actions in an automation.
- 🛡️ If someone changed an event on the server since your last sync, your edit or deletion gets stopped instead of quietly overwriting theirs. A calendar you only have read access to doesn't pretend otherwise.
- ⚡ Polling only does the expensive work when something actually changed, and events and to-dos of one calendar share a single check.

## Actions

Beyond the standard `calendar.*` actions, this integration adds:

| Action | What it does |
| --- | --- |
| `ha_caldav.create_event` / `update_event` | RFC 5545 event properties: alarms, attendees, organizer, URL, categories, status, transparency, classification, priority |
| `ha_caldav.search_events` | Server-side text search over summary, description, location, category, status or uid |
| `ha_caldav.get_free_busy` | The busy periods the server reports for a window |
| `ha_caldav.move_event` | Move or copy an event to another calendar |
| `ha_caldav.import_ics` / `export_ics` | Read and write whole iCalendar documents |
| `ha_caldav.set_calendar_color` | Push a colour to the server |
| `ha_caldav.respond_to_invitation` | Accept, decline or tentatively accept, on servers that do CalDAV scheduling |
| `ha_caldav.create_calendar` / `delete_calendar` | Manage the calendars on the account. Administrators only, and deleting one takes everything on it with it |

The three that only read something back (`search_events`, `get_free_busy`, `export_ics`) need a `response_variable`, and hand back `events`, `periods` and `ics`. Everything except `create_calendar` and `delete_calendar` targets a calendar entity, so a list that only holds tasks has nothing to call them on.

The upcoming event also exposes what it carries as attributes (`url`, `status`, `transparency`, `classification`, `priority`, `categories`, `attendees`, `organizer`, `alarms`), so a meeting link is `{{ state_attr('calendar.iven_personal', 'url') }}`. They're kept out of the recorder, so attendee addresses don't pile up in your history.

## Requirements

Home Assistant 2026.3 or newer, and a CalDAV account.

## Installation

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ivenos&repository=ha_caldav&category=integration)

To add it by hand, drop `https://github.com/ivenos/ha_caldav` into HACS as a custom repository (category **Integration**) and restart Home Assistant.

Once it's installed, start the setup:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ha_caldav)

...or open **Settings → Devices & services → Add integration** and search for **CalDAV Complete**. Enter your server URL, which for Nextcloud usually looks like `https://cloud.example.com/remote.php/dav`.

Only the bare host name works too: the server URL is resolved through `/.well-known/caldav` if it has to be. If your server wants a client certificate or a private CA, there are optional fields for that.

Each account turns into a device with a `calendar.<account>_<calendar>` entity and a `todo.<account>_<calendar>` list per calendar, depending on what the server says each calendar holds: an event-only calendar gets no to-do list, and a task list gets no calendar. From the options you can pick which calendars to load and how often to poll (every 15 minutes by default). How far ahead to look for the upcoming event (7 days), whether all-day events count, and whether to stay read-only can also be set per calendar. Change your password and it'll ask you to sign in again; move the server to a new address, or switch its TLS settings, and you can reconfigure the entry in place. Pointing an entry at a different account is refused; that's a new account, not a reconfiguration.

Already running the built-in `caldav` integration for the same account? Drop it, or you'll see everything twice.

Not supported: email alarms, attachments, journal entries, switching a recurring event between all-day and timed, and splitting a series at a date added to it by hand.

## Compatibility

Pull requests and changes to `main` run the unit tests, and, together with a weekly build, the live tests against the current and previous Nextcloud majors, Radicale, Xandikos, Baikal and SOGo. Other RFC 4791 servers should work too, they just aren't tested.

Something broken? [Open an issue](https://github.com/ivenos/ha_caldav/issues) and attach the diagnostics download from the integration's menu; it says what each calendar reports about itself, with credentials redacted.

## License

[GPL-3.0](LICENSE)
