# School rule evidence and defaults

Observed 2026-10-19 in the user's authenticated Safari at
`https://libseat.njfu.edu.cn/#/ic/userhelp`. The seventh-floor north reservation
page independently displayed the same weekly opening hours. No seat was
submitted, cancelled or changed.

| Regular rule | Built-in value |
| --- | --- |
| Next-day reservations open | 07:00 Beijing time on the previous day |
| Opening | 07:30 |
| Closing | 22:00; Friday 20:00 |
| Duration | 120–900 minutes |
| Sign-in | Within 30 minutes of the reserved start |

`local_app/policy.py` is the single source exposed by the read-only local
policy API. New plans use next day, 07:00 and `time_mode=full_day`.
The actual target weekday determines closing time when resolving a run,
including across month/year boundaries. Existing plans without `time_mode`
retain custom times; their values are not rewritten by loading the new UI.

New configuration accepts next-day scheduling. A browseable future date in
the official calendar is not evidence that a reservation is already open.
Custom intervals are validated against duration and regular opening hours;
execution checks the actual target date again before any school submission.
Special closures and future changes require current official notices.

Friday's 20:00 upper bound now applies to custom daily, weekly, and manual
same-day previews as well as full-day plans. Earlier chosen ends are preserved;
an interval shorter than 120 minutes after adjustment is rejected. The UI shows
the effective next use date/window separately from unsaved input. Already
accepted records are never shortened or cancelled by this normalization.

The initial policy-only validation kept the real submission gate disabled.
Current execution requires an explicit live launcher and an individually enabled
plan. Tests use synthetic accounts and school boundaries; public policy facts
do not establish a successful release-time booking.
