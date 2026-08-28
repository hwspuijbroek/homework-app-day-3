"""
Dutch weather + days-out MCP server.

Exposes six tools over MCP (Model Context Protocol) so a Databricks Agent Bricks
agent can call them like any other tool:
    - get_current_weather(location)
    - get_forecast(location, days)
    - get_outdoor_advice(location, day)      <- the judgement call, not a passthrough
    - get_best_day(location)
    - get_rain_timing(location, day)
    - find_activities(location, query, radius_km, day, limit)

Backed by Buienradar (measurements + per-coordinate forecast), Nominatim (place
names) and the Day 2 Lakebase venue corpus (~200 Dutch venues from
Wikidata/Wikipedia and OpenStreetMap, classified indoor/outdoor/mixed). Scope is
the Netherlands: Buienradar has no data elsewhere, and every tool says so
explicitly rather than inventing a forecast for Chicago.

Structure follows the Day 3 reference project (mcp_server/alpaca_mcp_server.py +
alpaca_broker.py): the tool functions below are thin, and every HTTP call, SQL
query and threshold lives in weather_client.py, geocode.py, venues.py,
verdict.py and weather_service.py.

Why every tool returns {"error": ...} instead of raising: a raised exception
reaches the agent as a transport-level tool failure with no usable text, and the
agent's only options are to retry or to fill the gap itself. A dict says *which*
of the three things went wrong — unknown place, service down, date outside the
horizon — and the system prompt tells the agent what to do with each.

Why the docstrings below are shaped the way they are: FastMCP parses Google-style
docstrings and sends the parts on separately — the summary and the prose that
follows it become the tool's description, each `Args:` entry becomes that
parameter's description in the JSON schema, and the `Returns:` section is
dropped. So anything the agent must know in order to *use* a result correctly
(which figure means what, when to hedge, what the scope is) sits in the prose,
above `Args:`. The `Returns:` blocks are kept for whoever reads the source, and
list the keys rather than repeating the guidance.

Deploy as its own Databricks App (see app.yaml, and the "host your own MCP"
pattern at https://docs.databricks.com/aws/en/agents/mcp-tools/custom-mcp), then
register its URL as an external MCP server.

Run locally:
    python weather_mcp_server.py
"""

import logging
import os
import threading
import time

from fastmcp import FastMCP

import weather_service
from weather_service import (
    DayWordNotUnderstood,
    LocationUnknown,
    NoForecastForDay,
    ServiceUnavailable,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-mcp-server")

mcp = FastMCP("nl-weather-and-days-out")


def _failure(tool: str, exception: Exception) -> dict:
    """
    Turn an exception into the error shape every tool returns.

    `reason` is a stable machine-readable code the system prompt can be written
    against; `error` is the sentence to show the user.

    The named exceptions carry messages written to be read by a person, so they
    are passed through. Anything else is logged with its traceback here and
    reported as a fixed sentence: an unexpected exception's text is whatever some
    library chose to put in it — a connection string, a hostname, a row of data —
    and everything the agent is told can end up in its answer.

    NoForecastForDay and DayWordNotUnderstood get different reason codes on
    purpose: the first means the date is real but outside the 7-day forecast,
    the second means the word itself was never resolved to a date. Collapsing
    them once already produced an agent that told a user asking about
    "morgenochtend" that the forecast only reaches seven days out.
    """
    if isinstance(exception, LocationUnknown):
        return {"error": str(exception), "reason": "location_unknown"}
    if isinstance(exception, ServiceUnavailable):
        return {"error": str(exception), "reason": "service_unavailable"}
    if isinstance(exception, DayWordNotUnderstood):
        return {"error": str(exception), "reason": "day_not_understood"}
    if isinstance(exception, NoForecastForDay):
        return {"error": str(exception), "reason": "date_out_of_range"}

    logger.exception("%s failed unexpectedly", tool)
    return {"error": f"{tool} kon niet worden uitgevoerd door een onverwachte fout; "
                     f"die staat in de log van de server.",
            "reason": "internal_error"}


@mcp.tool
def get_current_weather(location: str) -> dict:
    """
    Get the measured weather right now at the Dutch weather station nearest to a place.

    Netherlands only: this is Buienradar data and there is none elsewhere. A
    foreign or unknown place comes back as an error with reason
    'location_unknown', which means ask the user for a Dutch place — never
    answer it from your own knowledge.

    These are measurements from a station some kilometres away, not conditions
    at the exact address: mention distance_to_station_km when it is large. When
    `ambiguous_name` is true, more than one Dutch place carries this name (there
    are three Bergens) and `alternatives` lists them with their provinces — ask
    which one is meant before answering, rather than picking one and explaining
    afterwards which you took. Every tool here reports ambiguity the same way.

    Args:
        location: A Dutch place name, e.g. "Drunen", "Den Haag", "Bergen".

    Returns:
        location, province, ambiguous_name, alternatives (only when ambiguous),
        station, distance_to_station_km,
        observed_at, temperature_c, feels_like_c, description, humidity_pct,
        wind_speed_ms, wind_force_bft, wind_direction, visibility_m,
        rain_last_hour_mm, source — or error + reason on failure.
    """
    try:
        return weather_service.current_conditions(location)
    except Exception as e:
        return _failure("get_current_weather", e)


@mcp.tool
def get_forecast(location: str, days: int = 7, day: str | None = None) -> dict:
    """
    Get the multi-day forecast for a Dutch place, today first. Netherlands only.

    Asked about a single day ("morgen", "zaterdag", "dit weekend")? Pass it as
    `day`, the same word `get_outdoor_advice` and `get_rain_timing` take — do not
    translate it into a `days` count yourself. `days` counts forward from today,
    so days=1 returns *today* only ("today first"); asked about tomorrow, that
    row is one day short of the one you want, and nothing about the response
    signals the mismatch. `day` resolves the date server-side and returns just
    that row instead.

    Two rain figures come back per day and they answer different questions:
    daytime_precipitation_mm is how much rain is expected during the day, and
    rain_chance_pct is how likely rain is at all. Never quote one as the other —
    an 80% chance of 0,2 mm is a fine afternoon, a 40% chance of 12 mm is not.
    Both cover the daytime blocks only; nobody plans around rain at 03:00.

    The response carries `today` as an ISO date. You rarely need it: the other
    tools take "zaterdag" or "weekend" as their `day` argument and resolve it
    against the Dutch calendar themselves, which is safer than counting days in
    your head.

    Every day is a separate row, and `description`, `sun_chance_pct` and the
    temperatures belong to *that* row only. Quote the description of the date you
    were asked about, verbatim and translated by nobody — not a summary of it,
    and never the neighbouring day's. Measured failure: asked about tomorrow, a
    model reported "vrijwel zonnig", which was today's row ("Vrijwel onbewolkt
    (zonnig/helder)"); tomorrow said "Mix van opklaringen en hoge bewolking" at
    39% sun. The figures were right and the sky was wrong, which is the hardest
    kind of answer to catch.
    A day with is_partial=true is today with only some hours left, so its
    min/max covers less than a full day — say so before comparing it with
    another day.

    Args:
        location: A Dutch place name, e.g. "Utrecht".
        days: How many days to return, 1-7 (default 7), counted from today.
            Buienradar publishes further out than that, but as prose rather
            than as numbers, so this stops where the figures stop. Values
            outside the range are clamped. Ignored when `day` is given.
        day: A single day to resolve instead — an ISO date, a weekday
            ("zaterdag"), "vandaag", "morgen", "overmorgen", or "weekend".
            Returns a one-entry `days[]` for that date and ignores `days`.

    Returns:
        location, province, ambiguous_name, today, days[] (date, description,
        min_temperature_c, max_temperature_c, daytime_precipitation_mm,
        rain_chance_pct, sun_chance_pct, wind_force_bft, uv_index, day_parts,
        is_partial), source — or error + reason on failure.
    """
    try:
        return weather_service.forecast(location, days, day)
    except Exception as e:
        return _failure("get_forecast", e)


@mcp.tool
def get_outdoor_advice(location: str, day: str | None = None) -> dict:
    """
    Judge whether a day at a Dutch place is one to spend indoors or outdoors, and why.

    This is a derived judgement, not a passthrough of the forecast. The rules,
    tuned against a fortnight of real Buienradar output:
      - 3 mm or more of daytime rain -> 'binnen' on amount alone;
      - 1 mm or more together with a 50%+ chance -> 'binnen';
      - no amount known and a 60%+ chance -> 'binnen';
      - below 4 C, above 30 C, or wind force 7+ -> 'binnen';
      - a thunderstorm/fog/hail description vetoes an otherwise fine day to
        'gemengd', because those carry a low daily rain figure;
      - dry (under 0,5 mm) and 10-28 C -> 'buiten';
      - anything else -> 'gemengd', deliberately: the forecast often does not
        support a stronger claim.
    Amount is weighed before probability because an 80% chance of 0,2 mm drizzle
    is a fine afternoon and a 40% chance of 12 mm is not.

    `advice` has three values, not two: 'gemengd' means the forecast does not
    support a stronger claim, so offer both kinds of plan rather than picking
    for the user. Quote `reason` in your answer — it names the number the
    verdict turned on — and repeat the relevant entry from `caveats` instead of
    presenting a whole-day verdict as certainty. A date beyond the 7-day horizon
    comes back as an error with reason 'date_out_of_range'; say that the
    forecast does not reach that far rather than estimating it. A `day` that
    wasn't recognised at all (a past day, a typo, something not in the list
    below) comes back as 'day_not_understood' instead — a different problem,
    and worth telling apart: ask the user to rephrase using one of the
    accepted words rather than talking about the 7-day horizon.

    Args:
        location: A Dutch place name.
        day: A weekday ("zaterdag"), "weekend", "vandaag", "vanavond", "morgen",
            "overmorgen", an ISO date (YYYY-MM-DD), or omitted for today. Pass
            the word the user said — weekdays are resolved here against the
            Dutch calendar, so you do not have to count days yourself. Must fall
            inside the 7-day horizon, which reaches every weekday from any day.

    Returns:
        location, date, advice ('binnen' | 'buiten' | 'gemengd'), reason, score
        (0-100), factors, forecast (the underlying day), caveats, source — or
        error + reason on failure.
    """
    try:
        return weather_service.outdoor_advice(location, day)
    except Exception as e:
        return _failure("get_outdoor_advice", e)


@mcp.tool
def get_best_day(location: str) -> dict:
    """
    Rank the forecast days for a Dutch place from best to worst for going out.

    Scores each day 0-100 using the same signals as get_outdoor_advice,
    expressed as penalties so two reasonable days can still be ordered: daytime
    millimetres cost up to 70 points, rain chance is a modifier when an amount
    is known and dominant when it is not, temperature is scored as a comfort
    band (22 C beats 31 C), wind force 5+ and severe-weather descriptions
    subtract, sun chance adds a small bonus that is never decisive.

    Scores are a ranking device, not a measurement: a 71 and a 68 are the same
    day out. Name the winning date with the reason attached, and repeat `note`
    when it is set.

    Check `best_is_outdoor_worthy` before you recommend anything. When it is
    false, every day in the forecast is one to stay indoors and the winner is
    only the least bad of them — say that plainly ("de beste van een matige
    week") instead of presenting it as a good day out, and offer
    find_activities for indoor suggestions.

    Args:
        location: A Dutch place name.

    Returns:
        location, province, ambiguous_name, today, best (the winning day),
        ranked[] (all days, best first, each with date, score, factors, advice,
        reason, is_partial, forecast), best_is_outdoor_worthy, note, source — or
        error + reason on failure.
    """
    try:
        return weather_service.best_day(location)
    except Exception as e:
        return _failure("get_best_day", e)


@mcp.tool
def get_rain_timing(location: str, day: str | None = None) -> dict:
    """
    Say when it rains on a given day, hour by hour, for a Dutch place.

    Answers "hoe laat gaat het regenen?", which a daily figure cannot: "2% kans"
    over a day and "droog tot 15:00, daarna nat" are the same forecast. An hour
    counts as wet on measured millimetres (> 0,05 mm), not on chance, and times
    are reported as blocks ("13:00-14:00") because the hourly row is the start of
    an hour, not a moment.

    Call this whenever the question is about *when* rather than *whether* —
    "hoe laat", "wanneer", "vanmiddag", "kan ik straks nog weg". Quote the blocks
    as blocks: "13:00–14:00" is the hour rain falls in, not the minute it starts.
    Past hours of today are excluded, so `covered` starts at the current hour and
    an empty answer means the rest of today, not the whole day. When
    hourly_available is false the day is still in the daily forecast — use
    get_forecast and say the hourly detail does not reach that far.

    Args:
        location: A Dutch place name.
        day: A weekday ("zaterdag"), "weekend", "vandaag", "morgen", an ISO date,
            or omitted for today; the word the user said is fine. Hourly rows
            reach roughly 48 hours ahead; beyond that hourly_available is false
            and the daily forecast is the finest answer available.

    Returns:
        location, date, hourly_available, covered, rain_expected, starts,
        wet_hours[] (block, precipitation_mm, chance_pct), total_mm,
        highest_chance_pct (dry days), summary, source — or error + reason on
        failure.
    """
    try:
        return weather_service.rain_timing(location, day)
    except Exception as e:
        return _failure("get_rain_timing", e)


@mcp.tool
def find_activities(location: str, query: str | None = None, radius_km: int = 25,
                    day: str | None = None, limit: int = 8) -> dict:
    """
    Suggest places to go near a Dutch town, split by shelter and matched to the weather.

    Combines the outdoor verdict for the chosen day with the venue corpus
    (Wikidata/Wikipedia + OpenStreetMap, held in Lakebase): `lead_with` says
    which list the weather favours. Venues that are both — a castle with grounds,
    a zoo with indoor houses — appear in both lists rather than in neither.

    `lead_with` says which list the weather favours ('indoor', 'outdoor' or
    'both'); lead with that one and still mention the other.

    Check `waarschijnlijk_open` before you recommend a venue. It reads the
    venue's own opening hours against the date asked about: false means those
    hours exclude that day — do not offer it, or say plainly that it is closed
    then — and null means the hours are unknown, which is most venues and worth
    saying out loud ("openingstijden staan er niet bij, check even de website")
    rather than papering over. Public holidays are not accounted for. `weekday`
    gives the day in Dutch so you can name it. Only name venues
    that appear in the lists — an empty list means nothing is known within the
    radius, not that there is nothing there. Say that, and offer a larger radius,
    rather than filling the gap from memory.

    Read `similarity` as a coarse filter, not as a ranking. The scores cluster
    tightly — measured on this corpus, the top hits for one query sat between
    0,78 and 0,83, and a bowling alley outscored a zoo for "iets met dieren" —
    so the vector search is reliable for finding the right neighbourhood of
    options and unreliable for ordering inside it. Pick from the list on `type`
    and `name` against what was actually asked, rather than reading it out in
    the order it arrives. A `similarity` of null means "not scored yet", not
    "poor match". Credit `bron` per venue: the OpenStreetMap rows are
    ODbL-licensed. If this fails with reason 'service_unavailable' the venue
    database is asleep — the weather tools still work, so answer the weather half
    and say the suggestions are unavailable.

    Args:
        location: A Dutch place name.
        query: What the visitor is after, in Dutch, e.g. "iets met dieren",
            "museum met kinderen". Ranks by meaning (vector search over the
            venue descriptions) instead of by distance; omit it to get the
            nearest venues. Pass the visitor's own words — the ranking is better
            with them than with a keyword you distilled.

            Omit it entirely when they have not said what they want. "wat kunnen
            we doen?" carries no preference, and passing a word like "binnen" or
            "activiteit" is worse than passing nothing: those are not content
            words, so the search matches venue descriptions that happen to
            contain them instead of ranking on what somebody wants to do.
            Shelter is already handled — the two lists and `lead_with` do that —
            so an indoor day is not a reason to put "binnen" in the query.
        radius_km: Search radius, 1-50 (default 25, clamped).
        day: A weekday ("zaterdag"), "weekend", "vandaag", "morgen", an ISO date,
            or omitted for today; the word the user said is fine.
        limit: Max venues per list, 1-20 (default 8, clamped).

    Returns:
        location, date, advice, reason, lead_with, indoor[], outdoor[],
        radius_km, query, caveats, source. Each venue: name, type, shelter,
        town, distance_km, lat, lon, url, openingstijden, website,
        rolstoeltoegankelijk, beschrijving, bron, qid, part_of, similarity — or
        error + reason on failure. `beschrijving` is a one-to-two-sentence
        excerpt (Wikipedia-derived for most venues, a short OpenStreetMap-based
        line otherwise) — real, sourced text, not a summary you write yourself.
        It is None when nothing was seeded for that venue; say so rather than
        inventing one.
    """
    try:
        return weather_service.activities(location, query, radius_km, day, limit)
    except Exception as e:
        return _failure("find_activities", e)


def warm_embedding_model() -> None:
    """
    Load the venue embedding model now, so the first visitor does not wait for it.

    Measured on a cold container: the first find_activities call carrying a
    query took 23 seconds, because that call was also downloading 470 MB of
    model weights and loading torch. Every later call answered instantly. A
    demo, or a first impression, is exactly the call that pays it.

    Runs in a daemon thread, so the server is answering MCP requests while this
    happens and a hung download can never keep the process alive. Failure is
    logged and otherwise ignored: the lazy path in venues.py still works, and if
    the model cannot be loaded at all, the venue search falls back to ranking by
    distance rather than failing.

    Set SKIP_MODEL_WARMUP=1 to skip it — useful locally, where the download is
    not worth it for a run that only touches the weather tools.
    """
    started = time.monotonic()
    try:
        import venues

        venues.poi_embedding_model()
        logger.info("Venue embedding model ready in %.1fs", time.monotonic() - started)
    except Exception:
        # Deliberately broad: this runs on a thread nobody is waiting for, and
        # anything it raises would otherwise vanish into a dead thread.
        logger.warning(
            "Warming the venue embedding model failed after %.1fs; the first "
            "find_activities call will load it instead",
            time.monotonic() - started, exc_info=True)


if __name__ == "__main__":
    if os.getenv("SKIP_MODEL_WARMUP") != "1":
        threading.Thread(target=warm_embedding_model,
                         name="warm-embedding-model", daemon=True).start()

    # Databricks Apps route external HTTP traffic to this port via app.yaml, and
    # streamable-http is the transport the Databricks MCP client/gateway expects
    # (see the "host your own MCP" doc in the module docstring). Locally the
    # dev container sets PORT=8000 so it lands on the same port as the deploy.
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", 8000)))
    mcp.run(transport="http", host="0.0.0.0", port=port)
