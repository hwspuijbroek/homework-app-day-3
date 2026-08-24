"""
Venues near a coordinate, read from Lakebase — the "uitjes" half of the answer.

The Day 2 corpus is reused as-is: `poi_documents` / `poi_embeddings` hold ~200
Dutch venues seeded offline by Day 2's ingest_poi.py from Wikidata/Wikipedia and
OpenStreetMap, each classified as indoor, outdoor or mixed. Nothing external is
called on the request path, so an MCP tool call costs one Postgres round-trip.

Two things are deliberately *not* carried over from Day 2's app.py:

  - `llm.rerank_venues`. In Day 2 the app owned the language model, so it reread
    the question and the candidates together to fix the ordering. Here the agent
    *is* that model: it receives the candidates with their similarity scores and
    can weigh them itself. A second LLM call inside a tool would add latency and
    a failure mode for a job the caller is already better placed to do.
  - The Flask response shaping. Tools return plain dicts; phrasing is the
    agent's job.

Requires the Day 2 Lakebase instance and its seeded venue tables. Everything
weather-related in this server works without them — only the activity tools need
a database at all, which is why the connection is opened per call rather than at
import.
"""

import json
import logging
import math
import os

from lakebase import get_connection

logger = logging.getLogger(__name__)

# The venue corpus's embedding model. Deliberately *not* the English model the
# Day 2 assignment mandated for the weather corpus: this one measured four times
# its hit rate on Dutch questions (see Day 2's ingest_poi.py for the numbers).
# Both are 384-dimensional, so the schema is shared and the two must never be
# compared against each other — which is why they live in separate tables.
POI_MODEL_NAME = "intfloat/multilingual-e5-small"

# E5 models are trained with asymmetric prefixes: passages were embedded as
# "passage: …" at ingest, so a question has to be encoded as "query: …" or the
# two land in different regions of the same space.
E5_QUERY_PREFIX = "query: "

_POI_MODEL = None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Great-circle distance between two points on Earth, in kilometres.

    Args:
        lat1, lon1: First point (degrees)
        lat2, lon2: Second point (degrees)

    Returns:
        Distance in kilometers
    """
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def poi_embedding_model():
    """
    The venue corpus's embedding model, loaded on first use.

    Deliberately *not* the module-level `EMBEDDING_MODEL`: the weather corpus
    keeps `all-MiniLM-L6-v2` because the assignment mandates it, while the venue
    corpus uses a multilingual model that measured four times its hit rate on
    Dutch questions (see ingest_poi.py for the numbers). Both are 384-dim, so
    the schema is shared and the two must never be compared against each other —
    which is why they live in separate tables.
    """
    global _POI_MODEL
    if _POI_MODEL is None:
        from sentence_transformers import SentenceTransformer
        logger.info(f"Loading venue embedding model {POI_MODEL_NAME}…")
        _POI_MODEL = SentenceTransformer(POI_MODEL_NAME)
        # Skipped where startup is skipped — the hermetic tests fake the
        # database with a scripted cursor, and a diagnostic query would eat one
        # of its answers and fail a test about something else entirely.
        if not os.environ.get("SKIP_STARTUP_INIT"):
            _warn_if_vectors_do_not_match(_POI_MODEL)
    return _POI_MODEL


def _warn_if_vectors_do_not_match(model):
    """
    Does the loaded model still produce vectors of the shape the table holds?

    `poi_embeddings.model_name` records a name, not a version. A newer
    sentence-transformers or newer weights under the same name would encode
    questions into a different space from the stored passages, and the failure
    is silent: cosine still returns numbers between -1 and 1, the ranking is
    simply meaningless. Dimension is the one part of that we can check cheaply.

    It does not catch a same-dimension weights change — nothing cheap does — so
    the versions are pinned in requirements.txt as well. This is the backstop
    for when somebody raises them without re-embedding.

    Warns rather than raises: a corpus mismatch degrades the activity answers,
    and refusing to start would take the weather down with them.
    """
    try:
        loaded = model.get_sentence_embedding_dimension()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT vector_dims(embedding) AS dims
                    FROM poi_embeddings WHERE model_name = %s LIMIT 1
                """, (POI_MODEL_NAME,))
                row = cur.fetchone()
        if row and row['dims'] != loaded:
            logger.error(
                "Venue vectors are %s-dimensional but %s now produces %s. "
                "Questions and passages are in different spaces; re-run "
                "ingest_poi.py before trusting any activity answer.",
                row['dims'], POI_MODEL_NAME, loaded)
        elif not row:
            logger.warning("No stored vectors for %s yet.", POI_MODEL_NAME)
    except Exception as e:
        logger.warning(f"Could not verify the venue vector dimension: {e}")




def nearby_venues(lat, lon, radius_km=25, limit=8, query_text=None,
                   categories=None):
    """
    Venues near a coordinate, split into indoor and outdoor, nearest first.

    Returns (indoor, outdoor) — two lists, because a castle is grounds *and*
    interior and belongs in both rather than in neither.

    Reads only from Lakebase — the corpus is seeded offline by Day 2's
    `ingest_poi.py`, so nothing external is called on the request path. An area
    that was never seeded simply returns two empty lists, which the calling tool
    reports as "no venues known here" rather than as an error.

    Pass `query_text` to rank by semantic similarity to the question instead of
    by distance; see the comment at the retrieval step for why that split exists.
    """
    # Cheap bounding box first so the distance maths runs over a handful of rows
    # instead of the whole table. 1° latitude ≈ 111 km.
    lat_span = radius_km / 111.0
    lon_span = radius_km / (111.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    box = (lat - lat_span, lat + lat_span, lon - lon_span, lon + lon_span)

    # Geography filters, meaning ranks. Without `query_text` the caller wants
    # "what is near me" and distance is the only sensible order — that is the
    # activity card. With it, the question carries an intent that distance cannot
    # express: "iets met dieren" and "iets cultureels" have the same answer under
    # a distance sort. Retrieval over the venue descriptions can tell them apart,
    # so the vector index earns its place on exactly the queries that need it.
    embedding = None
    if query_text:
        try:
            # The venue corpus is embedded with a multilingual model, not the
            # English one the assignment mandates for the weather corpus. A
            # query encoded by the wrong model would be compared against vectors
            # from another space — silently, and with plausible-looking numbers.
            embedding = poi_embedding_model().encode(
                E5_QUERY_PREFIX + query_text, convert_to_numpy=True,
                normalize_embeddings=True).tolist()
        except Exception as e:
            logger.warning(f"Venue query embedding failed, ranking by distance: {e}")

    with get_connection() as conn:
        with conn.cursor() as cur:
            if embedding is None:
                cur.execute("""
                    SELECT id, location, source_type, headline, payload, lat, lon,
                           NULL::float AS similarity
                    FROM poi_documents
                    WHERE lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s
                """, box)
            else:
                # One row per venue: a document can hold several chunks, and its
                # best-matching chunk is what the venue scores.
                # LEFT JOIN, not JOIN. An inner join made the corpus differ per
                # route: a venue whose embedding had not been written yet was
                # invisible to every question that carried text, while the
                # distance-ranked card showed it happily. Seeding is not atomic
                # — Wikidata fails often enough that a run can leave part of the
                # corpus unembedded for hours — so the two routes must see the
                # same places, with an unscored one simply ranking last.
                cur.execute("""
                    SELECT d.id, d.location, d.source_type, d.headline, d.payload,
                           d.lat, d.lon,
                           MAX(1 - (e.embedding <=> %s::vector)) AS similarity
                    FROM poi_documents d
                    LEFT JOIN poi_embeddings e
                           ON e.document_id = d.id
                          -- Vectors from a different model live in a different
                          -- space: comparing them yields plausible numbers and
                          -- meaningless rankings, which is the hardest kind of
                          -- error to notice. The weather corpus keeps the model
                          -- the assignment mandates and this one does not, so
                          -- the join states which model it means rather than
                          -- trusting that nothing ever re-embedded half a table.
                          AND e.model_name = %s
                    WHERE d.lat BETWEEN %s AND %s AND d.lon BETWEEN %s AND %s
                    GROUP BY d.id, d.location, d.source_type, d.headline,
                             d.payload, d.lat, d.lon
                """, (embedding, POI_MODEL_NAME) + box)
            rows = cur.fetchall()

    venues = []
    for row in rows:
        if row['lat'] is None or row['lon'] is None:
            continue
        distance = haversine_km(lat, lon, row['lat'], row['lon'])
        if distance > radius_km:
            continue

        payload = json.loads(row['payload']) if isinstance(row['payload'], str) else (row['payload'] or {})
        # Both corpora share this table and prefix their source_type differently
        # (`poi_` for Wikidata, `osm_` for OpenStreetMap). Matching on the full
        # value silently filed every OSM venue as 'gemengd', so a soft play
        # centre counted as an outdoor option and a mini-golf course as an
        # indoor one. Match on the suffix, which is the part that means shelter.
        source_type = row['source_type'] or ''
        shelter = ('binnen' if source_type.endswith('_indoor')
                   else 'buiten' if source_type.endswith('_outdoor')
                   else 'gemengd')

        venues.append({
            'name': row['location'],
            'type': payload.get('type_label'),
            'shelter': shelter,
            'is_indoor': shelter == 'binnen',
            'town': payload.get('town'),
            'distance_km': round(distance, 1),
            'lat': row['lat'],
            'lon': row['lon'],
            'url': payload.get('wikipedia_url') or payload.get('website'),
            # OpenStreetMap knows things Wikipedia never has. Where it does, the
            # answer must stop saying it doesn't — and must credit the right
            # source under the right licence.
            'openingstijden': payload.get('opening_hours'),
            'website': payload.get('website'),
            'rolstoeltoegankelijk': payload.get('wheelchair') == 'yes',
            'bron': payload.get('bron') or 'Wikipedia & Wikidata',
            'qid': payload.get('qid'),
            'part_of': payload.get('part_of'),
            'similarity': round(row['similarity'], 3) if row['similarity'] is not None else None,
        })

    if embedding is None:
        venues.sort(key=lambda v: v['distance_km'])
    else:
        # Similarity leads, but distance still counts: a marginally better match
        # 24 km away is a worse suggestion than a good one down the road. The
        # penalty is deliberately small (0.004 per km, so 25 km costs 0.10 of
        # similarity) — enough to break near-ties, not enough to override a
        # genuinely better match.
        # An unscored venue ranks below every scored one rather than being
        # treated as similarity 0 and mixed in — "no answer yet" is not the same
        # claim as "a poor match".
        venues.sort(key=lambda v: (v['similarity'] is None,
                                   -((v['similarity'] or 0) - 0.004 * v['distance_km'])))

    # Narrow to the kind that was asked for *before* the limit. Applying it
    # afterwards threw away the very venues the question wanted: asked for
    # something to do with small children in the rain near Drunen, the top eight
    # by similarity held one kid-friendly venue, so the answer offered exactly
    # one option while a soft play centre sat 6 km away, unranked and unseen.
    if categories:
        of_that_kind = [v for v in venues if v.get('type') in categories]
        if of_that_kind:
            venues = of_that_kind

    # Day 2 reranked here with an LLM call, because vector search is good at
    # finding the right neighbourhood and poor at ordering inside it — measured
    # on this corpus, the top ten sit within 0.014 of one another. That call is
    # gone on purpose: the agent calling this tool is a language model that
    # already has the question, and it receives `similarity` and `distance_km`
    # per venue to order them itself. Doing it here would cost a second model
    # call and a second failure mode for the same judgement.

    # A castle is grounds and interior; a zoo has indoor houses. Those belong in
    # both lists rather than in neither, which is what a binary produced.
    return (
        [v for v in venues if v['shelter'] in ('binnen', 'gemengd')][:limit],
        [v for v in venues if v['shelter'] in ('buiten', 'gemengd')][:limit],
    )


