"""
Tests for the judgement layer — the part the assignment grades as "more than a
passthrough of the raw API".

Adapted from Day 2's tests/test_best_day.py and tests/test_weather_domain.py,
narrowed to the two functions that moved into verdict.py. What matters here is
not that the numbers are "right" — a threshold is a choice — but that the
documented reasoning holds: amount before probability, a comfort band rather
than a maximum, and 'gemengd' whenever the forecast does not support more.
"""

import pytest

from verdict import day_score, nl_number, outdoor_verdict


def day(date="2026-08-26", rain=None, high=None, wind=None, sun=None,
        mm=None, description="", partial=False):
    return {"date": date, "rain_chance": rain, "max_temperature": high,
            "windforce": wind, "sun_chance": sun,
            "daytime_precipitation_mm": mm, "description": description,
            "is_partial": partial}


# --- outdoor_verdict ---------------------------------------------------------

def test_a_dry_mild_day_is_a_day_to_be_outside():
    advice, reason = outdoor_verdict(day(mm=0.0, rain=10, high=22))
    assert advice == "buiten"
    assert "22" in reason


def test_heavy_daytime_rain_sends_you_indoors_on_amount_alone():
    advice, reason = outdoor_verdict(day(mm=4.0, rain=30, high=21))
    assert advice == "binnen"
    assert "mm" in reason


def test_a_high_chance_of_a_trace_of_drizzle_does_not_veto_the_day():
    """
    The bug this rule exists for: an 80% chance of 0,2 mm is a fine afternoon,
    and ranking on chance alone once vetoed a zoo trip over a rounding error.
    """
    advice, _ = outdoor_verdict(day(mm=0.2, rain=80, high=22))
    assert advice != "binnen"


def test_a_high_chance_without_an_amount_still_sends_you_indoors():
    """With no millimetres to weigh, probability is all there is."""
    advice, reason = outdoor_verdict(day(mm=None, rain=70, high=21))
    assert advice == "binnen"
    assert "70%" in reason


def test_cold_and_heat_both_send_you_indoors():
    assert outdoor_verdict(day(mm=0.0, rain=0, high=2))[0] == "binnen"
    assert outdoor_verdict(day(mm=0.0, rain=0, high=33))[0] == "binnen"


def test_a_gale_sends_you_indoors_on_a_dry_day():
    assert outdoor_verdict(day(mm=0.0, rain=5, high=20, wind=8))[0] == "binnen"


def test_a_thunderstorm_description_vetoes_an_otherwise_fine_day():
    """
    Severe-weather codes carry a low *daily* rain figure — "kans op enkele
    pittige (onweers)buien" looks dry in the numbers and is not a day to be out.
    """
    advice, reason = outdoor_verdict(
        day(mm=0.3, rain=25, high=23,
            description="Opklaringen en kans op enkele pittige (onweers)buien"))
    assert advice == "gemengd"
    assert "onweer" in reason


def test_an_in_between_day_is_neither_rather_than_a_guess():
    advice, _ = outdoor_verdict(day(mm=0.8, rain=35, high=16))
    assert advice == "gemengd"


def test_a_dry_day_with_a_shower_risk_says_both_things():
    """"Prima weer met 51% kans op regen" reads as a contradiction; it is one."""
    advice, reason = outdoor_verdict(day(mm=0.1, rain=45, high=21))
    assert advice == "buiten"
    assert "45%" in reason


@pytest.mark.parametrize("bad", [None, {}, {"rain_chance": "nogal"}])
def test_missing_or_unreadable_input_degrades_instead_of_raising(bad):
    advice, reason = outdoor_verdict(bad)
    assert advice == "gemengd"
    assert reason


# --- day_score ---------------------------------------------------------------

def test_a_dry_day_outranks_a_wet_one():
    dry, _ = day_score(day(mm=0.0, rain=10, high=22))
    wet, _ = day_score(day(mm=8.0, rain=80, high=22))
    assert dry > wet


def test_ranking_follows_millimetres_not_percentages():
    """
    Day 2's ranking bug: scoring on rain_chance alone put the wettest day of the
    week at the top, because a high-mm day happened to carry a modest percentage.
    """
    drizzle_likely, _ = day_score(day(mm=0.2, rain=80, high=21))
    downpour_unlikely, _ = day_score(day(mm=12.0, rain=40, high=21))
    assert drizzle_likely > downpour_unlikely


def test_comfort_is_a_band_not_a_maximum():
    pleasant, _ = day_score(day(mm=0.0, rain=5, high=22))
    scorching, _ = day_score(day(mm=0.0, rain=5, high=33))
    assert pleasant > scorching


def test_sun_is_a_bonus_and_never_decisive():
    sunny_but_wet, _ = day_score(day(mm=6.0, rain=70, high=20, sun=90))
    dull_but_dry, _ = day_score(day(mm=0.0, rain=10, high=20, sun=10))
    assert dull_but_dry > sunny_but_wet


def test_the_score_stays_inside_its_range():
    assert day_score(day(mm=40.0, rain=100, high=-5, wind=11))[0] == 0
    assert day_score(day(mm=0.0, rain=0, high=22, sun=100))[0] <= 100


def test_the_reason_names_the_figures_it_used():
    _, factors = day_score(day(mm=1.5, rain=40, high=19, wind=5, sun=30))
    for expected in ("mm", "%", "graden", "windkracht"):
        assert expected in factors


def test_an_empty_day_scores_zero_rather_than_raising():
    assert day_score(None) == (0, "geen verwachting")


# --- nl_number ---------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [(3.0, "3"), (0.5, "0,5"), (14.25, "14,2")])
def test_numbers_get_a_dutch_decimal_comma(value, expected):
    assert nl_number(value) == expected
