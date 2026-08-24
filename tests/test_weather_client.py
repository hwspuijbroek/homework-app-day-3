"""
Tests for weather_client.py normalization logic (pure, no network calls).

These specifically guard against the two bugs the id/schema fix was meant to
solve: (1) a station's id must be stable across syncs so /weather/sync
upserts instead of accumulating duplicate rows, and (2) the national 5-day
forecast (the only source of "morgen"/"weekend" answers) must parse into a
sane document.
"""

from datetime import datetime, timezone

from weather_client import WeatherClient


def make_station(stationname="Meetstation Volkel", timestamp="2026-08-14T12:00:00", **overrides):
    station = {
        "stationname": stationname,
        "timestamp": timestamp,
        "weatherdescription": "Onbewolkt",
        "temperature": 22.0,
        "humidity": 55,
        "windspeed": 3.0,
        "lat": 51.65,
        "lon": 5.7,
    }
    station.update(overrides)
    return station


def make_forecast_day(day="2026-08-15T00:00:00", **overrides):
    day_doc = {
        "day": day,
        "mintemperature": "18",
        "maxtemperature": "28",
        "rainChance": 40,
        "sunChance": 60,
        "windDirection": "w",
        "weatherdescription": "Afwisselend bewolkt met kans op regen",
    }
    day_doc.update(overrides)
    return day_doc


def test_station_id_is_stable_across_different_timestamps():
    """
    This is the core fix for the "240 of 320 documents have no embedding" bug:
    the id must NOT depend on the measurement timestamp, otherwise every sync
    inserts a brand new row instead of updating the existing one.
    """
    client = WeatherClient()
    national_forecast = {"title": "Weerbericht", "text": ""}

    doc_a = client._normalize_station(
        make_station(timestamp="2026-08-14T12:00:00"), national_forecast, "synced-a"
    )
    doc_b = client._normalize_station(
        make_station(timestamp="2026-08-14T18:30:00"), national_forecast, "synced-b"
    )

    assert doc_a["id"] == doc_b["id"]
    # issued_at should still reflect the latest measurement, even though the id doesn't change
    assert doc_a["issued_at"] == "2026-08-14T12:00:00"
    assert doc_b["issued_at"] == "2026-08-14T18:30:00"


def test_station_id_differs_per_station():
    client = WeatherClient()
    national_forecast = {"title": "Weerbericht", "text": ""}

    doc_volkel = client._normalize_station(make_station(stationname="Meetstation Volkel"), national_forecast, "s")
    doc_schiphol = client._normalize_station(make_station(stationname="Meetstation Schiphol"), national_forecast, "s")

    assert doc_volkel["id"] != doc_schiphol["id"]


def test_normalize_station_returns_none_without_stationname_or_timestamp():
    client = WeatherClient()
    assert client._normalize_station({}, {"title": "", "text": ""}, "s") is None
    assert client._normalize_station({"stationname": "X"}, {"title": "", "text": ""}, "s") is None


def test_forecast_day_id_is_stable_and_keyed_by_date():
    client = WeatherClient()

    doc_a = client._normalize_forecast_day(make_forecast_day(day="2026-08-15T00:00:00"), "synced-a")
    doc_b = client._normalize_forecast_day(make_forecast_day(day="2026-08-15T00:00:00", rainChance=90), "synced-b")
    doc_other_day = client._normalize_forecast_day(make_forecast_day(day="2026-08-16T00:00:00"), "synced-a")

    assert doc_a["id"] == doc_b["id"]  # same day -> same id, regardless of content -> real upsert
    assert doc_a["id"] != doc_other_day["id"]  # different day -> different id


def test_forecast_day_document_shape():
    client = WeatherClient()
    doc = client._normalize_forecast_day(make_forecast_day(), "synced-at")

    assert doc["location"] == "Nederland"
    assert doc["source_type"] == "forecast_daily"
    assert "zaterdag" in doc["narrative_text"]  # 2026-08-15 is a Saturday
    assert "18" in doc["narrative_text"] and "28" in doc["narrative_text"]
    assert doc["payload"]  # raw day dict preserved as JSON for provenance


def test_forecast_day_returns_none_without_day_field():
    client = WeatherClient()
    assert client._normalize_forecast_day({"weatherdescription": "x"}, "synced-at") is None


def test_station_narrative_excludes_the_national_outlook():
    """
    Regression: the national forecast used to be appended to every station's
    narrative, so the vector index held ~40 copies of the same paragraph —
    around 85% of all indexed station text — swamping what actually tells one
    station apart from another. It is indexed once, on its own document.
    """
    client = WeatherClient()
    national = {"title": "Weerbericht", "text": "Vanavond en vannacht blijft het lang droog."}

    doc = client._normalize_station(make_station(), national, "synced-at")

    assert "Vanavond en vannacht" not in doc["narrative_text"]
    assert "Landelijke verwachting" not in doc["narrative_text"]
    assert "Meetstation Volkel" in doc["narrative_text"]


def test_national_report_is_its_own_document():
    client = WeatherClient()
    national = {"title": "Zwoele avond", "text": "Vanavond en vannacht blijft het lang droog."}

    doc = client._normalize_national_report(national, "synced-at")

    assert doc["source_type"] == "national_report"
    assert doc["location"] == "Nederland"
    assert doc["headline"] == "Zwoele avond"
    assert "Vanavond en vannacht" in doc["narrative_text"]


def test_national_report_id_is_stable_so_syncs_replace_it():
    client = WeatherClient()

    first = client._normalize_national_report({"title": "A", "text": "tekst een"}, "s1")
    second = client._normalize_national_report({"title": "B", "text": "tekst twee"}, "s2")

    assert first["id"] == second["id"]


def test_national_report_returns_none_without_text():
    client = WeatherClient()
    assert client._normalize_national_report({"title": "Weerbericht", "text": ""}, "s") is None


def test_outlook_text_becomes_its_own_document():
    """
    The two outlook texts are the only documents describing days 2-11. Without
    them "wat is de verwachting voor volgende week?" could only ever retrieve
    today's report.
    """
    client = WeatherClient()
    outlook = {
        "startdate": "2026-08-16T00:00:00",
        "enddate": "2026-08-20T00:00:00",
        "forecast": "Af en toe zon en vanaf dinsdag enkele buien.",
    }

    doc = client._normalize_outlook(outlook, "shortterm", "Vooruitzicht 2-6 dagen", "synced-at")

    assert doc["source_type"] == "outlook"
    assert doc["location"] == "Nederland"
    assert "2026-08-16" in doc["narrative_text"]
    assert "enkele buien" in doc["narrative_text"]
    assert doc["issued_at"] == "2026-08-16T00:00:00"


def test_outlook_ids_are_stable_and_distinct_per_horizon():
    client = WeatherClient()
    base = {"startdate": "2026-08-16T00:00:00", "enddate": "2026-08-20T00:00:00", "forecast": "tekst"}

    short_a = client._normalize_outlook(base, "shortterm", "kort", "s1")
    short_b = client._normalize_outlook(dict(base, forecast="andere tekst"), "shortterm", "kort", "s2")
    long_one = client._normalize_outlook(base, "longterm", "lang", "s1")

    assert short_a["id"] == short_b["id"]      # same horizon -> upsert
    assert short_a["id"] != long_one["id"]     # different horizon -> own row


def test_outlook_returns_none_without_usable_input():
    client = WeatherClient()
    assert client._normalize_outlook(None, "shortterm", "kort", "s") is None
    assert client._normalize_outlook({"forecast": ""}, "shortterm", "kort", "s") is None
