"""
Tests for the per-coordinate forecast parser — the most load-bearing code here.

`fetch_local_forecast` turns Buienradar's per-location response into the day
dicts that everything downstream reasons about, and one field in particular:
`daytime_precipitation_mm`. That figure decides 'binnen' at 3 mm, drives the
ranking in day_score, and is the whole reason the verdict weighs amount before
probability. Everything above it was tested and it was not — the tests carried
over from Day 2 cover the *station* feed, and this is a different endpoint.

The shapes below are faithful to a real response captured from
forecast.buienradar.nl: the same key names (`precipitationmm`, `beaufort`,
`iconcode`), the same day-part dicts under morning/afternoon/evening/night, the
same hours nested per day. Two properties of the real feed matter and are
reproduced here:

  - The day-level `precipitation` field is not a probability. On 2026-08-17 it
    read 26 next to 14.2 mm of rain. The per-block figures are the real chances,
    and only the daytime ones are worth anything for a day out.
  - Days beyond about five carry no morning/afternoon/evening at all — only
    day-level values. That is where `daytime_precipitation_mm` becomes None and
    the caveat about "chance without an amount" earns its place.
"""

import pytest
import requests
import responses

from weather_client import BUIENRADAR_LOCAL_FORECAST_URL, WeatherClient


def block(part, mm=0.0, chance=0, high=20.0):
    """One of Buienradar's day parts, with its real key names."""
    return {"timetype": part, "precipitationmm": mm, "precipitation": chance,
            "maxtemperature": high, "mintemperature": high - 4, "beaufort": 2}


def hour(when, mm=0.0, chance=0, temperature=18.0, iconcode="a"):
    return {"datetime": when, "timetype": "Hour", "precipitationmm": mm,
            "precipitation": chance, "temperature": temperature,
            "feeltemperature": temperature, "windspeedms": 2.0,
            "winddirection": "ZW", "humidity": 70, "iconcode": iconcode}


def day(date="2026-08-28", *, parts=None, hours=None, mm=0.0, chance=0,
        iconcode="a", **extra):
    """
    A forecast day. `parts` is a dict like {"morning": block(...)}; pass none at
    all for the far-out days, which really do arrive without them.
    """
    out = {
        "date": f"{date}T00:00:00",
        "datetime": f"{date}T00:00:00",
        "timetype": "Day",
        "precipitationmm": mm,          # the full 24 hours
        "precipitation": chance,        # NOT a probability; see the module docstring
        "mintemperature": 14.6,
        "maxtemperature": 21.5,
        "beaufort": 3,
        "sunshine": 40,
        "uvindex": 4,
        "humidity": 72,
        "iconcode": iconcode,
        "hours": hours or [],
    }
    out.update(parts or {})
    out.update(extra)
    return out


def fetch(rows, **kwargs):
    """
    Run the parser against a stubbed response.

    The parameter is `rows` rather than `days` so that `fetch(..., days=2)` means
    what it says — the argument the parser takes, not the payload.
    """
    responses.add(responses.GET, BUIENRADAR_LOCAL_FORECAST_URL,
                  json={"location": {"name": "Drunen"}, "days": rows}, status=200)
    return WeatherClient().fetch_local_forecast(51.68, 5.13, **kwargs)


# --- daytime precipitation: the figure everything else rests on ---------------

@responses.activate
def test_daytime_rain_is_the_daytime_blocks_added_up():
    result = fetch([day(parts={
        "morning": block("Morning", mm=0.4),
        "afternoon": block("Afternoon", mm=2.1),
        "evening": block("Evening", mm=0.5),
        "night": block("Night", mm=9.6),
    })])
    assert result["days"][0]["daytime_precipitation_mm"] == pytest.approx(3.0)


@responses.activate
def test_rain_at_night_does_not_count_against_the_day():
    """
    Measured on 2026-08-17: 9,6 of the day's 14,2 mm fell at night and the
    afternoon was dry. Counting the night made it the wettest day of the week.
    """
    result = fetch([day(mm=14.2, parts={
        "morning": block("Morning", mm=0.0),
        "afternoon": block("Afternoon", mm=0.0),
        "evening": block("Evening", mm=0.0),
        "night": block("Night", mm=9.6),
    })])
    parsed = result["days"][0]
    assert parsed["daytime_precipitation_mm"] == 0.0
    # The 24-hour figure is still reported, just not as the one to decide on.
    assert parsed["precipitation_mm"] == 14.2


@responses.activate
def test_the_rain_chance_is_the_worst_daytime_block_not_the_day_field():
    """The day-level `precipitation` is not a probability, whatever it looks like."""
    result = fetch([day(chance=26, parts={
        "morning": block("Morning", chance=20),
        "afternoon": block("Afternoon", chance=80),
        "evening": block("Evening", chance=45),
    })])
    assert result["days"][0]["rain_chance"] == 80


@responses.activate
def test_a_day_without_parts_falls_back_to_the_day_level_chance():
    """
    Five or more days out, Buienradar sends no morning/afternoon/evening. Then
    an amount is genuinely unknown — and saying so is what triggers the caveat
    the agent has to repeat.
    """
    parsed = fetch([day(mm=1.2, chance=77)])["days"][0]
    assert parsed["daytime_precipitation_mm"] is None
    assert parsed["rain_chance"] == 77
    assert parsed["day_parts"] == []


@responses.activate
def test_the_day_parts_travel_along_for_the_agent_to_read():
    parsed = fetch([day(parts={
        "morning": block("Morning", mm=0.0, chance=10, high=18.0),
        "afternoon": block("Afternoon", mm=1.5, chance=60, high=22.0),
        "evening": block("Evening", mm=0.0, chance=15, high=20.0),
        "night": block("Night", mm=4.0, chance=90),
    })])["days"][0]

    assert [p["part"] for p in parsed["day_parts"]] == ["Morning", "Afternoon", "Evening"]
    assert parsed["day_parts"][1] == {"part": "Afternoon", "precipitation_mm": 1.5,
                                      "rain_chance": 60, "max_temperature": 22.0}


# --- the partial day ----------------------------------------------------------

@responses.activate
def test_today_is_flagged_partial_when_only_some_hours_are_left():
    """
    Late in the evening today's min/max covers three hours, not a day — 20,7 to
    21,2 where the whole day was 19,6 to 24,4. Presenting that as the day's
    forecast is a quiet lie.
    """
    evening = [hour(f"2026-08-26T{h:02d}:00:00") for h in (21, 22, 23)]
    assert fetch([day("2026-08-26", hours=evening)])["days"][0]["is_partial"] is True


@responses.activate
def test_a_full_first_day_is_not_partial():
    whole = [hour(f"2026-08-26T{h:02d}:00:00") for h in range(24)]
    assert fetch([day("2026-08-26", hours=whole)])["days"][0]["is_partial"] is False


@responses.activate
def test_only_the_first_day_can_be_partial():
    """A later day with few hours is a sparse forecast, not a day half gone."""
    days = [day("2026-08-26", hours=[hour("2026-08-26T23:00:00")]),
            day("2026-08-27", hours=[hour("2026-08-27T12:00:00")])]
    assert [d["is_partial"] for d in fetch(days)["days"]] == [True, False]


@responses.activate
def test_a_day_without_any_hours_is_not_called_partial():
    assert fetch([day("2026-08-26")])["days"][0]["is_partial"] is False


# --- hours --------------------------------------------------------------------

@responses.activate
def test_hours_are_flattened_so_the_next_24_can_cross_midnight():
    days = [day("2026-08-26", hours=[hour(f"2026-08-26T{h:02d}:00:00") for h in (22, 23)]),
            day("2026-08-27", hours=[hour(f"2026-08-27T{h:02d}:00:00") for h in (0, 1)])]
    times = [h["time"] for h in fetch(days)["hours"]]
    assert times == ["2026-08-26T22:00:00", "2026-08-26T23:00:00",
                     "2026-08-27T00:00:00", "2026-08-27T01:00:00"]


@responses.activate
def test_the_hour_count_is_respected():
    days = [day("2026-08-26", hours=[hour(f"2026-08-26T{h:02d}:00:00") for h in range(24)])]
    assert len(fetch(days, hours=6)["hours"]) == 6


@responses.activate
def test_an_hours_fields_are_renamed_not_reinterpreted():
    """precipitationmm is millimetres and precipitation is a percentage; the
    names they arrive under invite exactly the mix-up this project keeps hitting."""
    parsed = fetch([day(hours=[hour("2026-08-26T15:00:00", mm=0.7, chance=80,
                                    temperature=19.4)])])["hours"][0]
    assert parsed["precipitation_mm"] == 0.7
    assert parsed["precipitation_chance"] == 80
    assert parsed["temperature"] == 19.4
    assert parsed["time"] == "2026-08-26T15:00:00"


# --- descriptions and the request itself --------------------------------------

@responses.activate
def test_a_condition_code_becomes_a_readable_description():
    parsed = fetch([day(iconcode="q")])["days"][0]
    assert parsed["description"] == "Zwaar bewolkt en regen"


@responses.activate
def test_the_code_that_was_once_a_bare_string():
    """
    Code 'r' was added to the table as a string instead of a list, so lookups
    indexed into the *text* and described the weather as "a". It is a list now,
    and this is the test that says so.
    """
    assert fetch([day(iconcode="r")])["days"][0]["description"] == "Zwaar bewolkt en droog"


@responses.activate
def test_a_code_with_trailing_whitespace_still_resolves():
    """The feed pads some codes; unpadded lookups silently returned nothing."""
    assert fetch([day(iconcode="q ")])["days"][0]["description"] == "Zwaar bewolkt en regen"


@responses.activate
def test_an_unknown_code_degrades_to_an_empty_description():
    assert fetch([day(iconcode="zzz")])["days"][0]["description"] == ""


@responses.activate
def test_the_day_count_is_respected():
    days = [day(f"2026-08-{26 + n}") for n in range(5)]
    assert len(fetch(days, days=2)["days"]) == 2


@responses.activate
def test_the_coordinates_are_rounded_before_they_are_sent():
    """Four decimals is ~11 m. More is false precision and defeats the cache."""
    responses.add(responses.GET, BUIENRADAR_LOCAL_FORECAST_URL,
                  json={"location": {}, "days": []}, status=200)
    WeatherClient().fetch_local_forecast(51.6812345, 5.1298765)
    sent = responses.calls[0].request.url
    assert "lat=51.6812" in sent and "lon=5.1299" in sent


@responses.activate
def test_an_http_error_is_raised_for_the_service_layer_to_translate():
    """
    weather_service turns this into ServiceUnavailable; swallowing it here would
    leave the agent with an empty forecast and no idea the source was down.
    """
    responses.add(responses.GET, BUIENRADAR_LOCAL_FORECAST_URL, status=503)
    with pytest.raises(requests.HTTPError):
        WeatherClient().fetch_local_forecast(51.68, 5.13)


@responses.activate
def test_the_location_block_is_passed_through():
    responses.add(responses.GET, BUIENRADAR_LOCAL_FORECAST_URL,
                  json={"location": {"name": "Drunen", "country": "NL"}, "days": []},
                  status=200)
    assert WeatherClient().fetch_local_forecast(51.68, 5.13)["location"]["name"] == "Drunen"
