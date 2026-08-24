#!/usr/bin/env python
"""
Call every tool over MCP against a running server, and print what came back.

The test suite stubs Buienradar, Nominatim and Lakebase, which is what makes it
fast and deterministic — and also means it never proves the server *speaks MCP*.
This does: it connects as a real client over streamable HTTP, the same transport
the Databricks MCP gateway uses, and calls the tools against the live sources.

Run it against a locally started server:

    cd mcp_server && python weather_mcp_server.py      # terminal 1
    python tools/smoke.py                              # terminal 2

Or against the deployed Databricks App:

    python tools/smoke.py https://<app-url>/mcp

Deliberately includes the failure cases. A smoke test that only shows the happy
path would have missed both bugs this one found: a five-day horizon that had to
refuse "zaterdag" every Monday, and an ambiguity list that offered Bergen
(Noord-Holland) twice and a neighbourhood in Eindhoven.

find_activities needs the Lakebase secret, so without a Databricks login it
reports 'service_unavailable' — which is itself the degradation path worth
seeing.
"""

import asyncio
import json
import sys

from fastmcp import Client

DEFAULT_URL = "http://127.0.0.1:8000/mcp"

# (tool, arguments, keys worth printing) — chosen so each line shows the one
# thing that call is meant to prove.
CALLS = [
    ("get_current_weather", {"location": "Drunen"},
     ["location", "station", "distance_to_station_km", "temperature_c", "description"]),
    ("get_forecast", {"location": "Utrecht", "days": 3},
     ["location", "today"]),
    ("get_outdoor_advice", {"location": "Drunen", "day": "morgen"},
     ["date", "advice", "reason", "score"]),
    # A weekday and a weekend, resolved server-side: on a Monday these are the
    # calls a five-day horizon used to refuse.
    ("get_outdoor_advice", {"location": "Drunen", "day": "zaterdag"},
     ["date", "advice", "reason"]),
    ("get_outdoor_advice", {"location": "Drunen", "day": "dit weekend"},
     ["date", "advice", "reason"]),
    # "vanmorgen" contains "morgen" and means today; getting this wrong is a
    # whole day out and looks entirely correct.
    ("get_outdoor_advice", {"location": "Drunen", "day": "vanavond"},
     ["date", "advice"]),
    ("get_best_day", {"location": "Drunen"},
     ["today", "best_is_outdoor_worthy", "note"]),
    ("get_rain_timing", {"location": "Utrecht"},
     ["date", "hourly_available", "covered", "rain_expected", "summary"]),
    ("find_activities", {"location": "Drunen", "query": "iets met kinderen"},
     ["date", "advice", "lead_with", "error", "reason"]),
    # --- the failure paths ---
    ("get_current_weather", {"location": "Chicago"},
     ["error", "reason"]),
    ("get_current_weather", {"location": "Bergen"},
     ["location", "ambiguous_name", "alternatives"]),
    ("get_outdoor_advice", {"location": "Drunen", "day": "sint-juttemis"},
     ["error", "reason"]),
    ("get_outdoor_advice", {"location": "Drunen", "day": "2030-01-01"},
     ["error", "reason"]),
]


async def main(url: str) -> int:
    failures = 0
    async with Client(url) as client:
        tools = await client.list_tools()
        print(f"verbonden met {url} — {len(tools)} tools: "
              f"{', '.join(t.name for t in tools)}\n")

        for name, arguments, keys in CALLS:
            label = arguments.get("day") or arguments.get("query") or arguments["location"]
            try:
                result = await client.call_tool(name, arguments)
            except Exception as e:
                # A tool that raises instead of returning an error dict is the
                # one outcome this server is not supposed to produce.
                print(f"✗ {name:20} {label:16} raised {type(e).__name__}: {e}")
                failures += 1
                continue

            data = result.data if hasattr(result, "data") else result
            shown = {k: data[k] for k in keys if isinstance(data, dict) and k in data}
            mark = "!" if isinstance(data, dict) and "error" in data else "✓"
            print(f"{mark} {name:20} {label:16} "
                  f"{json.dumps(shown, ensure_ascii=False, default=str)[:220]}")

    print("\n! = de tool gaf een nette fout terug (bij Chicago en sint-juttemis is "
          "dat het gewenste antwoord)")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL)))
