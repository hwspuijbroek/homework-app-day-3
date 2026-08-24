"""
Weather Client for Buienradar API - Enhanced Version.

Inspired by the python-buienradar library but optimized for RAG pipeline:
- Fetches ALL 40 Dutch weather stations (not just nearest)
- Extracts 25+ fields per station
- Proper timezone handling (Europe/Amsterdam)
- Condition code mapping
- Icon URLs
- Rich narrative generation

References:
- https://data.buienradar.nl/2.0/feed/json
- https://github.com/mjj4791/python-buienradar
"""

import hashlib
import json
import html
import re
from datetime import datetime, timezone
from typing import Any

import requests

BUIENRADAR_URL = "https://data.buienradar.nl/2.0/feed/json"

# Per-location forecast: 14 days plus hourly values for one coordinate.
#
# This is Buienradar's own app backend, not part of their published open-data
# offering — it carries no usage terms and could change without notice. It is used
# only for the *live* forecast shown next to a location; nothing from it is stored
# or embedded, so if it disappears the app degrades to the national outlook from
# the documented feed rather than breaking. See README_WEATHER.md.
BUIENRADAR_LOCAL_FORECAST_URL = "https://forecast.buienradar.nl/2.0/forecast"

DEFAULT_TIMEOUT = 30
LOCAL_FORECAST_TIMEOUT = 5

# Condition code mapping (from python-buienradar)
CONDITION_CODES = {
    # Three elements like every other entry. Added as a bare string once, which
    # made `_description_from_code` index into the *text* — code 'r' described the
    # weather as "a" (the third character of "Zwaar") on every day it appeared.
    "r": ["cloudy", "cloudy", "Zwaar bewolkt en droog"],
    'a': ['clear', 'clear', 'Vrijwel onbewolkt (zonnig/helder)'],
    'b': ['cloudy', 'partlycloudy', 'Mix van opklaringen en middelbare of lage bewolking'],
    'j': ['cloudy', 'partlycloudy', 'Mix van opklaringen en hoge bewolking'],
    'o': ['cloudy', 'partlycloudy', 'Half bewolkt'],
    'c': ['cloudy', 'cloudy', 'Zwaar bewolkt'],
    'd': ['fog', 'partlycloudy-fog', 'Afwisselend bewolkt met lokaal mist(banken)'],
    'n': ['fog', 'fog', 'Opklaring en lokaal nevel of mist'],
    'f': ['rainy', 'partlycloudy-light-rain', 'Afwisselend bewolkt met (mogelijk) wat lichte regen'],
    'q': ['rainy', 'rainy', 'Zwaar bewolkt en regen'],
    'w': ['rainy', 'snowy-rainy', 'Zwaar bewolkt met regen en winterse neerslag'],
    'm': ['rainy', 'light-rain', 'Zwaar bewolkt met wat lichte regen'],
    'u': ['snowy', 'partlycloudy-light-snow', 'Afwisselend bewolkt met lichte sneeuwval'],
    'v': ['snowy', 'light-snow', 'Zwaar bewolkt met lichte sneeuwval'],
    't': ['snowy', 'snowy', 'Zware sneeuwval'],
    'g': ['lightning', 'partlycloudy-lightning', 'Opklaringen en kans op enkele pittige (onweers)buien'],
    's': ['lightning', 'lightning', 'Bewolkt en kans op enkele pittige (onweers)buien'],
}


class WeatherClient:
    """Enhanced client for Buienradar API with rich field extraction."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        """
        Initialize the weather client.
        
        Args:
            timeout: Request timeout in seconds.
        """
        self.timeout = timeout

    def fetch_weather(self, locations: list[str] = None) -> list[dict[str, Any]]:
        """
        Fetch weather data for ALL Dutch weather stations from Buienradar.
        
        Args:
            locations: Ignored (all stations are returned)
        
        Returns:
            List of normalized document dictionaries with 25+ fields per station.
        """
        resp = requests.get(BUIENRADAR_URL, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        
        # Extract national forecast
        national_forecast = self._extract_national_forecast(data)

        # Extract and normalize all station measurements
        stations = data.get("actual", {}).get("stationmeasurements", [])

        documents = []
        synced_at = datetime.now(timezone.utc).isoformat()

        for station in stations:
            doc = self._normalize_station(station, national_forecast, synced_at)
            if doc:
                documents.append(doc)

        # Extract the national 5-day forecast (needed to answer "morgen"/"weekend" questions,
        # since stationmeasurements above only contains *current* conditions per station)
        fivedayforecast = data.get("forecast", {}).get("fivedayforecast", [])
        for day in fivedayforecast:
            doc = self._normalize_forecast_day(day, synced_at)
            if doc:
                documents.append(doc)

        # The national forecaster's prose, indexed once rather than duplicated
        # across every station document.
        report = self._normalize_national_report(national_forecast, synced_at)
        if report:
            documents.append(report)

        # The multi-day outlook texts. These are the only documents describing
        # days 2-11, so without them a question like "wat is de verwachting voor
        # volgende week?" could only retrieve today's report.
        forecast = data.get("forecast", {})
        for key, label in (("shortterm", "Vooruitzicht 2-6 dagen"),
                           ("longterm", "Vooruitzicht 7-11 dagen")):
            doc = self._normalize_outlook(forecast.get(key), key, label, synced_at)
            if doc:
                documents.append(doc)

        return documents

    def _normalize_outlook(self, outlook, key, label, synced_at):
        """One of Buienradar's two longer-range outlook texts, as its own document."""
        if not isinstance(outlook, dict):
            return None

        text = self._clean_html(outlook.get("forecast") or "")
        if not text:
            return None

        start = outlook.get("startdate")
        end = outlook.get("enddate")
        period = ""
        if start and end:
            period = (f" (periode {datetime.fromisoformat(start).date().isoformat()} "
                      f"tot {datetime.fromisoformat(end).date().isoformat()})")

        return {
            "id": hashlib.md5(f"nl_outlook_{key}".encode()).hexdigest(),
            "location": "Nederland",
            "source_type": "outlook",
            "headline": label,
            "narrative_text": f"{label}{period}: {text}",
            "issued_at": start or synced_at,
            "payload": json.dumps(outlook),
            "synced_at": synced_at,
        }

    def fetch_local_forecast(self, lat: float, lon: float, hours: int = 24, days: int = 5) -> dict[str, Any]:
        """
        Forecast for one coordinate: the next `hours` hours and `days` days.

        Returns {"location": {...}, "hours": [...], "days": [...]}. Raises on
        transport errors so the caller can fall back to the national outlook.
        """
        resp = requests.get(
            BUIENRADAR_LOCAL_FORECAST_URL,
            params={"lat": round(lat, 4), "lon": round(lon, 4)},
            # Deliberately shorter than self.timeout: this sits on the request path
            # and a hang would hold one of only a few gunicorn threads. Losing the
            # hourly detail is much cheaper than blocking the app.
            timeout=LOCAL_FORECAST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        raw_days = data.get("days") or []

        # Hours are nested per day; flatten so "the next 24 hours" can cross midnight.
        flat_hours = []
        for day in raw_days:
            for hour in day.get("hours") or []:
                flat_hours.append({
                    "time": hour.get("datetime"),
                    "temperature": hour.get("temperature"),
                    "feeltemperature": hour.get("feeltemperature"),
                    "precipitation_mm": hour.get("precipitationmm"),
                    "precipitation_chance": hour.get("precipitation"),
                    "windspeed": hour.get("windspeedms"),
                    "winddirection": hour.get("winddirection"),
                    "humidity": hour.get("humidity"),
                    "icon_url": self._icon_url_from_code(hour.get("iconcode")),
                    # The emoji is decorative and hidden from screen readers, so
                    # without this an assistive user gets the time and the
                    # temperature and never the weather — which is the reason to
                    # read an hourly forecast at all. Empty for an unknown code;
                    # the caller degrades to time + temperature.
                    "description": self._description_from_code(hour.get("iconcode")),
                })
            if len(flat_hours) >= hours:
                break

        out_days = []
        for index, day in enumerate(raw_days[:days]):
            hour_count = len(day.get("hours") or [])
            # Today's min/max covers only the hours that are left, so late in the
            # evening it reads as 20.7-21.2 where the full day was 19.6-24.4. Flag
            # it rather than presenting a narrowed range as the day's forecast.
            partial = index == 0 and 0 < hour_count < 24

            code = (day.get("iconcode") or "").rstrip()

            # The day-level "precipitation" field is not a probability of rain:
            # 2026-08-17 returned 26 alongside 14.2 mm. Buienradar does give a
            # real per-block chance, though, in morning/afternoon/evening/night —
            # and only the daytime blocks matter for going outside. On that same
            # day 9.6 of the 14.2 mm fell at night and the afternoon and evening
            # were bone dry, which is the difference between "wettest day of the
            # week" and "fine from lunchtime".
            daytime = [day.get(part) for part in ("morning", "afternoon", "evening")]
            daytime = [b for b in daytime if isinstance(b, dict)]
            day_chance = max((b.get("precipitation") or 0 for b in daytime), default=None)
            day_mm = sum((b.get("precipitationmm") or 0) for b in daytime) if daytime else None

            out_days.append({
                "date": (day.get("date") or "")[:10],
                "min_temperature": day.get("mintemperature"),
                "max_temperature": day.get("maxtemperature"),
                # Full 24 hours, kept for completeness…
                "precipitation_mm": day.get("precipitationmm"),
                # …and the two figures decisions are actually made on.
                "daytime_precipitation_mm": day_mm,
                "rain_chance": day_chance if day_chance is not None else day.get("precipitation"),
                "day_parts": [
                    {
                        "part": b.get("timetype"),
                        "precipitation_mm": b.get("precipitationmm"),
                        "rain_chance": b.get("precipitation"),
                        "max_temperature": b.get("maxtemperature"),
                    }
                    for b in daytime
                ],
                "sun_chance": day.get("sunshine"),
                "uv_index": day.get("uvindex"),
                "humidity": day.get("humidity"),
                "windforce": day.get("beaufort"),
                "icon_url": self._icon_url_from_code(code),
                # This endpoint ships no text description; derive one from the
                # condition code so the icon still has an accessible label.
                "description": self._description_from_code(code),
                "is_partial": partial,
            })

        return {
            "location": data.get("location") or {},
            "hours": flat_hours[:hours],
            "days": out_days,
        }

    @staticmethod
    def _description_from_code(code) -> str:
        """Dutch condition text for a Buienradar letter code ('' when unknown)."""
        if not code:
            return ""
        # Night icons double the letter (aa, bb); the condition itself is the same.
        base = str(code)[0].lower()
        mapping = CONDITION_CODES.get(base)
        return mapping[2] if mapping else ""

    @staticmethod
    def _icon_url_from_code(code) -> str:
        """
        Rebuild the icon URL from Buienradar's condition letter.

        The forecast endpoint returns a bare code ("f") where the open feed returns
        a full URL; the frontend derives its glyph from the filename, so give it the
        same shape rather than teaching it a second format.
        """
        if not code:
            return ""
        return f"https://cdn.buienradar.nl/resources/images/icons/weather/30x30/{code}.png"

    def _extract_national_forecast(self, data: dict[str, Any]) -> dict[str, str]:
        """Extract the national forecast from Buienradar data."""
        forecast = data.get("forecast", {})
        weatherreport = forecast.get("weatherreport", {})
        
        title = weatherreport.get("title", "Weerbericht")
        text = weatherreport.get("text", "")
        
        # Clean HTML
        text = self._clean_html(text)
        title = self._clean_html(title)
        
        return {"title": title, "text": text}

    def _clean_html(self, text: str) -> str:
        """
        Remove HTML tags and entities from text.

        Tags first, then entities, and entities via `html.unescape` rather than
        a hand-written list. The list held five: nbsp, amp, lt, gt, quot — none
        of them accented, while this is a Dutch forecast. So "12 &agrave; 13
        graden" reached the corpus verbatim, was embedded verbatim, and came out
        of the model verbatim into an answer somebody reads.

        The order matters: unescaping first would turn "&lt;b&gt;" into a real
        tag that the next step then strips, quietly deleting text that was never
        markup to begin with.
        """
        if not text:
            return ""

        text = re.sub(r'<br\s*/?>', ' ', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = html.unescape(text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def _get_condition_category(self, icon_url: str) -> tuple[str, str, str]:
        """
        Extract condition code from icon URL and map to category.
        
        Args:
            icon_url: e.g. "https://www.buienradar.nl/resources/images/icons/weather/30x30/cc.png"
        
        Returns:
            (category, detailed, description_nl) tuple
        """
        if not icon_url:
            return ("unknown", "unknown", "Onbekend")
        
        # Extract code from URL (e.g., "cc" from "cc.png")
        parts = icon_url.split("/")
        if parts:
            filename = parts[-1]  # "cc.png"
            code = filename.split(".")[0]  # "cc"
            
            # Map single-char code
            first_char = code[0] if code else None
            if first_char in CONDITION_CODES:
                category, detailed, desc_nl = CONDITION_CODES[first_char]
                return (category, detailed, desc_nl)
        
        return ("unknown", "unknown", "Onbekend")

    def _get_barometer_forecast(self, pressure: float) -> tuple[int, str, str]:
        """
        Derive barometer forecast from pressure (from python-buienradar).
        
        Returns:
            (fc_code, fc_name_en, fc_name_nl) tuple
        """
        if pressure is None:
            return (0, None, None)
        
        if pressure < 974:
            return (1, "Thunderstorms", "Zware storm")
        elif pressure < 990:
            return (2, "Stormy", "Storm")
        elif pressure < 1002:
            return (3, "Rain", "Regen en wind")
        elif pressure < 1010:
            return (4, "Cloudy", "Bewolkt")
        elif pressure < 1022:
            return (5, "Unstable", "Veranderlijk")
        elif pressure < 1035:
            return (6, "Stable", "Mooi")
        else:
            return (7, "Very dry", "Zeer mooi")

    def _normalize_station(
        self, 
        station: dict[str, Any], 
        national_forecast: dict[str, str],
        synced_at: str
    ) -> dict[str, Any] | None:
        """
        Normalize a single weather station with enhanced field extraction.
        
        Extracts 25+ fields including:
        - Basic: stationname, temperature, humidity, windspeed
        - Advanced: feeltemperature, visibility, windgust, windazimuth, groundtemperature
        - Derived: condition category, barometer forecast
        - Icons: weather icon URLs
        """
        # Essential fields
        stationname = station.get("stationname", "")
        timestamp = station.get("timestamp", "")
        
        if not stationname or not timestamp:
            return None
        
        # Basic weather
        weatherdescription = station.get("weatherdescription", "Onbekend")
        temperature = station.get("temperature")
        humidity = station.get("humidity")
        windspeed = station.get("windspeed")
        
        # Enhanced fields (from python-buienradar inspiration)
        feeltemperature = station.get("feeltemperature")
        visibility = station.get("visibility")
        windgust = station.get("windgusts")
        windazimuth = station.get("winddirectiondegrees")
        groundtemperature = station.get("groundtemperature")
        irradiance = station.get("sunpower")
        pressure = station.get("airpressure")
        precipitation = station.get("precipitation")
        rainlast24hour = station.get("rainFallLast24Hour")
        rainlasthour = station.get("rainFallLastHour")
        winddirection = station.get("winddirection", "")
        windforce = station.get("windspeedBft")
        
        # Icon and condition
        icon_url = station.get("iconurl", "")
        condition_category, condition_detailed, condition_nl = self._get_condition_category(icon_url)
        
        # Barometer forecast (if pressure available)
        barometer_fc, barometer_name, barometer_name_nl = self._get_barometer_forecast(pressure)
        
        # Build rich narrative
        narrative_parts = []
        
        # Local conditions
        local_parts = [f"Actueel weer in {stationname}: {weatherdescription}"]
        
        if temperature is not None:
            local_parts.append(f"{temperature} graden")
        if feeltemperature is not None and feeltemperature != temperature:
            local_parts.append(f"(voelt als {feeltemperature}°)")
        if windspeed is not None:
            local_parts.append(f"windsnelheid {windspeed} m/s")
        if windgust is not None:
            local_parts.append(f"windstoten {windgust} m/s")
        if humidity is not None:
            local_parts.append(f"luchtvochtigheid {humidity}%")
        if visibility is not None:
            local_parts.append(f"zicht {visibility}m")
        
        local_text = ", ".join(local_parts) + "."
        narrative_parts.append(local_text)
        
        # The barometer "forecast" is deliberately NOT part of the narrative.
        # It maps *absolute* pressure to an outlook, but it is the pressure
        # *tendency* that forecasts anything — so it cheerfully labelled a station
        # reporting "Zwaar bewolkt en regen" at 1016 hPa as "Veranderlijk", and
        # would have put "Barometer voorspelling: Mooi." into the vector index for
        # a retrieval on "wordt het mooi weer?". The derived value is still kept
        # in the payload for reference; it just isn't presented as a forecast.

        # The national outlook is deliberately NOT appended here. It used to be,
        # which meant the vector index held 40 near-identical copies of the same
        # paragraph — roughly 85% of the indexed station text — drowning out what
        # actually distinguishes one station from another. It is now indexed once,
        # as its own document (see _normalize_national_report).
        narrative_text = " ".join(narrative_parts)
        
        # Stable per-station ID (NOT timestamped) so /weather/sync upserts in place
        # instead of accumulating a new row every sync.
        id_string = f"station_{stationname}"
        doc_id = hashlib.md5(id_string.encode()).hexdigest()
        
        return {
            "id": doc_id,
            "location": stationname,
            "source_type": "forecast",
            "headline": national_forecast["title"],
            "narrative_text": narrative_text,
            "issued_at": timestamp,
            "payload": json.dumps({
                # Core station data (for RAG queries)
                "stationname": stationname,
                "timestamp": timestamp,
                "weatherdescription": weatherdescription,
                "temperature": temperature,
                "feeltemperature": feeltemperature,
                "humidity": humidity,
                "windspeed": windspeed,
                "windgust": windgust,
                "winddirection": winddirection,
                "windforce": windforce,
                "windazimuth": windazimuth,
                "visibility": visibility,
                "pressure": pressure,
                "groundtemperature": groundtemperature,
                "irradiance": irradiance,
                "precipitation": precipitation,
                "rainlast24hour": rainlast24hour,
                "rainlasthour": rainlasthour,
                "icon_url": icon_url,
                "condition_category": condition_category,
                "condition_detailed": condition_detailed,
                "barometer_fc": barometer_fc,
                "barometer_name": barometer_name,
                "lat": station.get("lat"),
                "lon": station.get("lon"),
            }),
            "synced_at": synced_at,
        }

    def _normalize_national_report(
        self, national_forecast: dict[str, str], synced_at: str
    ) -> dict[str, Any] | None:
        """
        The national weather report as its own document.

        This is the richest free text Buienradar publishes — several paragraphs of
        forecaster prose — and it's the one document long enough that chunking
        genuinely matters. Indexing it once here (rather than appending it to all
        ~40 station documents) keeps the embedding index free of duplicates.
        """
        text = national_forecast.get("text")
        if not text:
            return None

        title = national_forecast.get("title") or "Weerbericht"
        # One stable id: each sync replaces the previous report in place.
        doc_id = hashlib.md5(b"nl_national_report").hexdigest()

        return {
            "id": doc_id,
            "location": "Nederland",
            "source_type": "national_report",
            "headline": title,
            "narrative_text": f"Landelijke verwachting: {text}",
            "issued_at": synced_at,
            "payload": json.dumps(national_forecast),
            "synced_at": synced_at,
        }

    _WEEKDAYS_NL = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]

    def _normalize_forecast_day(self, day: dict[str, Any], synced_at: str) -> dict[str, Any] | None:
        """
        Normalize one entry of Buienradar's national `forecast.fivedayforecast` into a
        document. This is the only source of *future* weather in the feed (the
        stationmeasurements above are current-conditions-only), so it's what answers
        "morgen"/"weekend"-style questions.
        """
        day_str = day.get("day")
        if not day_str:
            return None

        day_date = datetime.fromisoformat(day_str)
        weekday_nl = self._WEEKDAYS_NL[day_date.weekday()]

        weatherdescription = day.get("weatherdescription", "Onbekend")
        mintemp = day.get("mintemperature")
        maxtemp = day.get("maxtemperature")
        rain_chance = day.get("rainChance")
        sun_chance = day.get("sunChance")
        wind_direction = day.get("windDirection")

        narrative_parts = [
            f"Vooruitzicht voor {weekday_nl} {day_date.date().isoformat()}: {weatherdescription}."
        ]
        if mintemp is not None and maxtemp is not None:
            narrative_parts.append(f"Minimumtemperatuur {mintemp}°C, maximumtemperatuur {maxtemp}°C.")
        if rain_chance is not None:
            narrative_parts.append(f"Kans op regen {rain_chance}%.")
        if sun_chance is not None:
            narrative_parts.append(f"Kans op zon {sun_chance}%.")
        if wind_direction:
            narrative_parts.append(f"Windrichting {wind_direction}.")

        narrative_text = " ".join(narrative_parts)

        # Stable per-day ID so re-syncing upserts the same 5 rows instead of growing.
        id_string = f"nl_forecast_{day_date.date().isoformat()}"
        doc_id = hashlib.md5(id_string.encode()).hexdigest()

        return {
            "id": doc_id,
            "location": "Nederland",
            "source_type": "forecast_daily",
            "headline": f"Dagvooruitzicht {weekday_nl}",
            "narrative_text": narrative_text,
            "issued_at": day_date.isoformat(),
            "payload": json.dumps(day),
            "synced_at": synced_at,
        }
