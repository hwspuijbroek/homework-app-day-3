"""
The layer between the MCP tools and the four things this server reads from.

Tools stay thin (see weather_mcp_server.py); adapters stay dumb (weather_client,
geocode, venues); everything that has to *combine* them lives here — resolving a
place name before a forecast can be fetched, picking the station nearest to it,
turning "morgen" into a date, pairing a day's verdict with the venues that suit
it.

Errors are raised here as three named exceptions rather than returned as strings,
because the tool layer has to turn each into a different message for the agent:

    LocationUnknown       the place could not be resolved — ask the user
    ServiceUnavailable    Buienradar or Nominatim did not answer — say so
    NoForecastForDay      the date asked about is outside the forecast horizon

That distinction is the whole point of the assignment's error-handling
requirement: an agent that cannot tell "I don't know that town" from "the
weather service is down" will guess, and a guessed forecast reads exactly like a
real one.

What these messages deliberately do *not* carry is the underlying exception
text. Whatever an agent is told can end up in its answer to a user, and
psycopg2's OperationalError names the host and the role from the Lakebase
connection URL ("connection to server at ... failed: FATAL: password
authentication failed for user ..."). No password, but not something to publish
through a chat window either. The detail goes to the log, where an operator can
read it; the agent gets a sentence that says which service failed and what to do
instead.
"""

import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import venues as venues_module
from geocode import GeocodingUnavailable, geocode_place
from verdict import day_score, nl_number, outdoor_verdict
from weather_client import WeatherClient

logger = logging.getLogger(__name__)

# Buienradar's own day boundaries, not the container's timezone. A Databricks App
# runs in UTC, so without this "vandaag" flips over at 02:00 Dutch time in summer.
AMSTERDAM = ZoneInfo("Europe/Amsterdam")

# The per-coordinate endpoint actually carries 14 days. Day 2 asked it for 5,
# which was right for a page showing a row of day cards and wrong here: asked on
# a Monday about "zaterdag", a 5-day horizon has to refuse a question the source
# can answer — which would make the weekday support above pointless three days a
# week. Seven covers every weekday from every weekday, and both weekend days.
#
# Not fourteen. Buienradar publishes days 7-11 as a prose outlook rather than as
# numbers, which is its own statement about what the daily figures are worth that
# far out — and a verdict of 'binnen' built on a twelve-day-old guess reads
# exactly as confidently as one built on tomorrow's forecast.
FORECAST_HORIZON_DAYS = 7

DUTCH_DAYS = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag",
              "zaterdag", "zondag")

# All of these mean today. Checked before the bare "morgen", because "vanmorgen"
# and "goedemorgen" both contain it and both mean this morning, not tomorrow.
TODAY_WORDS = ("vandaag", "vanochtend", "vanmorgen", "vanmiddag", "vanavond",
               "vannacht")

# Both caches are process-local and short. Buienradar updates roughly every ten
# minutes, and one agent conversation easily produces four tool calls about the
# same town — the forecast key is rounded to ~1 km so those share a fetch.
_FORECAST_TTL_SECONDS = 300
_STATIONS_TTL_SECONDS = 300
_forecast_cache: dict[tuple[float, float], tuple[float, dict]] = {}
_stations_cache: tuple[float, list[dict]] | None = None


class LocationUnknown(Exception):
    """The place name did not resolve to a Dutch place."""


class ServiceUnavailable(Exception):
    """An upstream source (Buienradar, Nominatim, Lakebase) did not answer."""


class NoForecastForDay(Exception):
    """The requested date is outside the days the forecast covers."""


# --- resolving the two things every tool needs -------------------------------

def resolve_location(name: str) -> dict:
    """
    Place name to coordinates, or a clean failure.

    Raises LocationUnknown for a name that is not a Dutch place (including every
    foreign city — this server covers the Netherlands only) and
    ServiceUnavailable when the geocoder itself is unreachable.
    """
    try:
        place = geocode_place(name)
    except GeocodingUnavailable as e:
        logger.warning("Geocoding '%s' failed: %s", name, e)
        raise ServiceUnavailable(
            "de plaatsnaam-service (Nominatim) is nu niet bereikbaar, dus ik kan "
            "deze plaats niet opzoeken"
        ) from e

    if not place:
        raise LocationUnknown(
            f"'{name}' is niet gevonden als Nederlandse plaats. Deze server dekt "
            f"alleen Nederland (bron: Buienradar)."
        )
    return place


def resolve_day(days: list[dict], wanted: str | None) -> dict:
    """
    Pick one day out of a forecast.

    `wanted` accepts an ISO date ("2026-08-26"), a weekday ("zaterdag"), a
    relative word ("vandaag", "morgen", "overmorgen", "vanavond"), "weekend", or
    None for today.

    Weekdays are resolved here rather than left to the agent on purpose. The
    alternative — hand the model today's date and let it count — fails silently:
    it asks for Friday when the question was about Saturday, gets a perfectly
    valid forecast back, and presents it as Saturday's. Nothing raises, nothing
    looks wrong, and the answer is a day out. Date arithmetic is the one part of
    this a language model is measurably bad at, so it does not do it.

    Anything outside the horizon raises rather than returning the nearest day.
    """
    if not days:
        raise NoForecastForDay("er is geen verwachting beschikbaar voor deze locatie")

    today = datetime.now(AMSTERDAM).date()
    covered = [d.get("date") for d in days if d.get("date")]

    if wanted is None or not str(wanted).strip():
        target = today
    else:
        target = _target_date(str(wanted).strip().lower(), days, today)

    if target is None:
        raise NoForecastForDay(
            f"er valt geen weekenddag binnen de verwachting; die loopt van "
            f"{covered[0]} tot en met {covered[-1]}")

    for day in days:
        if day.get("date") == target.isoformat():
            return day

    raise NoForecastForDay(
        f"{target.isoformat()} valt buiten de verwachting; die loopt van "
        f"{covered[0]} tot en met {covered[-1]}" if covered else
        f"{target.isoformat()} valt buiten de verwachting"
    )


def _target_date(wanted: str, days: list[dict], today: date) -> date | None:
    """
    A day word to a date. None means "a weekend was asked for and none is left".

    Ported from Day 2's resolve_target_date, including the order of the checks:
    the today-words come before the bare "morgen" because "vanmorgen" contains
    it, and "overmorgen" comes before it for the same reason.
    """
    try:
        return date.fromisoformat(wanted[:10])
    except ValueError:
        pass

    if any(word in wanted for word in TODAY_WORDS):
        return today
    if "overmorgen" in wanted:
        return today + timedelta(days=2)
    if re.search(r"\bmorgen\b", wanted):
        return today + timedelta(days=1)

    for index, name in enumerate(DUTCH_DAYS):
        if name in wanted:
            # Today counts as itself: asked on a Saturday about "zaterdag",
            # people mean today, not a week from now.
            return today + timedelta(days=(index - today.weekday()) % 7)

    if "weekend" in wanted:
        # The first weekend day the forecast actually covers, rather than a
        # computed date that may already have passed. Late on a Saturday the
        # source has dropped today, and "the next Saturday" then lands a week
        # out — past the horizon, for a weekend that has already started.
        return next(
            (date.fromisoformat(d["date"]) for d in days
             if d.get("date") and date.fromisoformat(d["date"]).weekday() in (5, 6)),
            None,
        )

    raise NoForecastForDay(
        f"'{wanted}' is geen dag die ik begrijp; gebruik JJJJ-MM-DD, een weekdag "
        f"('zaterdag'), 'vandaag', 'morgen', 'overmorgen' of 'weekend'")


# --- the two Buienradar reads, cached ----------------------------------------

def local_forecast(lat: float, lon: float) -> dict:
    """The per-coordinate forecast (hours + days), briefly cached per ~1 km."""
    key = (round(lat, 2), round(lon, 2))
    cached = _forecast_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < _FORECAST_TTL_SECONDS:
        return cached[1]

    try:
        data = WeatherClient().fetch_local_forecast(lat, lon, hours=24, days=FORECAST_HORIZON_DAYS)
    except requests.RequestException as e:
        logger.warning("Local forecast for (%s, %s) failed: %s", lat, lon, e)
        raise ServiceUnavailable("Buienradar is nu niet bereikbaar voor de verwachting") from e

    _forecast_cache[key] = (now, data)
    return data


def stations() -> list[dict]:
    """
    Every Dutch measuring station with current conditions, from the live feed.

    Day 2 read these from Lakebase, where /weather/sync had put them. Here they
    come straight from the documented Buienradar feed instead: it is the same
    data, one HTTP call, and it keeps the weather tools working even when the
    database is asleep — which matters because an agent that gets a database
    error for "hoe warm is het in Utrecht?" has no way to know the weather was
    never in the database's gift.
    """
    global _stations_cache
    now = time.monotonic()
    if _stations_cache and now - _stations_cache[0] < _STATIONS_TTL_SECONDS:
        return _stations_cache[1]

    try:
        documents = WeatherClient().fetch_weather()
    except requests.RequestException as e:
        logger.warning("Station feed failed: %s", e)
        raise ServiceUnavailable("Buienradar is nu niet bereikbaar voor de metingen") from e

    out = []
    for document in documents:
        if document.get("source_type") != "forecast":
            continue
        payload = document["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        if payload.get("lat") is None or payload.get("lon") is None:
            continue
        out.append(payload)

    _stations_cache = (now, out)
    return out


def nearest_station(lat: float, lon: float) -> dict:
    """The closest current-conditions station, with its distance attached."""
    candidates = stations()
    if not candidates:
        raise ServiceUnavailable("Buienradar leverde geen meetstations")

    nearest = min(candidates,
                  key=lambda s: venues_module.haversine_km(lat, lon, s["lat"], s["lon"]))
    distance = venues_module.haversine_km(lat, lon, nearest["lat"], nearest["lon"])
    return {**nearest, "distance_km": round(distance, 1)}


# --- what the tools actually return ------------------------------------------

def current_conditions(location: str) -> dict:
    """Measured conditions at the station nearest to `location`."""
    place = resolve_location(location)
    station = nearest_station(place["lat"], place["lon"])

    return {
        # An ambiguous name (three Bergens) travels with its candidates rather
        # than being resolved silently; the system prompt tells the agent to ask.
        **_place_out(place),
        "station": station.get("stationname"),
        "distance_to_station_km": station["distance_km"],
        "observed_at": station.get("timestamp"),
        "temperature_c": station.get("temperature"),
        "feels_like_c": station.get("feeltemperature"),
        "description": station.get("weatherdescription"),
        "humidity_pct": station.get("humidity"),
        "wind_speed_ms": station.get("windspeed"),
        "wind_force_bft": station.get("windforce"),
        "wind_direction": station.get("winddirection"),
        "visibility_m": station.get("visibility"),
        "rain_last_hour_mm": station.get("rainlasthour"),
        "source": "Buienradar (data.buienradar.nl)",
    }


def forecast(location: str, days: int = 5) -> dict:
    """The multi-day outlook for `location`, today first."""
    place = resolve_location(location)
    data = local_forecast(place["lat"], place["lon"])
    wanted = max(1, min(int(days), FORECAST_HORIZON_DAYS))

    return {
        **_place_out(place),
        "today": datetime.now(AMSTERDAM).date().isoformat(),
        "days": [_day_out(day) for day in data.get("days", [])[:wanted]],
        "source": "Buienradar (forecast.buienradar.nl)",
    }


def outdoor_advice(location: str, day: str | None = None) -> dict:
    """
    The judgement call: indoors, outdoors, or neither — and why.

    Thresholds and reasoning live in verdict.py; this only resolves the place and
    the date around them, and reports what the verdict cannot know.
    """
    place = resolve_location(location)
    data = local_forecast(place["lat"], place["lon"])
    chosen = resolve_day(data.get("days", []), day)

    advice, reason = outdoor_verdict(chosen)
    score, factors = day_score(chosen)

    return {
        **_place_out(place),
        "date": chosen.get("date"),
        # 'binnen' | 'buiten' | 'gemengd' — three values, not two, because the
        # forecast frequently does not support a stronger claim than "wisselvallig".
        "advice": advice,
        "reason": reason,
        "score": round(score),
        "factors": factors,
        "forecast": _day_out(chosen),
        # Documented weaknesses of a whole-day verdict, passed to the agent so it
        # can hedge in the same places the logic does.
        "caveats": _caveats(chosen),
        "source": "Buienradar (forecast.buienradar.nl)",
    }


def best_day(location: str) -> dict:
    """Rank the forecast days for going out, best first."""
    place = resolve_location(location)
    data = local_forecast(place["lat"], place["lon"])
    days = data.get("days", [])
    if not days:
        raise NoForecastForDay("er is geen verwachting beschikbaar voor deze locatie")

    ranked = []
    for day in days:
        score, factors = day_score(day)
        advice, reason = outdoor_verdict(day)
        ranked.append({
            "date": day.get("date"),
            "score": round(score),
            "factors": factors,
            "advice": advice,
            "reason": reason,
            "is_partial": day.get("is_partial", False),
            "forecast": _day_out(day),
        })

    ranked.sort(key=lambda d: d["score"], reverse=True)

    # Two judgements of the same thing live in verdict.py: thresholds decide
    # indoors-or-out for one day, penalties put five days in order. Nothing
    # forces them to agree, and when they do not — the winner on points is still
    # an indoor day, because the rest of the week is wetter — the agent would
    # otherwise be handed "zaterdag is de beste dag" and "zaterdag kun je beter
    # binnen blijven" and left to reconcile them in front of the user. So the
    # ranking says it itself: a best-of-a-bad-week is reported as one.
    best_is_outdoor_worthy = ranked[0]["advice"] != "binnen"

    notes = []
    if days[0].get("is_partial"):
        # Today is scored on the hours that are left, so late in the evening it
        # competes on a narrowed range. Say so rather than letting the ranking
        # imply otherwise.
        notes.append("De score van vandaag telt alleen de resterende uren.")
    if not best_is_outdoor_worthy:
        notes.append(f"Let op: ook de best scorende dag is er een om binnen te "
                     f"blijven ({ranked[0]['reason']}). Dit is de beste van een "
                     f"matige reeks, geen aanrader om eropuit te gaan.")

    return {
        **_place_out(place),
        "today": datetime.now(AMSTERDAM).date().isoformat(),
        "best": ranked[0],
        "ranked": ranked,
        "best_is_outdoor_worthy": best_is_outdoor_worthy,
        "note": " ".join(notes) if notes else None,
        "source": "Buienradar (forecast.buienradar.nl)",
    }


def rain_timing(location: str, day: str | None = None) -> dict:
    """
    When it rains on a given day, per hour — the question a daily figure cannot answer.

    "2% kans op regen" over a whole day and "droog tot drie uur, daarna nat" are
    the same forecast; only one of them is an answer to "wanneer moet ik weg?".
    """
    place = resolve_location(location)
    data = local_forecast(place["lat"], place["lon"])
    chosen = resolve_day(data.get("days", []), day)
    target = chosen.get("date")

    now = datetime.now(AMSTERDAM)
    on_day = []
    for hour in data.get("hours") or []:
        try:
            when = datetime.fromisoformat(hour["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if when.date().isoformat() != target:
            continue
        # Buienradar returns local wall-clock time without an offset. Hours that
        # have already passed today are not a forecast.
        if when.date() == now.date() and when.hour < now.hour:
            continue
        on_day.append((when, hour))

    if not on_day:
        return {
            **_place_out(place),
            "date": target,
            "hourly_available": False,
            # The hourly rows only reach ~48 hours ahead; the day is still in the
            # daily forecast, so this is a limit, not a failure.
            "summary": "Voor deze dag zijn er geen uurwaarden; gebruik get_forecast "
                       "voor het dagbeeld.",
            "source": "Buienradar (forecast.buienradar.nl)",
        }

    # An hour counts as wet on measured millimetres, not on chance: 40% chance of
    # 0.0 mm is a dry hour, and quoting the chance as if it were rainfall is the
    # mistake that once showed 14.2 mm as "23%".
    wet = [(w, h) for w, h in on_day if (h.get("precipitation_mm") or 0) > 0.05]
    # The last row is the *start* of an hour, so the covered period runs an hour
    # past it — otherwise "20:00–23:00" quietly omits the hour it does cover.
    span = f"{on_day[0][0]:%H:%M}–{(on_day[-1][0] + timedelta(hours=1)):%H:%M}"

    if not wet:
        highest = max((h.get("precipitation_chance") or 0) for _, h in on_day)
        return {
            **_place_out(place),
            "date": target,
            "hourly_available": True,
            "covered": span,
            "rain_expected": False,
            "highest_chance_pct": highest,
            "wet_hours": [],
            "total_mm": 0.0,
            "summary": (f"Per uur ({span}) staat er geen neerslag voorspeld; "
                        f"de hoogste regenkans in die uren is {highest}%."),
            "source": "Buienradar (forecast.buienradar.nl)",
        }

    def block(when):
        # A range, not a single time. "13:00 0.7 mm" does not say whether that is
        # *at* one o'clock or *during* the hour that follows, and a reader — or a
        # model — fills that gap with a guess.
        return f"{when:%H:%M}–{(when + timedelta(hours=1)):%H:%M}"

    total = round(sum((h.get("precipitation_mm") or 0) for _, h in wet), 1)
    first, first_hour = wet[0]
    wet_hours = [{"block": block(w),
                  "precipitation_mm": h.get("precipitation_mm"),
                  "chance_pct": h.get("precipitation_chance")}
                 for w, h in wet]

    return {
        **_place_out(place),
        "date": target,
        "hourly_available": True,
        "covered": span,
        "rain_expected": True,
        "starts": block(first),
        "wet_hours": wet_hours,
        "total_mm": total,
        "summary": (f"Per uur ({span}) begint de neerslag in het uur {block(first)} "
                    f"({first_hour.get('precipitation_mm')} mm, "
                    f"{first_hour.get('precipitation_chance')}% kans). "
                    f"Samen {nl_number(total)} mm."),
        "source": "Buienradar (forecast.buienradar.nl)",
    }


def activities(location: str, query: str | None = None, radius_km: int = 25,
               day: str | None = None, limit: int = 8) -> dict:
    """
    Weather-aware suggestions for a day out: the verdict decides which list leads.

    This is the Day 2 pipeline end to end — geocode, forecast, verdict, then the
    Lakebase venue corpus — with the phrasing left to the agent.
    """
    place = resolve_location(location)
    data = local_forecast(place["lat"], place["lon"])
    chosen = resolve_day(data.get("days", []), day)
    advice, reason = outdoor_verdict(chosen)

    radius = max(1, min(int(radius_km), 50))
    try:
        indoor, outdoor = venues_module.nearby_venues(
            place["lat"], place["lon"], radius_km=radius,
            limit=max(1, min(int(limit), 20)), query_text=(query or None))
    except Exception as e:
        # Lakebase asleep, or never seeded for this area. The weather half of the
        # answer is still sound, so this degrades rather than fails: the agent is
        # told what it does have and what it does not.
        # The exception text stays in the log: a psycopg2 connection failure
        # names the Lakebase host and role, and this string is on its way to a
        # user via the agent.
        logger.warning("Venue lookup failed for %s: %s", location, e, exc_info=True)
        raise ServiceUnavailable(
            f"de uitjes-database (Lakebase) is nu niet bereikbaar. "
            f"Het weer voor {place['display_name']} is wel op te vragen met get_outdoor_advice."
        ) from e

    return {
        **_place_out(place),
        "date": chosen.get("date"),
        "advice": advice,
        "reason": reason,
        # Which list to lead with, spelled out rather than left to be inferred
        # from `advice` — the agent should not have to know that 'gemengd' means
        # "offer both".
        "lead_with": {"binnen": "indoor", "buiten": "outdoor"}.get(advice, "both"),
        "indoor": indoor,
        "outdoor": outdoor,
        "radius_km": radius,
        "query": query or None,
        "caveats": _caveats(chosen),
        "source": "Buienradar + Wikidata/Wikipedia & OpenStreetMap (via Lakebase)",
    }


# --- shared shaping -----------------------------------------------------------

def _place_out(place: dict) -> dict:
    """
    The location fields every answer opens with.

    `alternatives` is only present when the name was ambiguous, so its presence
    is itself the signal: the agent should ask which one was meant instead of
    explaining afterwards which one it took.
    """
    out = {
        "location": place["display_name"],
        "province": place.get("province", ""),
        "ambiguous_name": place.get("ambiguous", False),
    }
    if place.get("ambiguous") and place.get("alternatives"):
        out["alternatives"] = place["alternatives"]
    return out


def _day_out(day: dict) -> dict:
    """One forecast day, trimmed to what an agent needs to reason and quote."""
    return {
        "date": day.get("date"),
        "description": day.get("description"),
        "min_temperature_c": day.get("min_temperature"),
        "max_temperature_c": day.get("max_temperature"),
        # Both figures, because they answer different questions: the amount says
        # how wet, the chance says how likely. Day 2's ranking bug came from
        # treating one as the other.
        "daytime_precipitation_mm": day.get("daytime_precipitation_mm"),
        "rain_chance_pct": day.get("rain_chance"),
        "sun_chance_pct": day.get("sun_chance"),
        "wind_force_bft": day.get("windforce"),
        "uv_index": day.get("uv_index"),
        "day_parts": day.get("day_parts"),
        "is_partial": day.get("is_partial", False),
    }


def _caveats(day: dict) -> list[str]:
    """
    What this day's verdict cannot know, in the agent's own input.

    Kept as data rather than prose because the agent has to be able to repeat it:
    a confident "prima weer zaterdag" built on a partial day or on a rain chance
    without an amount is exactly the hallucination the assignment's system-prompt
    requirement is aimed at.
    """
    out = []
    if day.get("is_partial"):
        out.append("Dit is een gedeeltelijke dag: alleen de resterende uren tellen mee, "
                   "dus de max-temperatuur is versmald.")
    if day.get("daytime_precipitation_mm") is None and day.get("rain_chance") is not None:
        out.append("Alleen regenkans bekend, geen hoeveelheid: 80% kans op 0,2 mm motregen "
                   "en 40% kans op 12 mm zien er in dit cijfer hetzelfde uit.")
    out.append("Dit is een dagoordeel; voor 'regen tot 11:00, daarna droog' is "
               "get_rain_timing nauwkeuriger.")
    return out
