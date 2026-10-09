# Changelog

All notable changes to this project are documented in this file, in the format of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- Listing invitations on Radicale gave nothing instead of a clear message.

### Dependencies

- hypothesis 6.168.4 -> 6.168.5
- pytest-homeassistant-custom-component 0.13.368 -> 0.13.369

## [1.5.0] - 2026-10-08

### Added

- Subtasks for to-dos, and more fields such as start, priority, progress, categories and reminders (#24).
- Search the to-dos of a list with all their details (#24).
- Move or copy a to-do with its subtasks to another list.
- Import, export and the calendar color also work on a list without events.
- A reminder that comes due fires the event `ha_caldav_reminder`.
- Events can be created in another time zone, or moved to one.
- Attachments on events, to-dos and journal entries, as links or files.
- Reminders at a fixed time and by email.
- List the invitations you have not answered, and answer a single occurrence.
- Ask the server when other people are busy.
- Calendars that another account delegated to you show up too.
- Journal entries can be created, changed, deleted and searched.
- Share a calendar with another account, on Nextcloud and Baikal.
- List all events of a time span without a search text.
- Events show as confirmed or tentative on Home Assistant 2026.10 and newer.

### Changed

- Completing, reopening or deleting a to-do does the same to its subtasks, as in Nextcloud Tasks (#24).
- A recurring event can be switched between all-day and timed.
- A series can be split at a date that was added to it by hand.
- Series with "this and all later" exceptions from Apple Calendar or Outlook can be edited.
- Setting the alarms of an event replaces its email and fixed-time alarms too.
- A time with a UTC offset is stored in the time zone of Home Assistant.

### Fixed

- iCloud calendars with events were unavailable on Home Assistant 2026.10 (#27).
- A time with a UTC offset was stored with a time zone no other app understands.

### Dependencies

- ruff 0.16.9 -> 0.16.10 (#23)
- pytest-homeassistant-custom-component 0.13.367 -> 0.13.368 (#22)
- hypothesis 6.168.3 -> 6.168.4 (#25)
- mcr.microsoft.com/playwright v1.63.0-noble -> v1.64.0-noble (#26)

## [1.4.2] - 2026-10-02

### Fixed

- Editing and deleting failed with "This item changed on the server" behind a proxy that compresses responses, such as Caddy or Apache (#21).
- `update_event` and `delete_event` failed after another app had changed the event.

## [1.4.1] - 2026-10-01

### Changed

- The integration uses the caldav, icalendar and vobject versions that Home Assistant ships, instead of fixed ones.

### Fixed

- On Home Assistant 2026.10 the built-in CalDAV integration failed to load next to this one, until the restart after this update.

### Dependencies

- caldav 2.1.0 -> 3.3.0a1 (tests only)
- ruff 0.16.8 -> 0.16.9 (#17)
- pytest-homeassistant-custom-component 0.13.366 -> 0.13.367 (#19)
- hypothesis 6.168.1 -> 6.168.3 (#18, #20)

## [1.4.0] - 2026-09-26

### Breaking changes

- The account device is gone. Point automations, scripts and labels at the calendars and to-do lists instead.
- Renaming a calendar or to-do list in Home Assistant renames it on the server, for every other app too.

### Added

- `delete_event` deletes an event, one occurrence of a series, or an occurrence and all later ones.
- Calendar names stay in sync: a rename on the server shows in Home Assistant, and a rename in Home Assistant goes to the server.
- A calendar created on the server shows up by itself when all calendars are included.
- `search_events` returns each occurrence of a series when given a start and an end.
- Reconfigure also takes a new password.
- Setup names an invalid URL, a missing certificate file or a failed secure connection instead of an unexpected error.
- Calendar colors given as color names are recognized.

### Changed

- Calendars and to-do lists are named after the calendar alone, without the account in front, so Assist finds them by that name (#14).
- A write is refused when the item changed on the server in the meantime, and a new item never overwrites an existing one.
- Reading a calendar takes one request instead of two.
- A recurrence rule that repeats more often than hourly is refused.
- An import with unreadable or conflicting entries is refused as a whole instead of stored in part.
- Moving an event that has no ID is refused.
- An empty text in an action clears its field.
- An event can be copied out of a read-only calendar.
- A series split off from an occurrence keeps its links to other events.
- The actions exist right from startup, also while an account is still connecting.
- With every calendar ticked in the options, new calendars on the server are included again.
- Deleting a calendar also removes its own options.
- The options explain the calendar selection and what read-only covers.
- Setup messages and errors are translated in all five languages.

### Removed

- The account device. Its area carries over to the calendars and to-do lists.

### Fixed

- Moving a series or changing its rule dropped its exceptions and canceled occurrences.
- Moving a series to another weekday from one occurrence onwards was refused.
- "Does not repeat" did not end a series.
- A series with an end date could lose its last occurrence or end a day early.
- A series split in two kept the full number of repeats instead of the remaining ones.
- "This and following" on a moved occurrence shifted the series instead of applying the edit.
- Deleting or splitting from the first occurrence left an empty series behind.
- An occurrence of a very old series could not be edited.
- A new organizer was set on one occurrence only instead of the whole series.
- The revision number of an event changed when it should not, for example on events organized by someone else.
- One series with an unusual rule could take down the whole calendar or hang the setup.
- A broken time zone on one event shifted later events in the same zone.
- Setting alarms removed email alarms.
- The calendar state did not move on to the next event when one ended.
- A recurring to-do moved on when renamed, or landed on an excluded date when completed.
- Renaming a to-do could clear its progress.
- Two edits of one to-do in a row undid each other.
- A to-do completed in SOGo disappeared from the list.
- Text like "COMPLETED:3 of 5" in a description was changed.
- A second edit of the same item right after the first was refused.
- Writes and deletes behind a redirect were lost or wrongly reported as done.
- A failed move left a copy behind.
- A lookup that timed out read the whole calendar instead.
- Deleting a calendar could remove the to-do list of another calendar.
- `delete_calendar` on the only included calendar brought back all others, and both calendar actions ignored read-only.
- An empty `recurrence_id` from a template was refused.
- A calendar deleted on the server stayed unavailable forever instead of being removed.
- Renaming a selected calendar removed its entities.
- The options form could be blocked by an old empty selection, and calendars of the same name looked alike in it.
- One calendar without a readable color removed the colors of the whole account.
- Setup found no calendars when the server URL redirected.
- Setup gave up on a refused login without trying the address the server points to.
- A rejected password was not always recognized, so no new one was asked for.
- The same server spelled differently counted as another server.
- An unreachable server made setup and the options dialog wait twice as long.
- Accounts with more than ten calendars opened new connections on every poll.
- After a downgrade, the stored settings were migrated the wrong way.
- Diagnostics left out calendars with similar paths.
- Wording in the French, Spanish and Italian translations.

### Security

- Login data is no longer sent on from https to plain http on another host.
- New items get random IDs instead of ones that contain the network address of the host.
- Session cookies are hidden in the debug log.

### Dependencies

- ruff 0.16.7 -> 0.16.8 (#12)
- pytest-homeassistant-custom-component 0.13.365 -> 0.13.366 (#15)
- hypothesis 6.168.0 -> 6.168.1 (#16)

## [1.3.1] - 2026-09-16

### Added

- The integration shows its own icon and logo in Home Assistant instead of a placeholder.

### Changed

- The English UI spells the event status "Canceled".

### Dependencies

- ruff 0.16.6 -> 0.16.7 (#10)
- pytest-homeassistant-custom-component 0.13.364 -> 0.13.365 (#11)
- hypothesis 6.167.1 -> 6.168.0 (#9)

## [1.3.0] - 2026-09-08

### Added

- A request timeout option, 30 seconds by default and up to 120 (#2).
- The certificate paths sit in a collapsed "Certificates" section during setup.
- A new account is named `username@host`, so two accounts with the same username can be told apart.
- `search_events` returns the extra event details such as attendees, alarms and categories.
- Diagnostics show how up to date each calendar and to-do list is.

### Changed

- A calendar or to-do list keeps showing its last result for up to three failed polls before it becomes unavailable.
- A new password is only asked for when nothing else explains the failed login.
- Servers that cannot report changes are no longer asked for them on every poll.

### Fixed

- A space after the server URL or the username broke the connection.

### Dependencies

- ruff 0.15.21 -> 0.16.6 (#6)
- pytest-homeassistant-custom-component 0.13.346 -> 0.13.364 (#7)
- hypothesis 6.165.2 -> 6.167.1 (#7)
- actions/setup-python v6 -> v7 (#8)

## [1.2.0] - 2026-08-08

### Added

- Edit and delete one occurrence of a series, an occurrence and all later ones, or the whole series.
- More event details as attributes: alarms, attendees, organizer, link, categories, status and more.
- Actions `create_event` and `update_event`, which can set all of these details.
- Actions `search_events` and `get_free_busy`.
- Actions `move_event`, `import_ics` and `export_ics`.
- Actions `create_calendar`, `delete_calendar` and `set_calendar_color`.
- Action `respond_to_invitation` to accept or decline an invitation.
- To-dos can be reordered by dragging.
- Completing a recurring to-do moves it to its next date instead of closing the series.
- Options per calendar, next to the ones for the whole account.
- Reconfigure moves an account to another address without losing its entities.
- Client certificate, key and CA bundle for the connection.
- A bare host name is enough for setup, the CalDAV address is found by itself.
- Each calendar only gets the entities it can hold, and is read-only when the server says so.
- A repair notice when the server cannot report changes, and when the same account is also set up in the built-in CalDAV integration.

### Changed

- One account can no longer be set up twice under two spellings of its address.
- An edit is refused when the item changed on the server in the meantime.
- A calendar without events or without to-dos no longer gets an entity for them.
- Diagnostics show what each calendar can hold, its color and whether it is read.

### Fixed

- Ticking off a to-do could delete an event that shared its ID.
- Text containing `COMPLETED:` and digits was changed when saved.
- Every edit counted as two revisions.
- A to-do with a doubled recurrence rule could not be completed.
- Events could not be found on some servers, iCloud among them.
- An event with attendees but no organizer could not be deleted on servers such as Baikal.
- A series with an end date gained or lost its last occurrence, depending on the time zone.
- One broken item could make the whole calendar unavailable.
- A recurrence rule whose calculation never finishes was not refused.
- Events were saved without the definition of their time zone, which some servers reject.
- An export of a single item had line endings that some apps refuse.
- An attendee name with a quotation mark showed extra characters.

### Security

- Setup no longer follows a redirect away from https, and login data is not sent to another host.
- Diagnostics hide the password, the account address and the certificate paths.

## [1.1.0] - 2026-08-03

### Added

- Calendars take their color from the server.
- A color picked or cleared in Home Assistant overrides the server.
- Colors are understood in all common notations.
- Diagnostics list the color of each calendar.

## [1.0.0] - 2026-07-19

### Added

- Edit and delete calendar events, not just create them.
- Apply an edit or a deletion to one occurrence, to it and all later ones, or to the whole series.
- To-do lists with due dates and descriptions.
- Completing a recurring to-do moves it to its next due date.
- An edit made on the server in the meantime is noticed instead of overwritten.
- The calendar is only read again when something changed on the server.
- Diagnostics, with addresses and login data hidden.
- Options for calendar selection, poll interval, days ahead, all-day events and read-only.
- German, Spanish, French and Italian translations.

## [0.1.0] - 2026-07-15

### Added

- A calendar entity per CalDAV calendar, with create, edit and delete in the Home Assistant calendar view.
- Change or remove one occurrence of a series, an occurrence and all later ones, or the whole series.
- A to-do list entity per calendar, with due dates and descriptions.
- Setup with a connection check, and a new login when the password changes.
- Options for calendar selection, poll interval, days ahead, all-day events and read-only.
- English and German translations.

[Unreleased]: https://github.com/ivenos/ha_caldav/compare/v1.5.0...HEAD
[1.5.0]: https://github.com/ivenos/ha_caldav/compare/v1.4.2...v1.5.0
[1.4.2]: https://github.com/ivenos/ha_caldav/compare/v1.4.1...v1.4.2
[1.4.1]: https://github.com/ivenos/ha_caldav/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/ivenos/ha_caldav/compare/v1.3.1...v1.4.0
[1.3.1]: https://github.com/ivenos/ha_caldav/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/ivenos/ha_caldav/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/ivenos/ha_caldav/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/ivenos/ha_caldav/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/ivenos/ha_caldav/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/ivenos/ha_caldav/releases/tag/v0.1.0
