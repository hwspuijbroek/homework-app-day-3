"""
Tests for the Nominatim adapter — the "clean error for a bad location" half of
the assignment's error-handling requirement.

The interesting cases are all failures, and none of them touch the network:
`responses` stands in for Nominatim so the two ways a lookup can fail stay
distinguishable in tests as well as in production.
"""

import pytest
import requests
import responses

import geocode
from geocode import GeocodingUnavailable, geocode_place


def hit(name="Drunen", addresstype="town", lat="51.68", lon="5.13",
        state="Noord-Brabant", display_name=None):
    return {
        "name": name,
        "addresstype": addresstype,
        "lat": lat,
        "lon": lon,
        "display_name": display_name or f"{name}, {state}, Nederland",
        "address": {"state": state},
    }


def stub(payload, status=200):
    responses.add(responses.GET, geocode.NOMINATIM_URL, json=payload, status=status)


@pytest.fixture(autouse=True)
def empty_cache():
    """
    Lookups are cached for the life of the process, which for a test module is
    the whole run — without this, the first test's "Drunen" answers every later
    one and their stubs are never hit.
    """
    geocode.clear_cache()
    yield
    geocode.clear_cache()


@responses.activate
def test_a_dutch_town_resolves_to_coordinates():
    stub([hit()])
    place = geocode_place("Drunen")
    assert (round(place["lat"], 2), round(place["lon"], 2)) == (51.68, 5.13)
    assert place["province"] == "Noord-Brabant"
    assert place["ambiguous"] is False


@responses.activate
def test_the_request_is_pinned_to_the_netherlands():
    """Scope is enforced at the source, not by hoping the model behaves."""
    stub([hit()])
    geocode_place("Drunen")
    assert "countrycodes=nl" in responses.calls[0].request.url


@responses.activate
def test_nothing_found_is_not_an_error():
    stub([])
    assert geocode_place("Xyzzyburg") is None


@responses.activate
def test_a_foreign_city_is_declined_rather_than_answered_with_a_shop():
    """
    Nominatim is pinned to NL, so "Parijs" does not return nothing — it returns a
    clothes shop in Kampen. Day 2 then reported that shop's nearest station in
    fluent Dutch, and nothing about the answer looked wrong.
    """
    stub([hit(name="Parijs", addresstype="shop", state="Overijssel")])
    assert geocode_place("Parijs") is None


@responses.activate
def test_a_real_village_with_the_wrong_name_is_declined_too():
    """The type check passes for Knegsel; only the name check rejects it."""
    stub([hit(name="Knegsel", addresstype="village")])
    assert geocode_place("Antwerpen") is None


@responses.activate
def test_an_official_name_still_matches_what_a_person_typed():
    stub([hit(name="'s-Gravenhage (Den Haag)", addresstype="city", state="Zuid-Holland")])
    assert geocode_place("Den Haag") is not None


@responses.activate
def test_two_places_with_the_same_name_are_flagged_as_ambiguous():
    """There are three Bergens; picking one silently is how you answer 200 km off."""
    stub([hit(name="Bergen", state="Noord-Holland"),
          hit(name="Bergen", state="Limburg")])
    assert geocode_place("Bergen")["ambiguous"] is True


@responses.activate
def test_the_other_candidates_travel_with_the_answer():
    """
    So the caller can ask *which* Bergen. A bare flag only allows explaining
    afterwards which one was taken, on an answer that looked entirely right.
    """
    stub([hit(name="Bergen", state="Noord-Holland", lat="52.66", lon="4.70"),
          hit(name="Bergen", state="Limburg", lat="51.58", lon="6.05")])

    alternatives = geocode_place("Bergen")["alternatives"]
    assert [a["province"] for a in alternatives] == ["Noord-Holland", "Limburg"]
    assert alternatives[1]["lat"] == 51.58


@responses.activate
def test_an_unambiguous_place_has_an_empty_candidate_list():
    stub([hit()])
    assert geocode_place("Drunen")["alternatives"] == []


@responses.activate
def test_the_candidate_list_cannot_be_mutated_through_the_cache():
    """The nested list is shared too; a shallow copy would leak edits to it."""
    stub([hit(name="Bergen", state="Noord-Holland"),
          hit(name="Bergen", state="Limburg")])
    geocode_place("Bergen")["alternatives"].pop()
    assert len(geocode_place("Bergen")["alternatives"]) == 2


@responses.activate
def test_several_nearby_rows_for_one_place_are_not_ambiguous():
    stub([hit(name="Zaltbommel", state="Gelderland"),
          hit(name="Zaltbommel Centrum", addresstype="suburb", state="Gelderland")])
    assert geocode_place("Zaltbommel")["ambiguous"] is False


@responses.activate
def test_an_unreachable_geocoder_raises_rather_than_returning_none():
    """
    The distinction the agent needs: "no such town" means ask the user, "service
    down" means say so. Collapsing them is how a down service becomes a guess.
    """
    responses.add(responses.GET, geocode.NOMINATIM_URL,
                  body=requests.exceptions.ConnectionError("boom"))
    with pytest.raises(GeocodingUnavailable):
        geocode_place("Drunen")


@responses.activate
def test_a_server_error_is_unavailable_not_unknown():
    stub({"error": "rate limited"}, status=503)
    with pytest.raises(GeocodingUnavailable):
        geocode_place("Drunen")


def test_an_empty_name_never_reaches_the_network():
    assert geocode_place("   ") is None


# --- caching ------------------------------------------------------------------

@responses.activate
def test_the_same_place_is_only_looked_up_once():
    """
    Nominatim is a volunteer service asking for one request a second, and a
    single conversation resolves the same town four or five times.
    """
    stub([hit()])
    geocode_place("Drunen")
    geocode_place("Drunen")
    geocode_place("  DRUNEN ")     # same town, different typing
    assert len(responses.calls) == 1


@responses.activate
def test_a_cached_result_cannot_be_mutated_by_its_caller():
    """The cached dict is shared; handing it out directly shares every edit too."""
    stub([hit()])
    first = geocode_place("Drunen")
    first["province"] = "Zeeland"
    assert geocode_place("Drunen")["province"] == "Noord-Brabant"


@responses.activate
def test_a_failed_lookup_is_not_cached():
    """
    One bad minute must not turn into a permanently unresolvable place name, so
    the failure is retried — and the second attempt here succeeds.
    """
    responses.add(responses.GET, geocode.NOMINATIM_URL,
                  body=requests.exceptions.ConnectionError("boom"))
    responses.add(responses.GET, geocode.NOMINATIM_URL, json=[hit()], status=200)

    with pytest.raises(GeocodingUnavailable):
        geocode_place("Drunen")
    assert geocode_place("Drunen")["province"] == "Noord-Brabant"


@responses.activate
def test_the_same_place_listed_twice_is_one_candidate():
    """
    Nominatim returns a municipality and its main town as separate rows, so
    "Bergen (Noord-Holland) of Bergen (Noord-Holland)?" was a real question the
    agent could have asked.
    """
    stub([hit(name="Bergen", addresstype="municipality", state="Noord-Holland"),
          hit(name="Bergen", addresstype="town", state="Noord-Holland", lat="52.67"),
          hit(name="Bergen", addresstype="village", state="Limburg", lat="51.58")])

    alternatives = geocode_place("Bergen")["alternatives"]
    assert [a["province"] for a in alternatives] == ["Noord-Holland", "Limburg"]


@responses.activate
def test_a_neighbourhood_with_the_same_name_is_not_offered_as_a_choice():
    """
    There is a Bergen in the centre of Eindhoven. It is a real place and a fine
    answer if you ask for it directly, but it is noise beside the two towns.
    """
    stub([hit(name="Bergen", addresstype="town", state="Noord-Holland"),
          hit(name="Bergen", addresstype="village", state="Limburg", lat="51.58"),
          hit(name="Bergen", addresstype="quarter", state="Noord-Brabant", lat="51.43")])

    assert [a["province"] for a in geocode_place("Bergen")["alternatives"]] == [
        "Noord-Holland", "Limburg"]


@responses.activate
def test_the_candidate_list_stays_short_enough_to_read_out_loud():
    stub([hit(name="Zwaag", state=f"Provincie {n}", lat=f"52.{n}") for n in range(6)])
    assert len(geocode_place("Zwaag")["alternatives"]) == geocode.MAX_ALTERNATIVES
