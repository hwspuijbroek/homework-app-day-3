"""
Tests for the Lakebase venue lookup, with a scripted cursor standing in for the
database — same approach as Day 2's tests/conftest.py.

The rules under test are the ones that were bugs in Day 2 before they were
rules: shelter comes from the *suffix* of source_type (matching the whole value
filed every OpenStreetMap venue as 'gemengd'), the radius is enforced on the
real distance rather than on the bounding box, a venue with no embedding ranks
last instead of scoring zero, and the category filter runs before the limit.
"""

import json
from contextlib import contextmanager

import pytest

import venues


class FakeCursor:
    """Minimal psycopg2-cursor stand-in: returns `rows`, records the SQL it got."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, rows):
        self.cur = FakeCursor(rows)

    def cursor(self):
        return self.cur


@pytest.fixture
def database(monkeypatch):
    """Hands `nearby_venues` a scripted result set instead of Lakebase."""
    state = {}

    def use(rows):
        connection = FakeConnection(rows)
        state["connection"] = connection

        @contextmanager
        def fake_get_connection():
            yield connection

        monkeypatch.setattr(venues, "get_connection", fake_get_connection)
        return connection

    state["use"] = use
    return state


def row(name, lat=51.68, lon=5.13, source_type="poi_indoor", similarity=None,
        type_label="museum", town="Drunen", as_json=False, narrative_text=None):
    payload = {"type_label": type_label, "town": town,
               "wikipedia_url": f"https://nl.wikipedia.org/wiki/{name}",
               "bron": "Wikipedia & Wikidata"}
    return {"id": name.lower(), "location": name, "source_type": source_type,
            "headline": name, "lat": lat, "lon": lon, "similarity": similarity,
            "payload": json.dumps(payload) if as_json else payload,
            "narrative_text": narrative_text}


# --- shelter classification ---------------------------------------------------

@pytest.mark.parametrize("source_type,indoor,outdoor", [
    ("poi_indoor", True, False),
    ("osm_indoor", True, False),
    ("poi_outdoor", False, True),
    ("osm_outdoor", False, True),
    ("poi_gemengd", True, True),
    ("osm_gemengd", True, True),
])
def test_shelter_comes_from_the_suffix_of_source_type(database, source_type, indoor, outdoor):
    """
    Matching the whole value silently filed every OSM venue as 'gemengd', so a
    soft play centre counted as an outdoor option and mini-golf as an indoor one.
    """
    database["use"]([row("Plek", source_type=source_type)])
    inside, outside = venues.nearby_venues(51.68, 5.13)
    assert bool(inside) is indoor
    assert bool(outside) is outdoor


def test_a_mixed_venue_appears_in_both_lists(database):
    """A castle is grounds and interior; a binary put it in neither."""
    database["use"]([row("Kasteel", source_type="poi_gemengd")])
    inside, outside = venues.nearby_venues(51.68, 5.13)
    assert inside[0]["name"] == outside[0]["name"] == "Kasteel"


# --- geography ----------------------------------------------------------------

def test_the_radius_is_enforced_on_the_real_distance(database):
    """
    The SQL filters on a bounding box, which is a square: its corners reach
    ~1,4x the radius. Without the haversine check a venue 34 km away comes back
    from a 25 km search.
    """
    database["use"]([row("Dichtbij", lat=51.68, lon=5.13),
                     row("Hoek van het vierkant", lat=51.90, lon=5.45)])
    inside, _ = venues.nearby_venues(51.68, 5.13, radius_km=25)
    assert [v["name"] for v in inside] == ["Dichtbij"]


def test_a_row_without_coordinates_is_skipped(database):
    database["use"]([row("Nergens", lat=None, lon=None), row("Museum")])
    inside, _ = venues.nearby_venues(51.68, 5.13)
    assert [v["name"] for v in inside] == ["Museum"]


def test_without_a_question_the_nearest_venue_comes_first(database):
    database["use"]([row("Ver", lat=51.80), row("Dichtbij", lat=51.69)])
    inside, _ = venues.nearby_venues(51.68, 5.13)
    assert [v["name"] for v in inside] == ["Dichtbij", "Ver"]
    assert inside[0]["distance_km"] < inside[1]["distance_km"]


def test_a_payload_stored_as_json_text_is_parsed(database):
    database["use"]([row("Museum", as_json=True)])
    inside, _ = venues.nearby_venues(51.68, 5.13)
    assert inside[0]["type"] == "museum"
    assert inside[0]["town"] == "Drunen"


# --- description excerpt -------------------------------------------------------

def test_beschrijving_is_the_first_two_sentences_of_narrative_text(database):
    text = ("Ecomare is een aquarium in Texel. Het combineert opvang van "
            "zeehonden en vogels met een natuurmuseum. Vijftien zeehonden "
            "worden er permanent opgevangen.")
    database["use"]([row("Ecomare", narrative_text=text)])
    inside, _ = venues.nearby_venues(51.68, 5.13)
    assert inside[0]["beschrijving"] == (
        "Ecomare is een aquarium in Texel. Het combineert opvang van "
        "zeehonden en vogels met een natuurmuseum.")


def test_beschrijving_is_none_without_narrative_text(database):
    database["use"]([row("Bowlingcenter", narrative_text=None)])
    inside, _ = venues.nearby_venues(51.68, 5.13)
    assert inside[0]["beschrijving"] is None


@pytest.mark.parametrize("text,expected", [
    (None, None),
    ("", None),
    ("Eén zin zonder punt op het eind", "Eén zin zonder punt op het eind."),
    ("Twee zinnen. Precies twee.", "Twee zinnen. Precies twee."),
    ("Drie zinnen. Wordt afgekapt. Na de tweede.", "Drie zinnen. Wordt afgekapt."),
])
def test_first_sentences(text, expected):
    assert venues._first_sentences(text) == expected


# --- semantic ranking ---------------------------------------------------------

@pytest.fixture
def encoder(monkeypatch):
    """A stand-in for the sentence-transformers model; no weights, no download."""

    class FakeVector(list):
        """The real encoder returns a numpy array; only .tolist() is used on it."""

        def tolist(self):
            return list(self)

    class FakeModel:
        encoded = []

        def encode(self, text, convert_to_numpy=True, normalize_embeddings=True):
            FakeModel.encoded.append(text)
            return FakeVector([0.1, 0.2, 0.3])

    FakeModel.encoded = []
    monkeypatch.setattr(venues, "poi_embedding_model", lambda: FakeModel())
    return FakeModel


def test_a_question_is_encoded_with_the_e5_query_prefix(database, encoder):
    """
    The corpus was embedded as "passage: …". An E5 query without its prefix
    lands in a different region of the same space — plausible numbers, wrong
    neighbours.
    """
    database["use"]([row("Museum", similarity=0.8)])
    venues.nearby_venues(51.68, 5.13, query_text="iets met dieren")
    assert encoder.encoded == ["query: iets met dieren"]


def test_the_query_names_the_model_the_vectors_belong_to(database, encoder):
    """Vectors from another model live in another space; the join must say which."""
    connection = database["use"]([row("Museum", similarity=0.8)])
    venues.nearby_venues(51.68, 5.13, query_text="iets met dieren")
    sql, params = connection.cur.calls[0]
    assert "e.model_name = %s" in sql
    assert venues.POI_MODEL_NAME in params


def test_a_better_match_outranks_a_nearer_one(database, encoder):
    database["use"]([row("Dichtbij, matige match", lat=51.69, similarity=0.60),
                     row("Verder, goede match", lat=51.78, similarity=0.85)])
    inside, _ = venues.nearby_venues(51.68, 5.13, query_text="iets met dieren")
    assert inside[0]["name"] == "Verder, goede match"


def test_distance_breaks_a_near_tie(database, encoder):
    """0,004 per km: enough to order two equally good matches, never enough to override."""
    database["use"]([row("Vlakbij", lat=51.681, similarity=0.80),
                     row("Twintig kilometer verderop", lat=51.86, similarity=0.81)])
    inside, _ = venues.nearby_venues(51.68, 5.13, query_text="iets met dieren")
    assert inside[0]["name"] == "Vlakbij"


def test_an_unscored_venue_ranks_last_rather_than_as_a_poor_match(database, encoder):
    """
    Seeding is not atomic, so a venue can sit in the corpus unembedded for hours.
    "Not scored yet" is not the same claim as "similarity 0".
    """
    database["use"]([row("Nog niet ge-embed", similarity=None),
                     row("Zwakke match", similarity=0.05)])
    inside, _ = venues.nearby_venues(51.68, 5.13, query_text="iets met dieren")
    assert [v["name"] for v in inside] == ["Zwakke match", "Nog niet ge-embed"]
    assert inside[-1]["similarity"] is None


def test_a_failing_encoder_falls_back_to_distance_instead_of_failing(database, monkeypatch):
    def boom():
        raise RuntimeError("model download failed")

    monkeypatch.setattr(venues, "poi_embedding_model", boom)
    database["use"]([row("Ver", lat=51.80), row("Dichtbij", lat=51.69)])
    inside, _ = venues.nearby_venues(51.68, 5.13, query_text="iets met dieren")
    assert [v["name"] for v in inside] == ["Dichtbij", "Ver"]


# --- category narrowing -------------------------------------------------------

def test_the_category_filter_runs_before_the_limit(database, encoder):
    """
    Applying it afterwards threw away the venues the question wanted: the top
    eight by similarity held one kid-friendly venue while a soft play centre sat
    6 km away, unranked and unseen.
    """
    rows = [row(f"Museum {n}", similarity=0.9 - n / 100, type_label="museum") for n in range(8)]
    rows.append(row("Speeltuin", similarity=0.4, type_label="speeltuin"))
    database["use"](rows)

    inside, _ = venues.nearby_venues(51.68, 5.13, limit=3, query_text="iets voor kinderen",
                                     categories={"speeltuin"})
    assert [v["name"] for v in inside] == ["Speeltuin"]


def test_a_category_nothing_matches_is_ignored_rather_than_returning_nothing(database):
    database["use"]([row("Museum", type_label="museum")])
    inside, _ = venues.nearby_venues(51.68, 5.13, categories={"dierentuin"})
    assert [v["name"] for v in inside] == ["Museum"]


def test_the_limit_applies_per_list(database):
    rows = [row(f"Museum {n}", lat=51.68 + n / 1000) for n in range(10)]
    database["use"](rows)
    inside, _ = venues.nearby_venues(51.68, 5.13, limit=4)
    assert len(inside) == 4
