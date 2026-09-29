"""Small shared helpers."""

import logging
from datetime import date, datetime, time as dtime, timedelta, timezone

log = logging.getLogger(__name__)

#: Shipped default, and therefore what "today" means everywhere in the app.
#: Override with `schedule.timezone` on the config page.
DEFAULT_TIMEZONE = "Europe/Berlin"


def now_iso():
    """UTC timestamp, second precision. Every stored time uses this."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_timezone(name):
    """A ZoneInfo for `name`, falling back to Berlin rather than raising.

    A typo in the config page should not take down the scheduler.
    """
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(name)
    except Exception:
        log.warning("unknown timezone %r; falling back to %s", name,
                    DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE)


def day_bounds_utc(on_date, tz):
    """(start, end) UTC timestamps spanning the local day `on_date`.

    Timestamps are stored in UTC but a day means a local day: Berlin midnight
    is 22:00 UTC the previous day in summer, 23:00 in winter. Comparing
    against `on_date + "T00:00:00+00:00"` — which the daily report used to do
    — silently shifts the window by the UTC offset.

    The end is the next day's start, so callers use a half-open range and no
    row can fall between two days or land in both.
    """
    day = date.fromisoformat(on_date) if isinstance(on_date, str) else on_date
    start = datetime.combine(day, dtime.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return (start.astimezone(timezone.utc).isoformat(),
            end.astimezone(timezone.utc).isoformat())


def parse_iso(value):
    """Parse a stored timestamp back to an aware datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def age_days(value):
    """Whole days since an ISO timestamp, or None if unparseable."""
    parsed = parse_iso(value)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).days


def humanize_age(value):
    """'today', '3 days ago', '2 weeks ago' for display."""
    days = age_days(value)
    if days is None:
        return "date unknown"
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return "{0} days ago".format(days)
    if days < 60:
        return "{0} weeks ago".format(days // 7)
    return "{0} months ago".format(days // 30)
