"""Schedule helper: frequency, weekdays and time of day <-> cron expression.

The schedule form shows a small helper next to the cron field. It covers the
common cases (every hour, every few hours, daily, weekly on chosen weekdays,
monthly on a day) and falls back to "custom" for any other expression, which
stays editable in the cron field. Times are UTC, like the scheduler.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

FREQUENCIES: tuple[tuple[str, str], ...] = (
    ("hourly", "Every hour"),
    ("hours", "Every few hours"),
    ("daily", "Daily"),
    ("weekly", "Weekly, on chosen weekdays"),
    ("monthly", "Monthly, on one day"),
    ("custom", "Custom (edit the cron expression)"),
)
FREQUENCY_KEYS = frozenset(key for key, _ in FREQUENCIES)

# Cron weekday numbers in display order (Monday first); cron uses 0 for Sunday.
WEEKDAYS: tuple[tuple[int, str], ...] = (
    (1, "Mon"),
    (2, "Tue"),
    (3, "Wed"),
    (4, "Thu"),
    (5, "Fri"),
    (6, "Sat"),
    (0, "Sun"),
)
DAY_NAMES = dict(WEEKDAYS)
INTERVALS: tuple[int, ...] = (2, 3, 4, 6, 8, 12)

_NUMBER = r"(\d{1,2})"


@dataclass
class Helper:
    """The helper's fields; ``freq`` is one of the FREQUENCIES keys."""

    freq: str = "daily"
    minute: int = 0
    hour: int = 3
    every: int = 6
    days: list[int] = field(default_factory=lambda: [1])
    day_of_month: int = 1

    @property
    def time(self) -> str:
        """Time of day as ``HH:MM`` (the value of the form's time input)."""
        return f"{self.hour:02d}:{self.minute:02d}"


def build(helper: Helper) -> str | None:
    """Return the cron expression for the helper fields, or None for "custom"."""
    minute, hour = helper.minute, helper.hour
    if helper.freq == "hourly":
        return f"{minute} * * * *"
    if helper.freq == "hours":
        start = hour % helper.every
        hours = f"*/{helper.every}" if start == 0 else f"{start}/{helper.every}"
        return f"{minute} {hours} * * *"
    if helper.freq == "daily":
        return f"{minute} {hour} * * *"
    if helper.freq == "weekly":
        days = sorted(set(helper.days)) or [1]
        return f"{minute} {hour} * * {_days_field(days)}"
    if helper.freq == "monthly":
        return f"{minute} {hour} {helper.day_of_month} * *"
    return None


def _days_field(days: list[int]) -> str:
    """Weekdays as a cron field; consecutive runs become ranges (1-5)."""
    if days == list(range(7)):
        return "*"
    parts: list[str] = []
    start = previous = days[0]
    for day in [*days[1:], None]:
        if day is not None and day == previous + 1:
            previous = day
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        if day is not None:
            start = previous = day
    return ",".join(parts)


def _parse_days(text: str) -> list[int] | None:
    days: set[int] = set()
    for part in text.split(","):
        match = re.fullmatch(r"(\d)(?:-(\d))?", part)
        if match is None:
            return None
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first > last or last > 7:
            return None
        days.update(day % 7 for day in range(first, last + 1))
    return sorted(days)


def parse(expr: str) -> Helper:
    """Return the helper fields for a cron expression.

    ``freq`` is "custom" if the helper cannot show the expression.
    """
    fields = expr.split()
    custom = Helper(freq="custom")
    if len(fields) != 5:
        return custom
    minute_text, hour_text, dom_text, month_text, dow_text = fields
    if month_text != "*" or not re.fullmatch(_NUMBER, minute_text):
        return custom
    minute = int(minute_text)
    if minute > 59:
        return custom

    if hour_text == "*" and dom_text == "*" and dow_text == "*":
        return Helper(freq="hourly", minute=minute)

    step = re.fullmatch(r"(\*|\d{1,2})/(\d{1,2})", hour_text)
    if step and dom_text == "*" and dow_text == "*":
        every = int(step.group(2))
        start = 0 if step.group(1) == "*" else int(step.group(1))
        if every in INTERVALS and start < every:
            return Helper(freq="hours", minute=minute, hour=start, every=every)
        return custom

    if not re.fullmatch(_NUMBER, hour_text) or int(hour_text) > 23:
        return custom
    hour = int(hour_text)
    if dom_text == "*" and dow_text == "*":
        return Helper(freq="daily", minute=minute, hour=hour)
    if dom_text == "*":
        days = list(range(7)) if dow_text == "*" else _parse_days(dow_text)
        if days:
            return Helper(freq="weekly", minute=minute, hour=hour, days=days)
        return custom
    if dow_text == "*" and re.fullmatch(_NUMBER, dom_text) and 1 <= int(dom_text) <= 31:
        return Helper(freq="monthly", minute=minute, hour=hour, day_of_month=int(dom_text))
    return custom


def describe(expr: str) -> str:
    """Describe the expression in a short English sentence.

    Returns "" if the helper cannot show the expression.
    """
    helper = parse(expr)
    if helper.freq == "hourly":
        return f"Every hour at minute {helper.minute}"
    if helper.freq == "hours":
        hours = ", ".join(f"{hour:02d}" for hour in range(helper.hour, 24, helper.every))
        return f"Every {helper.every} hours at minute {helper.minute} ({hours} h UTC)"
    if helper.freq == "daily":
        return f"Daily at {helper.time} UTC"
    if helper.freq == "weekly":
        if len(helper.days) == 7:
            return f"Daily at {helper.time} UTC"
        names = ", ".join(DAY_NAMES[day] for day, _ in WEEKDAYS if day in helper.days)
        return f"Every {names} at {helper.time} UTC"
    if helper.freq == "monthly":
        text = f"Monthly on day {helper.day_of_month} at {helper.time} UTC"
        if helper.day_of_month > 28:
            text += " (skipped in months without that day)"
        return text
    return ""


def _int(form: Mapping[str, object], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(form.get(name) or default))
    except ValueError:
        return default
    return min(max(value, low), high)


def from_form(form: Mapping[str, object], days: list[str]) -> Helper:
    """Read the helper fields from the submitted form (``cb_*`` fields).

    Out-of-range values are clamped; an unknown frequency becomes "custom".
    """
    freq = str(form.get("cb_freq") or "custom")
    if freq not in FREQUENCY_KEYS:
        freq = "custom"
    hour, minute = 3, 0
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(form.get("cb_time") or ""))
    if match:
        hour, minute = min(int(match.group(1)), 23), min(int(match.group(2)), 59)
    if freq == "hourly":
        minute = _int(form, "cb_minute", minute, 0, 59)
    every = _int(form, "cb_every", 6, 2, 12)
    if every not in INTERVALS:
        every = 6
    weekdays = sorted({int(day) for day in days if day.isdigit() and int(day) <= 6})
    return Helper(
        freq=freq,
        minute=minute,
        hour=hour,
        every=every,
        days=weekdays,
        day_of_month=_int(form, "cb_dom", 1, 1, 31),
    )
