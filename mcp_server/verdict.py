"""
The judgement layer: is a day one to be outside for, and which day is best?

Lifted verbatim from Day 2's app.py (`outdoor_verdict`, `day_score`, `nl_number`)
because this is the part the Day 3 assignment actually grades: "the prediction
tool does more than echo the raw API — it applies some threshold/logic of your
choosing and explains it". The thresholds below were tuned against real
Buienradar output over a fortnight, and the reasoning that produced them is kept
in the comments rather than summarised away.

Pure functions over one forecast-day dict — no network, no database, no clock.
That is what makes them testable, and it is why they live here instead of inside
the @mcp.tool functions in weather_mcp_server.py.

The day dict is the shape weather_client.fetch_local_forecast() returns per day:
    {date, min_temperature, max_temperature, rain_chance, sun_chance,
     windforce, daytime_precipitation_mm, description, is_partial}
Every field is optional; both functions degrade to 'gemengd'/0 rather than
raising when a field is missing, because a forecast source that drops a field is
a normal Tuesday.
"""

# A thunderstorm or fog code vetoes a fine-looking day regardless of the numbers.
SEVERE_WORDS = ('onweer', 'buien', 'hagel', 'mist', 'sneeuw', 'ijzel', 'storm')


def nl_number(value):
    """A number with a Dutch decimal comma."""
    return f"{value:.1f}".rstrip('0').rstrip('.').replace('.', ',')


def outdoor_verdict(day):
    """
    Is today better spent indoors or outdoors, from the forecast we already have?

    Returns 'binnen', 'buiten' or 'gemengd' plus a short Dutch reason. Never
    asserts more than the data supports: anything between the two thresholds is
    'gemengd', and callers show both kinds of venue rather than picking for the
    user.

    Known weaknesses, documented in README_WEATHER.md: rain *chance* is not rain
    *amount* (an 80% chance of 0.2 mm drizzle vetoes a zoo), a whole-day figure
    hides "rain until 11:00, then clear", and late in the evening the day is
    partial so its max temperature is narrowed.
    """
    if not day:
        return 'gemengd', "geen verwachting beschikbaar"

    rain = day.get('rain_chance')
    high = day.get('max_temperature')
    wind = day.get('windforce')
    mm = day.get('daytime_precipitation_mm')
    description = (day.get('description') or '').lower()

    try:
        high = float(high) if high is not None else None
        rain = float(rain) if rain is not None else None
        wind = float(wind) if wind is not None else None
        mm = float(mm) if mm is not None else None
    except (TypeError, ValueError):
        return 'gemengd', "verwachting onleesbaar"

    # Amount first, then probability. A 70% chance of 0.2 mm drizzle is a fine
    # afternoon; a 40% chance of 12 mm is not, and a single scalar cannot say so.
    # Both figures cover the daytime blocks only — nobody plans an outing around
    # rain that falls at 03:00.
    if mm is not None and mm >= 3.0:
        return 'binnen', f"{nl_number(mm)} mm regen overdag"
    if mm is not None and mm >= 1.0 and (rain or 0) >= 50:
        return 'binnen', f"{nl_number(mm)} mm regen en {int(rain)}% kans"
    if mm is None and rain is not None and rain >= 60:
        return 'binnen', f"{int(rain)}% kans op regen"
    if high is not None and high < 4:
        return 'binnen', f"maar {round(high)} graden"
    if high is not None and high > 30:
        return 'binnen', f"{round(high)} graden, aan de warme kant"
    if wind is not None and wind >= 7:
        return 'binnen', f"windkracht {int(wind)}"

    # A thunderstorm or fog code vetoes a fine-looking day regardless of the
    # numbers: "kans op enkele pittige (onweers)buien" carries a low daily rain
    # figure and is not a day to be out in.
    if any(word in description for word in SEVERE_WORDS):
        return 'gemengd', "kans op onweer of slecht zicht"

    dry = (mm is not None and mm < 0.5) or (mm is None and rain is not None and rain <= 20)
    if dry and high is not None and 10 <= high <= 28:
        # "Prima weer met 51% kans op regen" reads as a contradiction, because it
        # is one. The two figures say different things — barely any rain is
        # expected, but a shower is possible — so say that rather than quoting a
        # scary percentage under a cheerful heading.
        if rain is not None and rain > 30:
            return 'buiten', (f"{round(high)} graden en vrijwel droog, "
                              f"al is er {int(rain)}% kans op een bui")
        chance = f" en {int(rain)}% kans op regen" if rain is not None else ""
        return 'buiten', f"{round(high)} graden{chance}"

    return 'gemengd', "wisselvallig"


def day_score(day):
    """
    How suitable is a day for going out, 0–100, with the reason in Dutch.

    `outdoor_verdict` answers "indoors or out?" for one day; comparing days needs
    a number. Same signals, same thresholds — expressed as penalties so two
    reasonable days can still be put in order.
    """
    if not day:
        return 0, "geen verwachting"

    description = (day.get('description') or '').lower()
    try:
        rain = day.get('rain_chance')
        high = day.get('max_temperature')
        wind = day.get('windforce')
        sun = day.get('sun_chance')
        mm = day.get('daytime_precipitation_mm')
        mm = float(mm) if mm is not None else None
        rain = float(rain) if rain is not None else None
        high = float(high) if high is not None else None
        wind = float(wind) if wind is not None else None
        sun = float(sun) if sun is not None else None
    except (TypeError, ValueError):
        return 0, "verwachting onleesbaar"

    score = 100.0
    bits = []

    # Amount dominates, chance modulates. Scoring on the "rain_chance" field
    # alone made this a sort by a number that was never a probability, and it
    # ranked the wettest day of the week as the best day out.
    if mm is not None:
        score -= min(mm, 10.0) * 7      # 10 mm of daytime rain costs 70 points
        bits.append(f"{nl_number(mm)} mm regen overdag")
    if rain is not None:
        score -= rain * (0.15 if mm is not None else 0.8)
        bits.append(f"{int(rain)}% kans op regen")
    if any(word in description for word in SEVERE_WORDS):
        score -= 25
        bits.append("kans op onweer of buien")
    if high is not None:
        # Comfort is a band, not a maximum: 22°C beats 31°C for a day out.
        if high < 18:
            score -= (18 - high) * 3
        elif high > 26:
            score -= (high - 26) * 4
        bits.append(f"{round(high)} graden")
    if wind is not None and wind >= 5:
        score -= (wind - 4) * 6
        bits.append(f"windkracht {int(wind)}")
    if sun is not None:
        score += sun * 0.1             # a small bonus, never decisive
        bits.append(f"{int(sun)}% kans op zon")

    return max(0.0, min(100.0, score)), ", ".join(bits)
