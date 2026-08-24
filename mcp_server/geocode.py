"""
Location resolution: a Dutch place name to coordinates, via Nominatim.

The adapter for the second external service this server talks to (Buienradar is
the first, in weather_client.py). No @mcp.tool function calls Nominatim itself —
the assignment is explicit about that, and it is also what makes the
"unresolvable location" path testable without a network.

Changed from Day 2's app.py in exactly one way, and deliberately: Day 2's
`_geocode_city_nl` returned None both for "no such place" and for "Nominatim did
not answer". An agent needs those apart — the first means *ask the user for a
different place*, the second means *say the service is down and do not guess* —
so a failed lookup raises GeocodingUnavailable and only a genuine miss returns
None. The MCP tools map the two onto different error messages.

The `_is_a_dutch_place` guard and its PLACE_TYPES list are carried over
unchanged; the comment on them records a failure that is worth not repeating.

Results are cached for the life of the process. Nominatim is a volunteer service
that asks for at most one request a second, and an agent conversation resolves
the same town four or five times over — "hoe warm is het in Drunen?", "en
morgen?", "wat kunnen we daar doen?". Towns do not move, so there is nothing to
expire; only successes and genuine misses are cached, never a failed call.
"""

import copy
import logging
import re
from difflib import SequenceMatcher
from functools import lru_cache

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Nominatim asks for a real User-Agent identifying the application; a request
# without one can be refused outright.
USER_AGENT = "WeatherMCPServer/1.0 (Databricks workshop homework; weather + activities)"

GEOCODE_TIMEOUT = 5

logger = logging.getLogger(__name__)


class GeocodingUnavailable(Exception):
    """Nominatim could not be reached (network/timeout) — distinct from 'no results'."""


# Nominatim is pinned to the Netherlands, so asking it for a foreign city does
# not return nothing — it returns the nearest Dutch thing with a similar name.
# "Parijs" is a clothes shop in Kampen, "Berlijn" the Berlijnstraat in Den Haag,
# "Londen" a business park in Barendrecht, "Tokio" a block of flats in Eindhoven.
# The app then reported the weather at the nearest station to that address, in
# fluent Dutch, citing a real station and a plausible distance. Nothing about the
# answer looked wrong.
PLACE_TYPES = {'municipality', 'city', 'town', 'village', 'hamlet', 'suburb',
               'borough', 'quarter', 'neighbourhood', 'isolated_dwelling'}

# A narrower list, used only when deciding whether a name is ambiguous and what
# the alternatives are. PLACE_TYPES has to admit neighbourhoods, because people
# do ask for them by name — but offering "Bergen, Centrum, Eindhoven" as a third
# answer to "welke Bergen bedoelt u?" is noise next to the towns in Noord-Holland
# and Limburg. A choice worth presenting is a choice between places somebody
# would name as their town.
SETTLEMENT_TYPES = {'municipality', 'city', 'town', 'village'}

# Nominatim returns a municipality and its main town as separate rows, so an
# undeduplicated list showed Bergen (Noord-Holland) twice out of five entries.
# One per province, and few enough to read out loud in a question.
MAX_ALTERNATIVES = 4


def _is_a_dutch_place(asked, result):
    """
    Is what came back an inhabited place, and is it the one that was asked for?

    Two rules, because one is not enough. The type check rejects the shop, the
    street and the building. It does not reject "Antwerpen", which Nominatim
    answers with the village of Knegsel — a real village, wrong by 60 km — so
    the name has to be recognisable too.
    """
    if result.get('addresstype') not in PLACE_TYPES:
        return False

    def plain(text):
        return re.sub(r'[^a-z]', '', (text or '').lower())

    wanted, found = plain(asked), plain(result.get('name'))
    if not wanted or not found:
        return False
    # Containment covers "Den Haag" vs "'s-Gravenhage (Den Haag)" and the
    # apostrophe in "'s-Hertogenbosch"; the ratio covers ordinary typing slips
    # without letting an unrelated village through.
    return (wanted in found or found in wanted
            or SequenceMatcher(None, wanted, found).ratio() >= 0.75)


def geocode_place(name: str) -> dict | None:
    """
    Geocode a Dutch place name to coordinates via Nominatim.

    Args:
        name: A place name as a person would type it, e.g. "Drunen" or "Den Haag".

    Returns:
        A dict with lat, lon, display_name, province, `ambiguous` and
        `alternatives`, or None when nothing that is actually a Dutch place came
        back. `alternatives` lists the other places carrying the same name, so
        the caller can ask *which* Bergen rather than only admitting afterwards
        which one it picked.

    Raises:
        GeocodingUnavailable: Nominatim did not answer. Tell the user; do not
            fall back to a guess, because a wrong coordinate produces a weather
            answer that looks entirely correct.
    """
    if not name or not name.strip():
        return None

    # A deep copy per caller: the cached entry is shared, and it now holds a
    # nested list — a caller that edited it would be editing what everybody who
    # asks next receives.
    found = _lookup(name.strip().lower())
    return copy.deepcopy(found) if found else None


def clear_cache() -> None:
    """Forget every cached lookup. For tests; nothing in the server calls it."""
    _lookup.cache_clear()


# maxsize is generous because an entry is a handful of floats and strings, and
# the Netherlands only has so many towns. lru_cache stores return values, not
# exceptions, so a GeocodingUnavailable is retried on the next call rather than
# turning one bad minute into a permanently broken place name.
@lru_cache(maxsize=1024)
def _lookup(name: str) -> dict | None:
    # limit=5, not 1: the extra rows cost nothing on a call already being made,
    # and they are the only way to notice that a name is ambiguous. There are
    # three Bergens in the Netherlands, and the app silently picked the one in
    # Noord-Holland without ever saying which — a reader in Bergen (Limburg)
    # would have been given the weather 200 km away, correctly labelled "Bergen".
    try:
        response = requests.get(
            NOMINATIM_URL,
            params={"q": f"{name}, Netherlands", "format": "json", "limit": 5,
                    "countrycodes": "nl", "addressdetails": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=GEOCODE_TIMEOUT,
        )
        response.raise_for_status()
        results = response.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("Geocoding '%s' failed: %s", name, e)
        raise GeocodingUnavailable(str(e)) from e

    if not results:
        return None

    places = [r for r in results if _is_a_dutch_place(name, r)]
    if not places:
        first = results[0]
        logger.info("Geocoding '%s' gave %s (%s) — not a place, declining",
                    name, first.get('display_name', '')[:60], first.get('addresstype'))
        return None

    result = places[0]
    address = result.get('address') or {}
    province = address.get('state') or address.get('county') or ''

    # Ambiguous only when another *equally named* settlement exists in another
    # province, so "Bergen" is flagged while "Zaltbommel" — which also returns a
    # handful of nearby rows — is not.
    same_name = [r for r in places
                 if (r.get('name') or '').lower() == (result.get('name') or '').lower()
                 and r.get('addresstype') in SETTLEMENT_TYPES]

    # One row per province: Nominatim lists the municipality and its main town
    # separately, and "Bergen (Noord-Holland) of Bergen (Noord-Holland)?" is not
    # a question anybody can answer.
    by_province = {}
    for row in same_name:
        address = row.get('address') or {}
        key = address.get('state') or address.get('county') or ''
        by_province.setdefault(key, row)

    ambiguous = len(by_province) > 1

    # The candidates travel with the answer, not just the fact that there were
    # several. A flag alone lets the agent say afterwards which Bergen it took;
    # the list lets it ask which one was meant — and for somebody in Bergen
    # (Limburg) that is the difference between a choice and a correction on an
    # answer that looked entirely right.
    alternatives = [
        {
            'display_name': row.get('display_name', name),
            'province': key,
            'lat': float(row['lat']),
            'lon': float(row['lon']),
        }
        for key, row in list(by_province.items())[:MAX_ALTERNATIVES]
    ] if ambiguous else []

    return {
        'lat': float(result['lat']),
        'lon': float(result['lon']),
        'display_name': result.get('display_name', name),
        'province': province,
        'ambiguous': ambiguous,
        'alternatives': alternatives,
    }
