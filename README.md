# 📅 CalDAV for Home Assistant

Home Assistant already has a built-in [CalDAV integration](https://www.home-assistant.io/integrations/caldav/). It's fine for showing your calendars and adding the odd event, but that's about where it stops: no editing, no deleting, and forget about changing a recurring series. Put something on your Nextcloud calendar through it and you're stuck with it until you go fix it somewhere else.

This one closes that gap. Same servers, same underlying CalDAV library, just with the write support the built-in version never got. So instead of only reading a calendar in Home Assistant, you can actually run it from there, and that's where the extra bits come in:

## Features

- ✏️ Edit and delete events, not only create them.
- 🔁 Recurring events done right: change or remove a single occurrence, that one and everything after it, or the whole series.
- ✅ Finish a repeating to-do and it rolls forward to the next due date instead of marking the whole series done.
- 🎨 Calendars come in the colour they have on the server and follow along when you change it there, unless you pick your own in Home Assistant.
- 🛡️ If someone changed an event on the server since your last sync, your edit gets stopped instead of quietly overwriting theirs.
- ⚡ Polling only does the expensive work when something actually changed, so it stays light.

## Requirements

Home Assistant 2026.3 or newer, and a CalDAV account you can write to.

## Installation

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ivenos&repository=ha_caldav&category=integration)

Prefer to add it by hand? Drop `https://github.com/ivenos/ha_caldav` into HACS as a custom repository (category **Integration**) and restart Home Assistant.

Once it's installed, start the setup:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ha_caldav)

...or open **Settings → Devices & services → Add integration** and search for **CalDAV Complete**. Enter your server URL, which for Nextcloud usually looks like `https://cloud.example.com/remote.php/dav`.

Each account turns into a device with a `calendar.<account>_<calendar>` entity and a `todo.<account>_<calendar>` list per calendar. From the options you can pick which calendars to load, how often to poll (every 15 minutes by default), and whether to keep everything read-only. Change your password and it'll ask you to sign in again.

Already running the built-in `caldav` integration for the same account? Drop it, or you'll see everything twice.

## Compatibility

CI runs the full test suite on every push against the current and previous Nextcloud majors, plus Radicale and Xandikos. Other RFC 4791 servers like Baikal should work too, they just aren't tested.

## License

[GPL-3.0](LICENSE)
