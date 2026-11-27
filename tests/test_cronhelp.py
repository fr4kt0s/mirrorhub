"""Schedule helper: cron expression <-> frequency, weekdays and time."""

import pytest

from mirrorhub import cronhelp
from mirrorhub.cronhelp import Helper


@pytest.mark.parametrize(
    ("expr", "freq", "text"),
    [
        ("15 * * * *", "hourly", "Every hour at minute 15"),
        ("0 */6 * * *", "hours", "Every 6 hours at minute 0 (00, 06, 12, 18 h UTC)"),
        ("30 3/6 * * *", "hours", "Every 6 hours at minute 30 (03, 09, 15, 21 h UTC)"),
        ("0 3 * * *", "daily", "Daily at 03:00 UTC"),
        ("0 4 * * 1-5", "weekly", "Every Mon, Tue, Wed, Thu, Fri at 04:00 UTC"),
        ("0 4 * * 1,3,5", "weekly", "Every Mon, Wed, Fri at 04:00 UTC"),
        ("0 4 * * 0,6", "weekly", "Every Sat, Sun at 04:00 UTC"),
        ("0 3 1 * *", "monthly", "Monthly on day 1 at 03:00 UTC"),
    ],
)
def test_round_trip(expr: str, freq: str, text: str) -> None:
    helper = cronhelp.parse(expr)
    assert helper.freq == freq
    assert cronhelp.build(helper) == expr
    assert cronhelp.describe(expr) == text


@pytest.mark.parametrize(
    "expr", ["*/5 * * * *", "0 3 * 1 *", "0 3 1 * 1", "0 */5 * * *", "0 3-5 * * *", "x", ""]
)
def test_other_expressions_are_custom(expr: str) -> None:
    assert cronhelp.parse(expr).freq == "custom"
    assert cronhelp.describe(expr) == ""


def test_sunday_as_seven_and_all_days() -> None:
    assert cronhelp.parse("0 4 * * 7").days == [0]
    assert cronhelp.build(Helper(freq="weekly", hour=4, days=list(range(7)))) == "0 4 * * *"
    assert cronhelp.build(Helper(freq="weekly", hour=4, days=[])) == "0 4 * * 1"


def test_every_few_hours_normalises_the_start() -> None:
    helper = cronhelp.from_form({"cb_freq": "hours", "cb_time": "07:30", "cb_every": "6"}, [])
    assert cronhelp.build(helper) == "30 1/6 * * *"


def test_monthly_late_day_warns() -> None:
    assert "skipped in months" in cronhelp.describe("0 4 31 * *")


def test_form_values_are_clamped() -> None:
    helper = cronhelp.from_form(
        {"cb_freq": "weekly", "cb_time": "25:99", "cb_dom": "40", "cb_every": "5"},
        ["1", "3", "x", "9"],
    )
    assert (helper.hour, helper.minute, helper.days) == (23, 59, [1, 3])
    assert helper.day_of_month == 31 and helper.every == 6
    assert cronhelp.from_form({"cb_freq": "bogus"}, []).freq == "custom"
