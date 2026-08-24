"""
Is a venue open on a given day? — a deliberately narrow reader of OSM opening hours.

The venue corpus carries OpenStreetMap's `opening_hours` strings verbatim:
"Tu-Th 10:00-16:00; Jul-Aug Su 10:00-16:00", "Mo-Fr 09:00-17:00; PH off",
"24/7". Nothing checked them against the day being asked about, and it showed:
asked what to do on Saturday 29 August, the answer recommended the Geniemuseum,
whose own hours in that same response say Tuesday to Thursday. Correct data,
useless advice, and only discoverable by standing in front of a closed door.

OSM's opening_hours is a full grammar — holidays, week numbers, sunset, date
ranges, comments in quotes — and a parser that half-understands it is worse than
none, because a wrong "open" is exactly the confident-and-mistaken answer this
whole project is built to avoid. So this reads a narrow subset and refuses
everything else:

    True   this day matches a rule that opens the venue
    False  every rule was understood and none of them opens this day
    None   something in the string was not understood, or there is no string

None is the common case and that is fine. "Openingstijden onbekend, check even
de website" is a useful sentence; "geopend" when it is shut is not.

Two things it deliberately does not know:

  - Public and school holidays. `PH`/`SH` rules are skipped rather than guessed
    at, so a bank holiday can still produce True for a venue that is closed.
    The tool response says as much in its caveats.
  - Times. A day is open or it is not; whether 15:00 falls inside 10:00-16:00 is
    a question the agent can answer from `openingstijden` itself, which travels
    with every venue anyway.
"""

import re
from datetime import date

# OSM weekday abbreviations, in Python's Monday-is-0 order.
WEEKDAYS = ("mo", "tu", "we", "th", "fr", "sa", "su")

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec")

# "10:00-16:00", "10:00-12:00,13:00-17:00" and the open-ended "13:00+" (opens
# then, closing time not stated) — recognised so they can be ignored: this
# module answers a question about days, not hours. The open-ended form earns its
# line by being unambiguous, not by being common: measured against this corpus it
# rescued two strings out of 483, and refusing a string we can plainly read is a
# worse trade than one extra branch.
_SPAN = r"\d{1,2}:\d{2}(?:-\d{1,2}:\d{2}|\+)"
_TIMES = re.compile(rf"^{_SPAN}(,{_SPAN})*$")

_CLOSED_WORDS = {"off", "closed"}

# Holiday rules. Skipped, not parsed: whether a given date is a public holiday
# is a question about the Dutch calendar that this module has no business
# answering, and getting it wrong in either direction is worse than silence.
_HOLIDAY_WORDS = {"ph", "sh"}


def open_on(spec: str | None, day: date) -> bool | None:
    """
    Is a venue with these opening hours open on `day`?

    Args:
        spec: An OSM `opening_hours` string, or None.
        day: The date being asked about.

    Returns:
        True, False, or None when the string could not be read with confidence.
    """
    if not spec or not spec.strip():
        return None

    text = spec.strip()
    if text.replace(" ", "") == "24/7":
        return True

    opens_today = False
    for rule in (r.strip() for r in text.split(";")):
        if not rule:
            continue

        parsed = _parse_rule(rule)
        if parsed is _UNREADABLE:
            # One rule we cannot read makes the whole string unsafe: the part we
            # skipped could be the "Sa off" that decides the answer.
            return None
        if parsed is _SKIP:
            continue

        weekdays, months, closed = parsed
        if months is not None and day.month not in months:
            continue
        if weekdays is not None and day.weekday() not in weekdays:
            continue

        # A rule that applies to this day and closes it wins outright: "Mo-Su
        # 10:00-17:00; Sa off" is a real and common shape.
        if closed:
            return False
        opens_today = True

    return opens_today


# Sentinels, so a rule that yields no weekdays and no months ("10:00-17:00",
# meaning every day) is not confused with one that could not be read.
_UNREADABLE = object()
_SKIP = object()


def _parse_rule(rule: str):
    """
    One `;`-separated rule to (weekdays, months, closed).

    weekdays/months are None when the rule does not restrict them — "10:00-17:00"
    applies to every day of every month. Returns _SKIP for holiday rules and
    _UNREADABLE for anything outside the subset.
    """
    weekdays = None
    months = None
    closed = False

    for token in rule.split():
        lowered = token.lower().rstrip(",")

        if lowered in _CLOSED_WORDS:
            closed = True
            continue
        if any(part.split("-")[0] in _HOLIDAY_WORDS
               for part in lowered.split(",")):
            return _SKIP
        if _TIMES.match(lowered):
            continue

        as_weekdays = _span(lowered, WEEKDAYS)
        if as_weekdays is not None:
            weekdays = as_weekdays if weekdays is None else weekdays | as_weekdays
            continue

        as_months = _span(lowered, MONTHS)
        if as_months is not None:
            months = as_months if months is None else months | as_months
            continue

        # Sunset, week numbers, "Mar 15-Oct 31", quoted comments, anything else.
        return _UNREADABLE

    return weekdays, months, closed


def _span(token: str, names: tuple[str, ...]) -> set[int] | None:
    """
    "tu-th" or "mo,we,fr" to a set of indices into `names`, or None if it is not
    one of those at all. A range that wraps ("sa-su" is fine, "fr-mo" wraps into
    the next week) is handled, because opening hours genuinely say "Sa-Mo".
    """
    out = set()
    for part in token.split(","):
        if not part:
            return None
        if "-" in part:
            start, _, end = part.partition("-")
            if start not in names or end not in names:
                return None
            first, last = names.index(start), names.index(end)
            if first <= last:
                out.update(range(first, last + 1))
            else:
                out.update(list(range(first, len(names))) + list(range(0, last + 1)))
        else:
            if part not in names:
                return None
            out.add(names.index(part))

    # Months are 1-based everywhere else in this codebase; weekdays are 0-based
    # because that is what date.weekday() gives.
    return {n + 1 for n in out} if names is MONTHS else out
