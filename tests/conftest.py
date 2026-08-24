"""
Shared test setup.

Puts mcp_server/ on the path so the tests import the modules exactly as the
deployed app does (flat imports — a Databricks App runs from its own folder,
there is no package to import through).

Nothing here touches the network or Lakebase, and nothing should: every test in
this suite either exercises pure logic or stubs the boundary. That is the point
of keeping the HTTP calls in weather_client.py/geocode.py and the SQL in
venues.py — the interesting behaviour is testable without either.
"""

import os
import sys

# venues.poi_embedding_model() checks this before running its vector-dimension
# diagnostic, which would need a live database.
os.environ.setdefault("SKIP_STARTUP_INIT", "1")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "mcp_server"))
