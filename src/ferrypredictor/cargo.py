"""Livestock and timber: can they travel on each sailing?

Farmers and hauliers need this as much as the sailing itself. CalMac doesn't flag it in a field: it
says so in the text of a weather notice. The first case we logged (route 301, Troon to Brodick, notice
issued 17:47 on 25 November 2026, updated 18:07):

    "Due to adverse weather forecast of strong winds, service from the afternoon up to and including
    the first sailing on Friday is liable to disruption or cancellation at short notice.
    No livestock or timber will be carried on these crossings. Following the first crossing on Friday,
    livestock and timber will be carried on a case-by-case basis."

Livestock and timber have always come together so far, so they share one light. If CalMac ever
splits them, the sentence is kept, and `kinds` says which were named.

CalMac's word on a sailing (from active SAILING notices):
- barred: "No livestock or timber will be carried on these crossings."
- case_by_case: "... will be carried on a case-by-case basis."
- carried: "... will be carried" with no condition (a restriction lifted).

Which crossings a notice means ("these crossings") is read from the notice:
- days: the notice's start and end (UK dates), else the date in its title;
- start: "afternoon" moves the start to 12:00 on the first day, "evening" to 17:00, and "from the first
  sailing on Friday" to that sailing;
- end: "up to and including the first sailing on Friday" ends it at that day's first sailing;
- case-by-case after "following the first crossing on Friday" covers that day's later sailings, and
  overrides the notice's window (it is the more specific statement).

The light for each sailing:
- red: CalMac bars livestock and timber, or has cancelled the sailing (CalMac);
  or our chance it runs is under 40% (estimate);
- amber: CalMac says case by case (CalMac); or, as an estimate, the chance it runs is under 75%,
  the weather chance of cancellation is 10% or more, or CalMac has the sailing at risk or diverted;
- green: CalMac says they will be carried (CalMac); otherwise, as an estimate, nothing above applies.

The estimate thresholds are a starting point, set from one notice. CalMac barred livestock on the
Troon route on 26 November when our weather chance of cancellation was only 3-6%, so the estimate is
likely to under-call amber on route 301 until it learns. Every notice version is logged, so restriction
labels can be rebuilt from data/notices at any time; predictions archive the light as issued, so it
can be scored and the thresholds fitted once there are enough restriction days.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from . import parse
from .parse import UK, WEEKDAYS, Sailing

RED_BELOW = 0.40
AMBER_BELOW = 0.75
AMBER_WEATHER_AT = 0.10

CARGO_RE = re.compile(r"\b(livestock|timber|logs)\b", re.I)
CASE_RE = re.compile(r"\bcase[\s-]+by[\s-]+case\b", re.I)
BARRED_RE = re.compile(
    r"\bno\s+(?:livestock|timber)\b"
    r"|\b(?:will|can|could)\s*not\s+be\s+(?:carried|conveyed|accepted|transported)\b"
    r"|\bwon't\s+be\s+(?:carried|conveyed|accepted|transported)\b"
    r"|\bnot\s+(?:be\s+)?(?:permitted|allowed)\b",
    re.I,
)
CARRIED_RE = re.compile(r"\bwill\s+(?:now\s+|again\s+)?be\s+(?:carried|conveyed|accepted)\b", re.I)
WD = "|".join(WEEKDAYS)
UNTIL_FIRST_RE = re.compile(rf"\bup\s+to\s+and\s+including\s+the\s+first\s+(?:sailing|crossing)\s+on\s+({WD})\b", re.I)
FROM_FIRST_RE = re.compile(rf"\bfrom\s+the\s+first\s+(?:sailing|crossing)\s+on\s+({WD})\b", re.I)
AFTER_FIRST_RE = re.compile(rf"\b(?:following|after)\s+the\s+first\s+(?:sailing|crossing)\s+on\s+({WD})\b", re.I)
AFTERNOON_RE = re.compile(r"\bafternoon\b", re.I)
EVENING_RE = re.compile(r"\bevening\b", re.I)
MORNING_RE = re.compile(r"\bmorning\b", re.I)
RANK = {"barred": 3, "case_by_case": 2, "carried": 1}


def _sentences(detail: str) -> list[str]:
    text = (detail or "").replace("**", "").replace("__", "").replace("\r", "")
    out = []
    for line in text.split("\n"):
        out += [s.strip() for s in re.split(r"(?<=[.!?])\s+", line) if s.strip()]
    return out


def classify(sentence: str) -> str | None:
    """CalMac's word on livestock and timber in one sentence, or None if it says nothing about them."""
    if not CARGO_RE.search(sentence):
        return None
    if CASE_RE.search(sentence):
        return "case_by_case"
    if BARRED_RE.search(sentence):
        return "barred"
    if CARRIED_RE.search(sentence):
        return "carried"
    return None


def _days(notice: dict) -> list[date]:
    start, end = notice.get("start_at"), notice.get("end_at")
    if start and end and end > start:
        first = start.astimezone(UK).date()
        last_moment = end.astimezone(UK) - timedelta(seconds=1)  # end is exclusive (midnight)
        last = last_moment.date()
        if 0 <= (last - first).days <= 7:
            return [first + timedelta(days=k) for k in range((last - first).days + 1)]
    stamp = notice.get("updated_at") or notice.get("first_seen_at")
    one = parse.title_date(notice.get("title") or "", stamp.astimezone(UK).date()) if stamp else None
    return [one] if one else []


def notice_restrictions(notice: dict, timetable: list[Sailing]) -> dict[str, dict]:
    """Livestock and timber status per sailing key named by one notice version."""
    sentences = _sentences(notice.get("detail") or "")
    said = [(s, classify(s)) for s in sentences]
    said = [(s, k) for s, k in said if k]
    if not said:
        return {}
    days = _days(notice)
    if not days:
        return {}
    route = notice["route_code"]
    on_route = sorted((s for s in timetable if s.route == route and s.date in days), key=lambda s: (s.date, s.minutes))
    first_of = {}
    for s in on_route:
        first_of.setdefault(s.date, s)

    context = " ".join(s for s, k in [(s, classify(s)) for s in sentences] if k is None)  # the disruption wording
    start_min = 0
    if EVENING_RE.search(context) and not MORNING_RE.search(context):
        start_min = 17 * 60
    elif AFTERNOON_RE.search(context) and not MORNING_RE.search(context):
        start_min = 12 * 60
    start_cut: tuple[date, int] | None = None
    since = FROM_FIRST_RE.search(context)
    if since:
        d = parse._weekday_near(since.group(1), days[0])
        if d in first_of:
            start_cut = (d, first_of[d].minutes)
    end_cut: tuple[date, int] | None = None
    until = UNTIL_FIRST_RE.search(context)
    if until:
        d = parse._weekday_near(until.group(1), days[0])
        if d in first_of:
            end_cut = (d, first_of[d].minutes)

    def in_window(s: Sailing) -> bool:
        if s.date == days[0] and s.minutes < start_min:
            return False
        if start_cut and (s.date, s.minutes) < start_cut:
            return False
        if end_cut and (s.date, s.minutes) > end_cut:
            return False
        return True

    out: dict[str, dict] = {}

    def put(s: Sailing, status: str, sentence: str) -> None:
        cur = out.get(s.key)
        if cur is None or RANK[status] > RANK[cur["status"]]:
            out[s.key] = {"status": status, "sentence": sentence, "notice_id": notice["notice_id"],
                          "kinds": sorted({m.lower() for m in CARGO_RE.findall(sentence)})}

    # Sentences anchored to a crossing ("Following the first crossing on Friday, ... case-by-case") are more
    # specific than the notice's window, so they override it: on 26 November CalMac wrote "service from the first
    # sailing on Friday is liable to disruption ... No livestock or timber will be carried on this crossing.
    # Following the first crossing on Friday, livestock and timber will be carried on a case-by-case basis."
    anchored = []
    for sentence, status in said:
        after = AFTER_FIRST_RE.search(sentence)
        if after:
            anchored.append((after, sentence, status))
            continue
        for s in on_route:
            if in_window(s):
                put(s, status, sentence)
    for after, sentence, status in anchored:
        d = parse._weekday_near(after.group(1), days[0])
        first = first_of.get(d)
        for s in on_route:
            if s.date == d and first and s.minutes > first.minutes:
                out[s.key] = {"status": status, "sentence": sentence, "notice_id": notice["notice_id"],
                              "kinds": sorted({m.lower() for m in CARGO_RE.findall(sentence)})}
    return out


def restrictions(live: dict[str, dict], timetable: list[Sailing]) -> dict[str, dict]:
    """CalMac's current word on livestock and timber, per sailing key, from the live notice versions."""
    out: dict[str, dict] = {}
    for n in sorted(live.values(), key=lambda n: n.get("updated_at") or n.get("first_seen_at")):
        for key, r in notice_restrictions(n, timetable).items():
            out[key] = r  # the newest notice wins
    return out


def light(restriction: dict | None, kinds: set[str] | list[str], cancelled: bool,
          p_runs: float | None, p_weather: float | None) -> dict | None:
    """{light: red|amber|green, source: calmac|estimate, why} for one sailing, or None if we can't say."""
    if cancelled:
        return {"light": "red", "source": "calmac", "why": "CalMac has cancelled this sailing."}
    if restriction:
        st = restriction["status"]
        if st == "barred":
            return {"light": "red", "source": "calmac", "why": restriction["sentence"]}
        if st == "case_by_case":
            return {"light": "amber", "source": "calmac", "why": restriction["sentence"]}
        if st == "carried":
            return {"light": "green", "source": "calmac", "why": restriction["sentence"]}
    if p_runs is None:
        return None
    if round(p_runs * 100) < RED_BELOW * 100:
        return {"light": "red", "source": "estimate", "why": "The sailing itself is unlikely to run."}
    if round(p_runs * 100) < AMBER_BELOW * 100:
        return {"light": "amber", "source": "estimate", "why": "The sailing may not run, so restrictions are likely."}
    if p_weather is not None and p_weather >= AMBER_WEATHER_AT:
        return {"light": "amber", "source": "estimate", "why": "Strong enough wind that CalMac may restrict livestock and timber."}
    if set(kinds) & {"at_risk", "at_risk_diversion", "diverted"}:
        return {"light": "amber", "source": "estimate", "why": "CalMac has warned about this sailing; restrictions often come with that."}
    return {"light": "green", "source": "estimate", "why": "No sign of restrictions in the forecast."}
