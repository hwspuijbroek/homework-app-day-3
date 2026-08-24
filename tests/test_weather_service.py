"""
Tests for the layer that combines the adapters.

Every boundary is stubbed — no Buienradar, no Nominatim, no Lakebase — so what
is under test is the combining itself: which failure becomes which exception,
how "morgen" turns into a date, which station is nearest, and what the hourly
rows say once the hours that have already passed are dropped.

Dates are built relative to *now* rather than hardcoded, because resolve_day
reads the clock in Europe/Amsterdam and a fixed date would rot within a week.
"""

from datetime import date, datetime, timedelta

import pytest

import weather_service
from geocode import GeocodingUnavailable
from weather_service import (
    AMSTERDAM,
    LocationUnknown,
    NoForecastForDay,
    ServiceUnavailable,
)

TODAY = datetime.now(AMSTERDAM).date()
TOMORROW = TODAY + timedelta(days=1)

DRUNEN = {"lat": 51.68, "lon": 5.13, "display_name": "Drunen, Noord-Brabant",
          "province": "Noord-Brabant", "ambiguous": False, "alternatives": []}

BERGEN = {"lat": 52.66, "lon": 4.70, "display_name": "Bergen, Noord-Holland",
          "province": "Noord-Holland", "ambiguous": True,
          "alternatives": [{"display_name": "Bergen, Noord-Holland",
                            "province": "Noord-Holland", "lat": 52.66, "lon": 4.70},
                           {"display_name": "Bergen, Limburg",
                            "province": "Limburg", "lat": 51.58, "lon": 6.05}]}


def forecast_day(date, **overrides):
    day = {"date": date.isoformat(), "min_temperature": 12, "max_temperature": 21,
           "daytime_precipitation_mm": 0.0, "rain_chance": 10, "sun_chance": 60,
           "windforce": 3, "description": "Half bewolkt", "uv_index": 4,
           "day_parts": [], "is_partial": False}
    day.update(overrides)
    return day


def hour(when, mm=0.0, chance=5):
    # Buienradar returns local wall-clock time without an offset, so these are
    # naive on purpose — the service compares them against Dutch local time.
    return {"time": when.replace(tzinfo=None).isoformat(),
            "precipitation_mm": mm, "precipitation_chance": chance,
            "temperature": 18}


class FakeWeatherClient:
    """Stands in for weather_client.WeatherClient; records nothing, invents nothing."""

    days = None
    hours = None
    stations = None
    error = None

    def __init__(self, *args, **kwargs):
        pass

    def fetch_local_forecast(self, lat, lon, hours=24, days=5):
        if FakeWeatherClient.error:
            raise FakeWeatherClient.error
        return {"location": {}, "days": FakeWeatherClient.days or [],
                "hours": FakeWeatherClient.hours or []}

    def fetch_weather(self, locations=None):
        if FakeWeatherClient.error:
            raise FakeWeatherClient.error
        return FakeWeatherClient.stations or []


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Both caches are module-level; a leaked entry would answer the next test."""
    weather_service._forecast_cache.clear()
    weather_service._stations_cache = None
    FakeWeatherClient.days = [forecast_day(TODAY), forecast_day(TOMORROW)]
    FakeWeatherClient.hours = []
    FakeWeatherClient.stations = []
    FakeWeatherClient.error = None
    monkeypatch.setattr(weather_service, "WeatherClient", FakeWeatherClient)
    monkeypatch.setattr(weather_service, "geocode_place", lambda name: DRUNEN)
    yield
    weather_service._forecast_cache.clear()
    weather_service._stations_cache = None


def station(name, lat, lon, **overrides):
    payload = {"stationname": name, "lat": lat, "lon": lon, "temperature": 21.4,
               "weatherdescription": "Half bewolkt", "humidity": 62,
               "windspeed": 3.4, "windforce": 2, "timestamp": "2026-08-24T14:00:00"}
    payload.update(overrides)
    return {"source_type": "forecast", "location": name, "payload": payload}


# --- resolve_location ---------------------------------------------------------

def test_an_unresolvable_place_is_its_own_failure(monkeypatch):
    monkeypatch.setattr(weather_service, "geocode_place", lambda name: None)
    with pytest.raises(LocationUnknown) as excinfo:
        weather_service.resolve_location("Chicago")
    # The message has to carry the scope, or the agent cannot explain the refusal.
    assert "Nederland" in str(excinfo.value)


def test_a_down_geocoder_is_a_different_failure(monkeypatch):
    def boom(name):
        raise GeocodingUnavailable("timeout")
    monkeypatch.setattr(weather_service, "geocode_place", boom)
    with pytest.raises(ServiceUnavailable):
        weather_service.resolve_location("Drunen")


# --- resolve_day --------------------------------------------------------------

def test_no_day_given_means_today():
    days = [forecast_day(TODAY), forecast_day(TOMORROW)]
    assert weather_service.resolve_day(days, None)["date"] == TODAY.isoformat()


@pytest.mark.parametrize("word,offset", [("vandaag", 0), ("morgen", 1), ("Morgen", 1)])
def test_the_dutch_day_words_resolve(word, offset):
    days = [forecast_day(TODAY + timedelta(days=n)) for n in range(3)]
    expected = (TODAY + timedelta(days=offset)).isoformat()
    assert weather_service.resolve_day(days, word)["date"] == expected


def test_an_iso_date_resolves():
    days = [forecast_day(TODAY), forecast_day(TOMORROW)]
    assert weather_service.resolve_day(days, TOMORROW.isoformat())["date"] == TOMORROW.isoformat()


def test_a_day_past_the_horizon_raises_instead_of_returning_the_nearest_one():
    """
    "Zaterdag" answered with Wednesday's weather is worse than no answer: it is
    wrong in a way neither the agent nor the reader can see.
    """
    days = [forecast_day(TODAY), forecast_day(TOMORROW)]
    with pytest.raises(NoForecastForDay) as excinfo:
        weather_service.resolve_day(days, (TODAY + timedelta(days=9)).isoformat())
    assert TODAY.isoformat() in str(excinfo.value)   # says what it *does* cover


def test_an_unparseable_day_says_what_it_accepts():
    days = [forecast_day(TODAY)]
    with pytest.raises(NoForecastForDay) as excinfo:
        weather_service.resolve_day(days, "volgende zomer")
    assert "JJJJ-MM-DD" in str(excinfo.value)


def test_an_empty_forecast_raises():
    with pytest.raises(NoForecastForDay):
        weather_service.resolve_day([], None)


# --- stations and current conditions -----------------------------------------

def test_the_nearest_station_wins():
    FakeWeatherClient.stations = [
        station("Meetstation Volkel", 51.65, 5.70),
        station("Meetstation Gilze Rijen", 51.57, 4.93),
        station("Meetstation Leeuwarden", 53.22, 5.75),
    ]
    assert weather_service.nearest_station(51.68, 5.13)["stationname"] == "Meetstation Gilze Rijen"


def test_stations_without_coordinates_are_skipped():
    FakeWeatherClient.stations = [
        station("Meetstation Nergens", None, None),
        station("Meetstation Volkel", 51.65, 5.70),
    ]
    assert len(weather_service.stations()) == 1


def test_only_current_conditions_documents_are_treated_as_stations():
    """fetch_weather also returns forecast/outlook documents; they have no station."""
    FakeWeatherClient.stations = [
        station("Meetstation Volkel", 51.65, 5.70),
        {"source_type": "forecast_daily", "location": "Nederland", "payload": {}},
        {"source_type": "outlook", "location": "Nederland", "payload": {}},
    ]
    assert [s["stationname"] for s in weather_service.stations()] == ["Meetstation Volkel"]


def test_current_conditions_reports_how_far_away_the_measurement_was_taken():
    FakeWeatherClient.stations = [station("Meetstation Gilze Rijen", 51.57, 4.93)]
    out = weather_service.current_conditions("Drunen")
    assert out["station"] == "Meetstation Gilze Rijen"
    assert 0 < out["distance_to_station_km"] < 30
    assert out["temperature_c"] == 21.4
    assert out["source"].startswith("Buienradar")


def test_an_upstream_outage_becomes_service_unavailable():
    import requests
    FakeWeatherClient.error = requests.exceptions.ConnectionError("no route")
    with pytest.raises(ServiceUnavailable):
        weather_service.current_conditions("Drunen")


def test_the_station_list_is_cached_within_the_ttl():
    calls = []

    class Counting(FakeWeatherClient):
        def fetch_weather(self, locations=None):
            calls.append(1)
            return [station("Meetstation Volkel", 51.65, 5.70)]

    weather_service.WeatherClient = Counting
    weather_service.stations()
    weather_service.stations()
    assert len(calls) == 1


# --- forecast -----------------------------------------------------------------

def test_the_forecast_carries_todays_date_so_the_agent_can_resolve_weekdays():
    assert weather_service.forecast("Drunen")["today"] == TODAY.isoformat()


def test_the_day_count_is_clamped_to_the_horizon():
    FakeWeatherClient.days = [forecast_day(TODAY + timedelta(days=n)) for n in range(10)]
    assert len(weather_service.forecast("Drunen", days=99)["days"]) == 7
    assert len(weather_service.forecast("Drunen", days=0)["days"]) == 1


def test_the_horizon_reaches_every_weekday_from_any_weekday():
    """
    A 5-day horizon has to refuse "zaterdag" when asked on a Monday — a question
    the source can answer, which made the weekday support pointless three days a
    week. Seven days is the smallest horizon that never has to.
    """
    days = [forecast_day(TODAY + timedelta(days=n))
            for n in range(weather_service.FORECAST_HORIZON_DAYS)]
    for name in weather_service.DUTCH_DAYS:
        assert weather_service.resolve_day(days, name)["date"]


def test_both_rain_figures_survive_into_the_output():
    """The amount and the chance answer different questions; dropping one is the bug."""
    FakeWeatherClient.days = [forecast_day(TODAY, daytime_precipitation_mm=4.2, rain_chance=55)]
    day = weather_service.forecast("Drunen")["days"][0]
    assert day["daytime_precipitation_mm"] == 4.2
    assert day["rain_chance_pct"] == 55


# --- outdoor advice -----------------------------------------------------------

def test_advice_pairs_a_verdict_with_the_figure_it_turned_on():
    FakeWeatherClient.days = [forecast_day(TODAY, daytime_precipitation_mm=5.0, rain_chance=70)]
    out = weather_service.outdoor_advice("Drunen")
    assert out["advice"] == "binnen"
    assert "mm" in out["reason"]
    assert out["forecast"]["daytime_precipitation_mm"] == 5.0


def test_a_partial_day_is_flagged_in_the_caveats():
    FakeWeatherClient.days = [forecast_day(TODAY, is_partial=True)]
    assert any("gedeeltelijke dag" in c for c in weather_service.outdoor_advice("Drunen")["caveats"])


def test_a_chance_without_an_amount_is_flagged_in_the_caveats():
    FakeWeatherClient.days = [forecast_day(TODAY, daytime_precipitation_mm=None, rain_chance=40)]
    assert any("geen hoeveelheid" in c for c in weather_service.outdoor_advice("Drunen")["caveats"])


# --- best day -----------------------------------------------------------------

def test_the_best_day_is_the_driest_one():
    FakeWeatherClient.days = [
        forecast_day(TODAY, daytime_precipitation_mm=9.0, rain_chance=90),
        forecast_day(TOMORROW, daytime_precipitation_mm=0.0, rain_chance=10),
    ]
    out = weather_service.best_day("Drunen")
    assert out["best"]["date"] == TOMORROW.isoformat()
    assert [d["date"] for d in out["ranked"]][0] == TOMORROW.isoformat()


def test_a_partial_today_comes_with_a_note():
    FakeWeatherClient.days = [forecast_day(TODAY, is_partial=True), forecast_day(TOMORROW)]
    assert weather_service.best_day("Drunen")["note"]


# --- rain timing --------------------------------------------------------------

def test_a_dry_day_says_so_and_quotes_the_highest_chance():
    noon = datetime.combine(TOMORROW, datetime.min.time()).replace(hour=12)
    FakeWeatherClient.hours = [hour(noon, mm=0.0, chance=20),
                               hour(noon + timedelta(hours=1), mm=0.0, chance=35)]
    out = weather_service.rain_timing("Drunen", "morgen")
    assert out["rain_expected"] is False
    assert out["highest_chance_pct"] == 35


def test_a_trace_of_rain_does_not_count_as_a_wet_hour():
    """0,04 mm is a damp pavement, not rain; the threshold exists to say so."""
    noon = datetime.combine(TOMORROW, datetime.min.time()).replace(hour=12)
    FakeWeatherClient.hours = [hour(noon, mm=0.04, chance=30)]
    assert weather_service.rain_timing("Drunen", "morgen")["rain_expected"] is False


def test_a_wet_hour_is_reported_as_a_block_not_a_moment():
    noon = datetime.combine(TOMORROW, datetime.min.time()).replace(hour=13)
    FakeWeatherClient.hours = [hour(noon - timedelta(hours=1), mm=0.0),
                               hour(noon, mm=0.7, chance=80),
                               hour(noon + timedelta(hours=1), mm=1.3, chance=90)]
    out = weather_service.rain_timing("Drunen", "morgen")
    assert out["rain_expected"] is True
    assert out["starts"] == "13:00–14:00"
    assert out["total_mm"] == 2.0
    assert len(out["wet_hours"]) == 2


def test_hours_that_have_already_passed_today_are_not_a_forecast():
    now = datetime.now(AMSTERDAM)
    if now.hour < 2 or now.hour > 21:
        pytest.skip("needs a few hours on either side of now within the same day")
    FakeWeatherClient.hours = [hour(now.replace(minute=0) - timedelta(hours=2), mm=5.0),
                               hour(now.replace(minute=0) + timedelta(hours=1), mm=0.0)]
    out = weather_service.rain_timing("Drunen", "vandaag")
    assert out["rain_expected"] is False


def test_a_day_without_hourly_rows_says_what_to_use_instead():
    FakeWeatherClient.days = [forecast_day(TODAY + timedelta(days=n)) for n in range(5)]
    FakeWeatherClient.hours = []
    out = weather_service.rain_timing("Drunen", (TODAY + timedelta(days=4)).isoformat())
    assert out["hourly_available"] is False
    assert "get_forecast" in out["summary"]


# --- activities ---------------------------------------------------------------

def _venues(indoor=("Museum",), outdoor=("Kasteeltuin",)):
    def fake(lat, lon, radius_km=25, limit=8, query_text=None, categories=None):
        return ([{"name": n, "shelter": "binnen", "distance_km": 4.0} for n in indoor],
                [{"name": n, "shelter": "buiten", "distance_km": 6.0} for n in outdoor])
    return fake


def test_a_wet_day_leads_with_indoor_venues(monkeypatch):
    monkeypatch.setattr(weather_service.venues_module, "nearby_venues", _venues())
    FakeWeatherClient.days = [forecast_day(TODAY, daytime_precipitation_mm=6.0, rain_chance=80)]
    out = weather_service.activities("Drunen", "iets met kinderen")
    assert out["advice"] == "binnen"
    assert out["lead_with"] == "indoor"
    assert out["indoor"] and out["outdoor"]      # both lists are still returned


def test_an_in_between_day_offers_both(monkeypatch):
    monkeypatch.setattr(weather_service.venues_module, "nearby_venues", _venues())
    FakeWeatherClient.days = [forecast_day(TODAY, daytime_precipitation_mm=0.8, rain_chance=35,
                                           max_temperature=16)]
    assert weather_service.activities("Drunen")["lead_with"] == "both"


def test_the_radius_is_clamped(monkeypatch):
    seen = {}

    def fake(lat, lon, radius_km=25, limit=8, query_text=None, categories=None):
        seen["radius"] = radius_km
        return ([], [])

    monkeypatch.setattr(weather_service.venues_module, "nearby_venues", fake)
    weather_service.activities("Drunen", radius_km=500)
    assert seen["radius"] == 50


def test_a_sleeping_database_still_points_at_the_weather_tools(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("endpoint has been disabled")

    monkeypatch.setattr(weather_service.venues_module, "nearby_venues", boom)
    with pytest.raises(ServiceUnavailable) as excinfo:
        weather_service.activities("Drunen")
    assert "get_outdoor_advice" in str(excinfo.value)


def test_the_database_error_itself_stays_in_the_log(monkeypatch):
    """
    psycopg2 names the host and the role it failed to authenticate as. That is
    an operator's business, not a chat answer's.
    """
    def boom(*args, **kwargs):
        raise RuntimeError("connection to server at lakebase-9f2.cloud.databricks.com "
                           "port 5432 failed: FATAL: role 'weerapp' does not exist")

    monkeypatch.setattr(weather_service.venues_module, "nearby_venues", boom)
    with pytest.raises(ServiceUnavailable) as excinfo:
        weather_service.activities("Drunen")

    message = str(excinfo.value)
    assert "lakebase-9f2" not in message
    assert "weerapp" not in message


# --- weekdays and weekends ----------------------------------------------------

def week(n=5):
    return [forecast_day(TODAY + timedelta(days=d)) for d in range(n)]


def test_a_weekday_resolves_here_and_not_in_the_agents_head():
    """
    The failure this prevents is silent: an agent counting days itself asks for
    Friday, gets a perfectly valid forecast, and calls it Saturday.
    """
    days = week(7)
    saturday = next(TODAY + timedelta(days=n) for n in range(7)
                    if (TODAY + timedelta(days=n)).weekday() == 5)
    assert weather_service.resolve_day(days, "zaterdag")["date"] == saturday.isoformat()


def test_todays_own_weekday_means_today():
    """Asked on a Saturday about "zaterdag", people mean today — not a week on."""
    name = weather_service.DUTCH_DAYS[TODAY.weekday()]
    assert weather_service.resolve_day(week(7), name)["date"] == TODAY.isoformat()


@pytest.mark.parametrize("word", ["vanmorgen", "vanochtend", "vanmiddag",
                                  "vanavond", "vannacht"])
def test_the_today_words_beat_the_bare_morgen_inside_them(word):
    """"vanmorgen" contains "morgen"; matching that first answers a day late."""
    assert weather_service.resolve_day(week(), word)["date"] == TODAY.isoformat()


def test_weekend_picks_the_first_weekend_day_the_forecast_covers():
    days = week(7)
    expected = next(d["date"] for d in days
                    if date.fromisoformat(d["date"]).weekday() in (5, 6))
    assert weather_service.resolve_day(days, "dit weekend")["date"] == expected


def test_a_weekend_outside_the_horizon_says_so_rather_than_guessing():
    monday = TODAY - timedelta(days=TODAY.weekday())      # a Monday, whatever today is
    days = [forecast_day(monday + timedelta(days=n)) for n in range(3)]   # ma-wo
    with pytest.raises(NoForecastForDay) as excinfo:
        weather_service.resolve_day(days, "weekend")
    assert "weekenddag" in str(excinfo.value)


def test_a_word_it_does_not_know_lists_what_it_accepts():
    with pytest.raises(NoForecastForDay) as excinfo:
        weather_service.resolve_day(week(), "sint-juttemis")
    assert "weekdag" in str(excinfo.value)


# --- ambiguous place names ----------------------------------------------------

def test_an_ambiguous_name_carries_its_candidates(monkeypatch):
    """
    A flag alone lets the agent say afterwards which Bergen it took; the list
    lets it ask which one was meant.
    """
    monkeypatch.setattr(weather_service, "geocode_place", lambda name: BERGEN)
    FakeWeatherClient.stations = [station("Meetstation Berkhout", 52.64, 4.98)]

    out = weather_service.current_conditions("Bergen")
    assert out["ambiguous_name"] is True
    assert [a["province"] for a in out["alternatives"]] == ["Noord-Holland", "Limburg"]


def test_an_unambiguous_name_carries_no_alternatives_at_all(monkeypatch):
    """Presence of the key is the signal; an empty list would still read as one."""
    FakeWeatherClient.stations = [station("Meetstation Gilze Rijen", 51.57, 4.93)]
    assert "alternatives" not in weather_service.current_conditions("Drunen")


def test_every_tool_reports_the_ambiguity_not_just_the_weather_one(monkeypatch):
    monkeypatch.setattr(weather_service, "geocode_place", lambda name: BERGEN)
    monkeypatch.setattr(weather_service.venues_module, "nearby_venues",
                        lambda *a, **k: ([], []))
    FakeWeatherClient.hours = []

    for out in (weather_service.forecast("Bergen"),
                weather_service.outdoor_advice("Bergen"),
                weather_service.best_day("Bergen"),
                weather_service.rain_timing("Bergen"),
                weather_service.activities("Bergen")):
        assert out["ambiguous_name"] is True
        assert len(out["alternatives"]) == 2


# --- best day: when the two judgements disagree -------------------------------

def test_a_winner_that_is_still_an_indoor_day_says_so():
    """
    Otherwise the agent gets "zaterdag is de beste dag" and "zaterdag kun je
    beter binnen blijven" in the same response and has to reconcile them live.
    """
    FakeWeatherClient.days = [
        forecast_day(TODAY, daytime_precipitation_mm=12.0, rain_chance=95),
        forecast_day(TOMORROW, daytime_precipitation_mm=4.0, rain_chance=80),
    ]
    out = weather_service.best_day("Drunen")

    assert out["best"]["date"] == TOMORROW.isoformat()      # still ranked
    assert out["best_is_outdoor_worthy"] is False
    assert "binnen te blijven" in out["note"]


def test_a_genuinely_good_winner_carries_no_warning():
    FakeWeatherClient.days = [forecast_day(TODAY, daytime_precipitation_mm=0.0,
                                           rain_chance=5, max_temperature=22)]
    out = weather_service.best_day("Drunen")
    assert out["best_is_outdoor_worthy"] is True
    assert out["note"] is None


# --- opening hours ------------------------------------------------------------

def _venues_with_hours(hours):
    def fake(lat, lon, radius_km=25, limit=8, query_text=None, categories=None):
        museum = {"name": "Geniemuseum", "shelter": "binnen", "distance_km": 9.2,
                  "openingstijden": hours}
        castle = {"name": "Kasteel d'Oultremont", "shelter": "gemengd",
                  "distance_km": 2.7, "openingstijden": None}
        return ([museum, castle], [castle])
    return fake


def test_a_venue_closed_on_the_asked_day_is_flagged(monkeypatch):
    """
    The failure this exists for: asked what to do on a Saturday, an agent
    recommended a museum whose own hours in the same response said Tu-Th.
    """
    monkeypatch.setattr(weather_service.venues_module, "nearby_venues",
                        _venues_with_hours("Tu-Th 10:00-16:00"))
    saturday = next(TODAY + timedelta(days=n) for n in range(7)
                    if (TODAY + timedelta(days=n)).weekday() == 5)
    FakeWeatherClient.days = [forecast_day(TODAY + timedelta(days=n)) for n in range(8)]

    out = weather_service.activities("Drunen", day=saturday.isoformat())
    museum = next(v for v in out["indoor"] if v["name"] == "Geniemuseum")
    assert museum["waarschijnlijk_open"] is False
    assert out["weekday"] == "zaterdag"


def test_unknown_hours_stay_unknown(monkeypatch):
    """Most venues have none, and "onbekend" is a useful thing to be told."""
    monkeypatch.setattr(weather_service.venues_module, "nearby_venues",
                        _venues_with_hours(None))
    out = weather_service.activities("Drunen")
    assert all(v["waarschijnlijk_open"] is None for v in out["indoor"])


def test_a_mixed_venue_is_annotated_once_and_shows_in_both_lists(monkeypatch):
    monkeypatch.setattr(weather_service.venues_module, "nearby_venues",
                        _venues_with_hours("24/7"))
    out = weather_service.activities("Drunen")
    castle_in = next(v for v in out["indoor"] if v["name"].startswith("Kasteel"))
    castle_out = next(v for v in out["outdoor"] if v["name"].startswith("Kasteel"))
    assert castle_in is castle_out                      # the same dict, annotated once
    assert "waarschijnlijk_open" in castle_in


def test_the_caveat_says_what_null_means(monkeypatch):
    monkeypatch.setattr(weather_service.venues_module, "nearby_venues",
                        _venues_with_hours(None))
    assert any("Feestdagen" in c for c in weather_service.activities("Drunen")["caveats"])
