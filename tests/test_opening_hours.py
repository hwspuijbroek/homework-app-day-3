"""
Tests for the opening-hours reader.

Two properties matter, and the second one more than the first: it must be right
when it answers, and it must refuse to answer whenever the string steps outside
the subset it understands. A wrong "open" sends somebody to a closed door with a
confident sentence in their pocket, which is exactly the failure this project
spends its comments on.

The strings below are real shapes from the OSM venue corpus.
"""

from datetime import date

import pytest

from opening_hours import open_on

# A week of known weekdays, so the tests read as days rather than as dates.
MONDAY = date(2026, 8, 24)
TUESDAY = date(2026, 8, 25)
WEDNESDAY = date(2026, 8, 26)
THURSDAY = date(2026, 8, 27)
FRIDAY = date(2026, 8, 28)
SATURDAY = date(2026, 8, 29)
SUNDAY = date(2026, 8, 30)


def test_the_case_that_started_this():
    """
    The Geniemuseum, recommended for a Saturday by an agent holding these very
    hours. August matches the second rule's month range; Saturday does not match
    its Sunday.
    """
    hours = "Tu-Th 10:00-16:00; Jul-Aug Su 10:00-16:00"
    assert open_on(hours, SATURDAY) is False
    assert open_on(hours, WEDNESDAY) is True
    assert open_on(hours, SUNDAY) is True          # August, so the summer rule applies
    assert open_on(hours, date(2026, 3, 1)) is False   # a March Sunday: outside Jul-Aug


# --- the shapes it understands ------------------------------------------------

def test_a_weekday_range():
    assert open_on("Mo-Fr 09:00-17:00", WEDNESDAY) is True
    assert open_on("Mo-Fr 09:00-17:00", SATURDAY) is False


def test_a_weekday_list():
    assert open_on("Mo,We,Fr 09:00-17:00", WEDNESDAY) is True
    assert open_on("Mo,We,Fr 09:00-17:00", TUESDAY) is False


def test_a_range_that_wraps_into_the_next_week():
    """"Sa-Mo" is a real shape; read naively it is an empty range."""
    assert open_on("Sa-Mo 10:00-18:00", SUNDAY) is True
    assert open_on("Sa-Mo 10:00-18:00", MONDAY) is True
    assert open_on("Sa-Mo 10:00-18:00", WEDNESDAY) is False


def test_hours_without_a_weekday_apply_every_day():
    assert open_on("10:00-17:00", SUNDAY) is True


def test_split_shifts_are_still_one_day():
    """Whether 12:30 is inside the gap is a question about times, not days."""
    assert open_on("Mo-Fr 09:00-12:00,13:00-17:00", TUESDAY) is True


def test_an_open_ended_time_is_still_a_time():
    """
    "Mo,Tu 13:00+" means it opens at one and does not say when it shuts. That is
    a statement about hours, and this module is only asked about days.
    """
    assert open_on("Mo,Tu 13:00+; We-Fr 11:00+; Sa,Su 12:00+", SATURDAY) is True
    assert open_on("We 14:00+; Th-Sa 15:00+", MONDAY) is False


def test_24_7():
    assert open_on("24/7", SATURDAY) is True


def test_a_closing_rule_beats_an_earlier_opening_one():
    """"Mo-Su …; Sa off" — the exception is the whole point of the second rule."""
    assert open_on("Mo-Su 10:00-17:00; Sa off", SATURDAY) is False
    assert open_on("Mo-Su 10:00-17:00; Sa off", FRIDAY) is True


def test_a_month_range_narrows_the_season():
    assert open_on("Apr-Sep Mo-Su 10:00-18:00", SATURDAY) is True
    assert open_on("Apr-Sep Mo-Su 10:00-18:00", date(2026, 1, 3)) is False


def test_holiday_rules_are_skipped_not_guessed_at():
    """
    Whether a date is a public holiday is a question about the Dutch calendar
    that this module has no business answering. Skipping the rule keeps the rest
    of the string usable.
    """
    assert open_on("Mo-Fr 09:00-17:00; PH off", WEDNESDAY) is True
    assert open_on("Mo-Fr 09:00-17:00; PH off", SATURDAY) is False


# --- everything else is refused, on purpose -----------------------------------

@pytest.mark.parametrize("spec", [
    None,
    "",
    "   ",
    "op afspraak",                       # free text, and common
    'Mo-Fr 09:00-17:00 "bel even"',      # a quoted comment
    "Mar 15-Oct 31 10:00-18:00",         # a date range, not a month range
    "sunrise-sunset",
    "week 1-20 Mo-Fr 09:00-17:00",
    "Mo-Fr 09:00-17:00; Su[1] 12:00-16:00",   # first Sunday of the month
])
def test_anything_outside_the_subset_returns_unknown(spec):
    assert open_on(spec, SATURDAY) is None


def test_one_unreadable_rule_makes_the_whole_string_unknown():
    """
    The part that was skipped could be the "Sa off" that decides the answer, so
    a half-read string is not a basis for saying "open".
    """
    assert open_on("Mo-Fr 09:00-17:00; Su sunrise-sunset", WEDNESDAY) is None


def test_a_typo_is_unknown_rather_than_ignored():
    assert open_on("Mon-Fri 09:00-17:00", WEDNESDAY) is None
