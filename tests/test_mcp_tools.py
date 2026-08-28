"""
Tests for the MCP surface itself: what the agent sees, and what it gets when
something goes wrong.

Two things are worth guarding here. First, that all six tools register with
their schemas and docstrings — the Databricks MCP gateway introspects exactly
this, and a tool with no description is a tool the agent will call wrongly.
Second, that no failure escapes as an exception: a raised error reaches the
agent as a transport-level failure with no usable text, and its only remaining
options are to retry or to invent the weather.
"""

import asyncio

import pytest

import weather_mcp_server as server
from weather_service import LocationUnknown, NoForecastForDay, ServiceUnavailable

TOOLS = {
    "get_current_weather": lambda: server.get_current_weather("Drunen"),
    "get_forecast": lambda: server.get_forecast("Drunen"),
    "get_outdoor_advice": lambda: server.get_outdoor_advice("Drunen", "morgen"),
    "get_best_day": lambda: server.get_best_day("Drunen"),
    "get_rain_timing": lambda: server.get_rain_timing("Drunen"),
    "find_activities": lambda: server.find_activities("Drunen", "iets met dieren"),
}

SERVICE_FUNCTION = {
    "get_current_weather": "current_conditions",
    "get_forecast": "forecast",
    "get_outdoor_advice": "outdoor_advice",
    "get_best_day": "best_day",
    "get_rain_timing": "rain_timing",
    "find_activities": "activities",
}


@pytest.fixture
def registered():
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


# --- the surface the gateway introspects -------------------------------------

def test_all_six_tools_are_registered(registered):
    assert set(registered) == set(TOOLS)


def test_every_tool_has_a_description(registered):
    """Agent Bricks picks tools by their description; an empty one is unusable."""
    for name, tool in registered.items():
        assert tool.description and len(tool.description) > 100, name


def test_every_argument_carries_its_own_description(registered):
    """FastMCP moves each `Args:` entry into the parameter schema; none may be blank."""
    for name, tool in registered.items():
        for argument, schema in tool.parameters["properties"].items():
            assert schema.get("description"), f"{name}.{argument}"


def test_the_usage_guidance_survives_into_what_the_agent_sees(registered):
    """
    FastMCP drops the `Returns:` section when it builds a tool's description, so
    anything the agent needs in order to *use* a result has to sit in the prose
    above `Args:`. This test is the guard on that: writing the guidance under
    `Returns:` would leave the agent with a one-line summary and no instructions,
    and nothing else would fail.
    """
    assert "Returns:" not in registered["get_forecast"].description
    # The two figures it must not confuse, and the date it must not invent.
    assert "daytime_precipitation_mm" in registered["get_forecast"].description
    assert "today" in registered["get_forecast"].description
    # The scope refusal, and the three-valued verdict.
    assert "location_unknown" in registered["get_current_weather"].description
    assert "gemengd" in registered["get_outdoor_advice"].description


def test_location_is_required_everywhere_and_the_rest_is_optional(registered):
    for name, tool in registered.items():
        assert tool.parameters["required"] == ["location"], name


# --- failures come back as data, never as exceptions -------------------------

@pytest.mark.parametrize("tool_name", sorted(TOOLS))
@pytest.mark.parametrize("exception,expected_reason", [
    (LocationUnknown("geen Nederlandse plaats"), "location_unknown"),
    (ServiceUnavailable("Buienradar antwoordde niet"), "service_unavailable"),
    (NoForecastForDay("valt buiten de verwachting"), "date_out_of_range"),
    (RuntimeError("iets onverwachts"), "internal_error"),
])
def test_each_failure_becomes_its_own_reason_code(monkeypatch, tool_name,
                                                  exception, expected_reason):
    def boom(*args, **kwargs):
        raise exception

    monkeypatch.setattr(server.weather_service, SERVICE_FUNCTION[tool_name], boom)
    result = TOOLS[tool_name]()

    assert result["reason"] == expected_reason
    assert result["error"]


def test_an_unexpected_error_does_not_reach_the_agent_at_all(monkeypatch):
    """
    An unexpected exception's text is whatever some library put in it — psycopg2
    names the database host and role, and everything the agent is told can end
    up in its answer to a user. It goes to the log; the agent gets a sentence.
    """
    secret_ish = ("connection to server at db-1234.cloud.databricks.com failed: "
                  "FATAL: password authentication failed for user 'weerapp'")

    def boom(*args, **kwargs):
        raise RuntimeError(secret_ish)

    monkeypatch.setattr(server.weather_service, "current_conditions", boom)
    result = server.get_current_weather("Drunen")

    assert result["reason"] == "internal_error"
    assert "Traceback" not in result["error"]
    assert "databricks.com" not in result["error"]
    assert "weerapp" not in result["error"]


def test_the_three_named_failures_do_keep_their_message(monkeypatch):
    """Those messages are written for a person to read — that is the point of them."""
    def boom(*args, **kwargs):
        raise LocationUnknown("'Chicago' is niet gevonden als Nederlandse plaats")

    monkeypatch.setattr(server.weather_service, "current_conditions", boom)
    assert "Chicago" in server.get_current_weather("Chicago")["error"]


def test_a_successful_call_carries_no_error_key(monkeypatch):
    monkeypatch.setattr(server.weather_service, "current_conditions",
                        lambda location: {"location": location, "temperature_c": 21.4})
    assert "error" not in server.get_current_weather("Drunen")


def test_the_tools_pass_their_arguments_through_unchanged(monkeypatch):
    seen = {}

    def capture(location, query=None, radius_km=25, day=None, limit=8):
        seen.update(location=location, query=query, radius_km=radius_km,
                    day=day, limit=limit)
        return {}

    monkeypatch.setattr(server.weather_service, "activities", capture)
    server.find_activities("Drunen", "iets met dieren", radius_km=30,
                           day="morgen", limit=5)

    assert seen == {"location": "Drunen", "query": "iets met dieren",
                    "radius_km": 30, "day": "morgen", "limit": 5}


# --- startup warm-up ----------------------------------------------------------

def test_the_warm_up_swallows_its_own_failure(monkeypatch, caplog):
    """
    It runs on a daemon thread nobody waits for. Anything it raises would vanish
    into a dead thread, and the lazy path in venues.py still works, so the only
    correct behaviour is to log and carry on.
    """
    import venues

    def boom():
        raise RuntimeError("no network for the model download")

    monkeypatch.setattr(venues, "poi_embedding_model", boom)
    server.warm_embedding_model()          # must not raise

    assert "find_activities" in caplog.text


def test_the_warm_up_reports_how_long_it_took(monkeypatch, caplog):
    """
    The 23-second first call was found by a human waiting for it. A line in the
    log is where that belongs.
    """
    import logging

    import venues

    monkeypatch.setattr(venues, "poi_embedding_model", lambda: object())
    with caplog.at_level(logging.INFO):
        server.warm_embedding_model()

    assert "ready in" in caplog.text
