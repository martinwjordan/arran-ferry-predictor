"""parse job: turn CalMac SAILING notice text into per-sailing events.

Rule-based and tested on real notices (design spec, section 7). The notice is split into dated
sections ("**Tuesday 24 November**"); each sentence is classified; each sailing it names
("the 18:10 departing Troon", "**Depart** Troon 10:15") is matched to the logged timetable.

Event types: cancelled, diverted, at_risk, at_risk_diversion, reinstated, review_at.
- "service from the 13:55 departing Brodick ..." applies to that sailing and every later one that day.
- "service is liable to disruption ..." with no sailing named applies to the whole day on that route.
- Diversion timetables ("will operate via Troon to the following timetable:") are matched on exact
  time, or to a unique scheduled sailing within 30 minutes; anything looser is left for review.

Sentences that name a sailing but match no rule, or name a sailing the timetable does not have,
go to data/unparsed/ for review. They are never guessed.

CalMac deletes cancelled sailings from its timetable feed (the 18:10 from Troon and the 07:30 from
Brodick vanished on 23 November 2026), so matching uses every sailing ever logged, removed or not.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa

from .collect import NOTICE_SCHEMA, SAILING_SCHEMA, sailing_key
from .health import update_health
from .storage import append_rows, load_state, read_dataset, save_state

PARSER_VERSION = 1
UK = ZoneInfo("Europe/London")
NEAREST_MINUTES = 30
# Where each route's mainland sailings go when diverted, and the port they replace.
DIVERSION = {"003": ("Troon", "Ardrossan")}

TS = pa.timestamp("us", tz="UTC")
EVENT_SCHEMA = pa.schema(
    [
        pa.field("parser_version", pa.int16(), nullable=False),
        pa.field("notice_id", pa.string(), nullable=False),
        pa.field("notice_updated_at", TS),
        pa.field("route_code", pa.string(), nullable=False),
        pa.field("event_type", pa.string(), nullable=False),
        pa.field("sailing_key", pa.string()),
        pa.field("sailing_route", pa.string()),
        pa.field("sailing_date", pa.date32()),
        pa.field("origin", pa.string()),
        pa.field("departure_local", pa.string()),
        pa.field("diversion_port", pa.string()),
        pa.field("review_time", pa.string()),
        pa.field("reason", pa.string()),
        pa.field("match", pa.string()),
        pa.field("confidence", pa.float64()),
        pa.field("sentence", pa.string()),
        pa.field("parsed_at", TS, nullable=False),
    ]
)
UNPARSED_SCHEMA = pa.schema(
    [
        pa.field("parser_version", pa.int16(), nullable=False),
        pa.field("notice_id", pa.string(), nullable=False),
        pa.field("notice_updated_at", TS),
        pa.field("route_code", pa.string(), nullable=False),
        pa.field("section_date", pa.date32()),
        pa.field("sentence", pa.string(), nullable=False),
        pa.field("why", pa.string(), nullable=False),
        pa.field("parsed_at", TS, nullable=False),
    ]
)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december"]
PORT = r"(Ardrossan|Brodick|Troon)"
TIME = r"(\d{1,2})[:.](\d{2})"
DATE_RE = re.compile(
    rf"\b({'|'.join(WEEKDAYS)})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+({'|'.join(MONTHS)}))?", re.I
)
MONTH_RE = re.compile(rf"\b({'|'.join(MONTHS)})\b", re.I)
MENTION_RE = re.compile(
    rf"\b{TIME}\s+(?:sailing\s+)?(?:departing|departure\s+from|from|ex)\s+{PORT}\b", re.I
)
AFTER_MENTION_DATE_RE = re.compile(
    rf"^\s*(?:on|this)\s+({'|'.join(WEEKDAYS)})(?:\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+({'|'.join(MONTHS)}))?)?", re.I
)
LIST_DEPART_RE = re.compile(rf"^Depart\s+{PORT}\s+{TIME}$", re.I)
LIST_ARRIVE_RE = re.compile(rf"^Arrive\s+{PORT}\s+{TIME}$", re.I)
REVIEW_RE = re.compile(rf"\breview\b.*?\b(?:at|around)\s+(?:approximately\s+|about\s+)?{TIME}", re.I)
CANCEL_REF_RE = re.compile(r"^(?:as a result of|following|due to)\s+the\s+cancellation\s+of\s+(.+?),\s*(.*)$", re.I)
SKIP_RE = re.compile(r"\b(?:bus|buses|coach|taxi|train|kiosk|continue to operate|continue operating)\b", re.I)
REINSTATED_RE = re.compile(r"\breinstated\b|\bwill now (?:operate|sail|run)\b(?!\s+(?:from|to|via)\s+troon)", re.I)
CANCELLED_RE = re.compile(r"\bcancelled\b|\bwill not (?:operate|sail|run)\b", re.I)
AT_RISK_DIVERSION_RE = re.compile(
    r"\bliable to (?:operate|sail|depart|run|divert)\b|\brisk of diversion\b|\bmay (?:operate|sail|be diverted|divert)\b", re.I
)
DIVERTED_RE = re.compile(
    r"\b(?:will|to)\s+(?:now\s+)?(?:operate|sail|depart|run)\s+(?:from|to|via)\s+troon\b"
    r"|\b(?:is|are|will be|has been|have been)\s+diverted\b|\bdivert(?:ing|ed)\s+to\s+troon\b",
    re.I,
)
AT_RISK_RE = re.compile(
    r"\bliable to (?:disruption|cancellation)\b|\brisk of (?:disruption|cancellation)\b"
    r"|\bdisruption or cancellation\b|\bat short notice\b|\bmay be (?:disrupted|cancelled)\b",
    re.I,
)
SERVICE_RE = re.compile(r"\bservices?\b", re.I)


@dataclass(frozen=True)
class Sailing:
    key: str
    route: str
    date: date
    origin: str
    destination: str
    local: str  # departure, HH:MM UK time
    minutes: int  # minutes after midnight, UK time
    removed: bool = False  # dropped from CalMac's feed, usually because it was cancelled
    removed_at: datetime | None = None  # when it was dropped, if it was


@dataclass
class Mention:
    origin: str
    local: str
    date: date
    is_range: bool


def build_timetable(rows: list[dict]) -> list[Sailing]:
    """Latest known version of every sailing ever logged, flagged if CalMac has since removed it.

    Older parts lack sailing_key, so it is rebuilt. When a removal and a re-add share a timestamp
    (CalMac reissued every id at 16:02 on 23 November 2026), the sailing counts as still listed.
    """
    latest: dict[str, tuple] = {}
    for r in rows:
        if r.get("departure_at") is None or r.get("origin") is None:
            continue
        key = r.get("sailing_key") or sailing_key(r["route_code"], r["origin"], r["departure_at"])
        stamp = r["observed_at"]
        alive = r["change_type"] != "removed"
        if key not in latest or stamp > latest[key][0] or (stamp == latest[key][0] and alive):
            latest[key] = (stamp, alive, r)
    out = []
    for key, (_, alive, r) in latest.items():
        local = r["departure_at"].astimezone(UK)
        out.append(Sailing(key, r["route_code"], local.date(), r["origin"], r["destination"] or "",
                           f"{local:%H:%M}", local.hour * 60 + local.minute, removed=not alive,
                           removed_at=None if alive else r["observed_at"]))
    return sorted(out, key=lambda s: (s.route, s.date, s.minutes))


def _hhmm(h: str, m: str) -> str:
    return f"{int(h):02d}:{m}"


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("**", "").replace("__", "")).strip()


def _resolve_date(day: int, month: int, ref: date) -> date | None:
    best = None
    for year in (ref.year - 1, ref.year, ref.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((d - ref).days) < abs((best - ref).days):
            best = d
    return best


def _weekday_near(weekday: str, around: date) -> date:
    target = WEEKDAYS.index(weekday.lower())
    candidates = [around + timedelta(days=k) for k in range(-6, 7)]
    return min((d for d in candidates if d.weekday() == target), key=lambda d: abs((d - around).days))


def _date_from(match: re.Match, ref: date, month_hint: int | None = None) -> date | None:
    weekday, day, month = match.group(1), match.group(2), match.group(3)
    if day and (month or month_hint):
        m = MONTHS.index(month.lower()) + 1 if month else month_hint
        return _resolve_date(int(day), m, ref)
    return _weekday_near(weekday, ref)


def title_date(title: str, ref: date) -> date | None:
    """First date in a title such as 'Monday 23 - Tuesday 24 November' (month given once)."""
    m = DATE_RE.search(title or "")
    if not m:
        return None
    month = m.group(3)
    if not month:
        later = MONTH_RE.search(title[m.end():])
        month = later.group(1) if later else None
    hint = MONTHS.index(month.lower()) + 1 if month else None
    return _date_from(m, ref, hint)


def header_date(line: str, ref: date) -> date | None:
    """A line that is only a date, e.g. '**Tuesday 24 November**'."""
    text = _plain(line).rstrip(":")
    m = DATE_RE.fullmatch(text)
    return _date_from(m, ref) if m and m.group(3) else None


def mentions(text: str, section: date, ref: date) -> list[Mention]:
    out = []
    for m in MENTION_RE.finditer(text):
        when = section
        after = AFTER_MENTION_DATE_RE.match(text[m.end():])
        if after:
            when = _date_from(after, ref) if after.group(2) and after.group(3) else _weekday_near(after.group(1), section)
        before = text[: m.start()].lower()
        is_range = bool(re.search(r"\b(?:from|after)\s+the\s*$", before))
        out.append(Mention(m.group(3).title(), _hhmm(m.group(1), m.group(2)), when, is_range))
    return out


def classify(text: str) -> str | None:
    if SKIP_RE.search(text):
        return "skip"
    if REVIEW_RE.search(text):
        return "review_at"
    if REINSTATED_RE.search(text):
        return "reinstated"
    if CANCELLED_RE.search(text):
        return "cancelled"
    if AT_RISK_DIVERSION_RE.search(text):
        return "at_risk_diversion"
    if DIVERTED_RE.search(text):
        return "diverted"
    if AT_RISK_RE.search(text):
        return "at_risk"
    return None


class _Notice:
    def __init__(self, notice: dict, timetable: list[Sailing], parsed_at: datetime):
        self.n = notice
        self.route = notice["route_code"]
        self.parsed_at = parsed_at
        self.events: list[dict] = []
        self.unparsed: list[dict] = []
        self.seen: set[tuple] = set()
        self.by_slot: dict[tuple, list[Sailing]] = defaultdict(list)
        self.by_day: dict[tuple, list[Sailing]] = defaultdict(list)
        for s in timetable:
            self.by_slot[(s.date, s.origin, s.local)].append(s)
            self.by_day[(s.route, s.date)].append(s)
        stamp = notice.get("updated_at") or notice.get("first_seen_at") or parsed_at
        self.ref = stamp.astimezone(UK).date()

    # --- output -------------------------------------------------------------------------------
    def event(self, kind: str, sentence: str, sailing: Sailing | None = None, *, day: date | None = None,
              match: str = "exact", confidence: float = 1.0, diversion_port: str | None = None,
              review_time: str | None = None) -> None:
        ident = (kind, sailing.key if sailing else (day, review_time))
        if ident in self.seen:
            return
        self.seen.add(ident)
        if kind in ("diverted", "at_risk_diversion") and diversion_port is None:
            diversion_port = DIVERSION.get(sailing.route if sailing else self.route, (None,))[0]
        self.events.append(
            {
                "parser_version": PARSER_VERSION,
                "notice_id": self.n["notice_id"],
                "notice_updated_at": self.n.get("updated_at"),
                "route_code": self.route,
                "event_type": kind,
                "sailing_key": sailing.key if sailing else None,
                "sailing_route": sailing.route if sailing else None,
                "sailing_date": sailing.date if sailing else day,
                "origin": sailing.origin if sailing else None,
                "departure_local": sailing.local if sailing else None,
                "diversion_port": diversion_port,
                "review_time": review_time,
                "reason": self.n.get("reason"),
                "match": match,
                "confidence": confidence,
                "sentence": sentence,
                "parsed_at": self.parsed_at,
            }
        )

    def flag(self, sentence: str, why: str, section: date | None) -> None:
        self.unparsed.append(
            {
                "parser_version": PARSER_VERSION,
                "notice_id": self.n["notice_id"],
                "notice_updated_at": self.n.get("updated_at"),
                "route_code": self.route,
                "section_date": section,
                "sentence": sentence,
                "why": why,
                "parsed_at": self.parsed_at,
            }
        )

    # --- matching -----------------------------------------------------------------------------
    def find(self, day: date, origin: str, local: str) -> list[Sailing]:
        found = self.by_slot.get((day, origin, local), [])
        own = [s for s in found if s.route == self.route]
        return own or found

    def day_sailings(self, day: date) -> list[Sailing]:
        return self.by_day.get((self.route, day), [])

    def apply(self, kind: str, mention: Mention, sentence: str, section: date) -> bool:
        found = self.find(mention.date, mention.origin, mention.local)
        if len(found) != 1:
            why = "no such sailing in the timetable" if not found else "matches more than one sailing"
            self.flag(sentence, f"{why}: {mention.local} from {mention.origin} on {mention.date}", section)
            return False
        start = found[0]
        if mention.is_range and kind != "cancelled":
            for s in self.day_sailings(start.date):
                if s.minutes >= start.minutes:
                    self.event(kind, sentence, s, match="range")
        else:
            self.event(kind, sentence, start)
        return True

    def apply_list_item(self, kind: str, origin: str, local: str, arrive: str | None, day: date, sentence: str) -> None:
        if kind == "diverted":
            divert_to, replaces = DIVERSION.get(self.route, (None, None))
            if origin == divert_to and replaces:
                origin = replaces  # "Depart Troon 07:00" stands in for the 07:00 from Ardrossan
            exact = self.find(day, origin, local)
            if len(exact) == 1:
                self.event(kind, sentence, exact[0], diversion_port=divert_to)
                return
            minutes = int(local[:2]) * 60 + int(local[3:])
            near = [s for s in self.day_sailings(day) if s.origin == origin and abs(s.minutes - minutes) <= NEAREST_MINUTES]
            if len(near) == 1:
                self.event(kind, sentence, near[0], match="nearest", confidence=0.8, diversion_port=divert_to)
                return
            self.flag(sentence, f"diversion timetable entry {local} from {origin} has no clear scheduled match", day)
            return
        found = self.find(day, origin, local)
        if len(found) == 1:
            self.event(kind, sentence, found[0])
        else:
            self.flag(sentence, f"no single sailing {local} from {origin} on {day}", day)

    # --- parsing ------------------------------------------------------------------------------
    def sentence(self, text: str, section: date | None) -> None:
        if section is None:
            if MENTION_RE.search(text) or REVIEW_RE.search(text):
                self.flag(text, "no date for this section", None)
            return
        ref_clause = CANCEL_REF_RE.match(text)
        if ref_clause:
            for m in mentions(ref_clause.group(1), section, self.ref):
                m.is_range = False
                self.apply("cancelled", m, text, section)
            text_rest = ref_clause.group(2)
        else:
            text_rest = text
        kind = classify(text_rest)
        found = mentions(text_rest, section, self.ref)
        if kind == "skip":
            return
        if kind == "review_at":
            r = REVIEW_RE.search(text_rest)
            self.event("review_at", text, day=section, review_time=_hhmm(r.group(1), r.group(2)))
            return
        if kind is None:
            if found:
                self.flag(text, "names a sailing but matches no rule", section)
            return
        if found:
            for m in found:
                self.apply(kind, m, text, section)
        elif kind in ("at_risk", "at_risk_diversion") and SERVICE_RE.search(text_rest):
            for s in self.day_sailings(section):
                self.event(kind, text, s, match="day", confidence=0.9)
        elif kind in ("cancelled", "diverted", "reinstated"):
            self.flag(text, f"{kind} with no sailing named", section)

    def parse(self) -> None:
        section = title_date(self.n.get("title") or "", self.ref)
        lines = [ln.strip() for ln in (self.n.get("detail") or "").replace("\r", "").split("\n")]
        list_kind, last_depart = None, None
        for line in lines:
            if not line:
                continue
            new_section = header_date(line, self.ref)
            if new_section:
                section, list_kind = new_section, None
                continue
            text = _plain(line)
            depart = LIST_DEPART_RE.match(text)
            arrive = LIST_ARRIVE_RE.match(text)
            if list_kind and (depart or arrive):
                if depart:
                    last_depart = (depart.group(1).title(), _hhmm(depart.group(2), depart.group(3)))
                    self.apply_list_item(list_kind, *last_depart, None, section, text)
                continue
            if depart and section:  # a Depart line outside a recognised list
                self.flag(text, "timetable line without a recognised heading", section)
                continue
            if arrive:
                continue
            list_kind = None
            if text.endswith(":"):
                kind = classify(text)
                if kind in ("cancelled", "diverted", "at_risk_diversion", "reinstated"):
                    list_kind = kind
                    continue
            for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text):
                self.sentence(sentence.strip(), section)


def parse_notice(notice: dict, timetable: list[Sailing], parsed_at: datetime) -> tuple[list[dict], list[dict]]:
    p = _Notice(notice, timetable, parsed_at)
    p.parse()
    return p.events, p.unparsed


def notice_version(n: dict) -> str:
    stamp = n.get("updated_at")
    return f"{n['notice_id']}|{stamp.isoformat() if stamp else ''}"


def run_parse(now: datetime, data_dir: Path, site_dir: Path) -> dict:
    data_dir, site_dir = Path(data_dir), Path(site_dir)
    notices = read_dataset(data_dir / "notices", NOTICE_SCHEMA).to_pylist()
    timetable = build_timetable(read_dataset(data_dir / "sailings", SAILING_SCHEMA).to_pylist())
    state_path = data_dir / "state" / "parsed_notices.json"
    state = load_state(state_path, {"parser_version": 0, "parsed": []})
    reparse = state.get("parser_version") != PARSER_VERSION
    done = set() if reparse else set(state.get("parsed", []))

    todo = [n for n in notices if n.get("notice_type") == "SAILING" and notice_version(n) not in done]
    events, unparsed = [], []
    for n in todo:
        e, u = parse_notice(n, timetable, now)
        events += e
        unparsed += u
        done.add(notice_version(n))

    append_rows(data_dir / "events", events, EVENT_SCHEMA, now)
    append_rows(data_dir / "unparsed", unparsed, UNPARSED_SCHEMA, now)
    save_state(state_path, {"parser_version": PARSER_VERSION, "parsed": sorted(done)})
    summary = {
        "parser_version": PARSER_VERSION,
        "reparsed_all": reparse,
        "versions_parsed": len(todo),
        "events": dict(Counter(e["event_type"] for e in events)),
        "unparsed": len(unparsed),
    }
    update_health(site_dir, "parse", now, details=summary)
    return summary
