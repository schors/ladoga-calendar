#!/usr/bin/env python3
"""Build the Ladoga calendar data files from ladoga-definitions.json.

Writes, per language, one file per civil year, a holidays file and the time-of-day
names, plus language-independent time-of-day files and an index:
    data/index.json
    data/<language>/holidays.json
    data/<language>/<YYYY>.json
    data/<language>/time.json
    data/time/<YYYY>.json

data/time/ is built from data/sun/, which scripts/build_sun.py writes; this
script only reads data/sun/ and needs nothing outside the standard library.

The output is deterministic: the same definitions always give byte-identical
files. Nothing is written until the whole output has been built in memory.
Files are removed only inside data/<language>/ for the languages being built
and inside data/time/.

Usage:
    python3 scripts/build_calendar.py           # (re)write the data files
    python3 scripts/build_calendar.py --check   # compare files on disk, write nothing
Exit code: 0 on success, 1 if --check finds differences or the input is invalid.
"""
import argparse
import calendar
import collections
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DEFINITIONS = ROOT / "ladoga-definitions.json"
PLACEHOLDER = re.compile(r"\{(\w+)\}")

# Rule values this generator implements; anything else is refused.
SUPPORTED_RULES = {
    "anchor_selection": "nearest",
    "tie_breaker": "past_then_holiday_order",
    "candidate_year_offsets": [-1, 0, 1],
}
# Holiday fields that templates may use.
HOLIDAY_TEXT_FIELDS = ["name", "short_name", "name_genitive", "on_day_phrase"]
# Holiday fields published in data/<language>/holidays.json.
PUBLISHED_HOLIDAY_FIELDS = ["id", "month", "day", "name", "short_name", "name_genitive",
                            "on_day_phrase", "aliases", "meaning", "suggested_custom",
                            "historical_note"]
NUMBER_FIELDS = ["cardinal", "days_word", "ordinal"]
# Days after a holiday read either "after the holiday" or as days of the holiday itself.
AFTER_KINDS = ["after", "own_day"]
# Template kinds and placeholders of each phrase group. The short form of the
# next day uses the holiday's after kind, so it has no template of its own.
FORMAT_TEMPLATES = {
    "day_phrases": (["on_day", "eve", "next_day", "before"] + AFTER_KINDS,
                    set(HOLIDAY_TEXT_FIELDS) | set(NUMBER_FIELDS)),
    "short_phrases": (["on_day", "eve", "before"] + AFTER_KINDS,
                      set(HOLIDAY_TEXT_FIELDS) | {"era_year", "count", "day_number"}),
}

TIME_DIR = "time"
SUN_DIR = "sun"
SUN_EVENTS = ["sunrise", "solar_noon", "sunset"]
PERIODS = ["day", "night"]
PORA_TEXT_FIELDS = ["short_name", "name", "name_genitive", "phrase", "meaning"]
PUBLISHED_PORA_FIELDS = ["id", "period", "first_hour", "last_hour"] + PORA_TEXT_FIELDS
SUPPORTED_CLOCK = {"day_hours": 12, "night_hours": 12, "hours_per_pora": 3,
                   "interval_bounds": "start_included_end_excluded",
                   "local_boundary_rounding": "up_to_whole_second"}
SUPPORTED_RELATIVE = {"events": SUN_EVENTS, "candidate_day_offsets": [-1, 0, 1], "selection": "nearest",
                      "tie_breaker": "past", "display_unit": "minute", "rounding": "nearest_minute_half_up"}
# Placeholders each time template may use.
TIME_TEMPLATE_FIELDS = {
    "seasonal": {"hour", "period_adjective"},
    "seasonal_short": {"period_letter", "hour_2d"},
    "relative_before": {"duration", "event_genitive"},
    "relative_after": {"duration", "event_genitive"},
    "relative_near": {"event_genitive"},
    "full_text": {"day_text", "year_phrase", "pora_phrase"},
}


class DefinitionError(Exception):
    pass


def reject_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    dups = [k for k, c in collections.Counter(keys).items() if c > 1]
    if dups:
        raise DefinitionError(f"duplicate JSON keys: {dups[:5]}")
    return dict(pairs)


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def fill(template, values):
    """Replace {name} placeholders with string values; unknown names are an error."""
    def repl(m):
        value = values.get(m.group(1))
        if not isinstance(value, str):
            raise DefinitionError(f"placeholder {{{m.group(1)}}} in {template!r} has no text value")
        return value
    return PLACEHOLDER.sub(repl, template)


def output_dir(defs):
    """Resolve build.output_dir and make sure it is a subfolder of the repository."""
    raw = defs["build"].get("output_dir")
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise DefinitionError(f"build.output_dir must be a non-empty relative path, got {raw!r}")
    out = (ROOT / raw).resolve()
    if out == ROOT or ROOT not in out.parents:
        raise DefinitionError(f"build.output_dir must be a subfolder of the repository, got {raw!r}")
    for protected in (ROOT / "scripts", ROOT / ".git"):
        if out == protected or protected in out.parents:
            raise DefinitionError(f"build.output_dir {raw!r} would overlap project files")
    return out


def validate(defs):
    """Refuse definitions the generator cannot turn into correct data."""
    b = defs["build"]
    start, end = b.get("start_year"), b.get("end_year")
    if not (is_int(start) and is_int(end)):
        raise DefinitionError("build.start_year and build.end_year must be integers")
    # Year +-1 is needed for anchor candidates and for range edge checks.
    if not (2 <= start <= end <= 9998):
        raise DefinitionError(f"build range {start}-{end} is empty or outside 2..9998")
    if not isinstance(defs.get("language"), str) or not re.fullmatch(r"[a-z]{2,3}", defs["language"]):
        raise DefinitionError(f"language must be a short language code, got {defs.get('language')!r}")
    if not is_int(defs["era"].get("year_offset")):
        raise DefinitionError("era.year_offset must be an integer")

    fmt = defs["formatting"]
    for key, value in SUPPORTED_RULES.items():
        if fmt.get(key) != value:
            raise DefinitionError(f"formatting.{key} is {fmt.get(key)!r}; this generator supports only {value!r}")

    holidays = defs["holidays"]
    if not holidays:
        raise DefinitionError("holidays is empty")
    seen_ids, seen_dates = set(), {}
    for i, h in enumerate(holidays):
        hid = h.get("id")
        where = f"holidays[{i}] ({hid!r})"
        if not isinstance(hid, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", hid):
            raise DefinitionError(f"{where}: id must be lowercase latin letters, digits and _")
        if hid in seen_ids:
            raise DefinitionError(f"{where}: duplicate id")
        seen_ids.add(hid)
        m, d = h.get("month"), h.get("day")
        if not (is_int(m) and is_int(d)):
            raise DefinitionError(f"{where}: month and day must be integers")
        # A fixed annual holiday must exist in every year, so Feb 29 is not allowed.
        try:
            dt.date(2001, m, d)
        except ValueError:
            raise DefinitionError(f"{where}: {m:02d}-{d:02d} does not exist in every year")
        if (m, d) in seen_dates:
            raise DefinitionError(f"{where}: same date as {seen_dates[(m, d)]!r}")
        seen_dates[(m, d)] = hid
        for field in HOLIDAY_TEXT_FIELDS:
            if not isinstance(h.get(field), str) or not h[field].strip():
                raise DefinitionError(f"{where}: {field} must be a non-empty string")
        if h.get("after_kind") not in AFTER_KINDS:
            raise DefinitionError(f"{where}: after_kind must be one of {AFTER_KINDS}")

    for group, (kinds, allowed) in FORMAT_TEMPLATES.items():
        templates = fmt.get(group)
        if not (isinstance(templates, dict) and set(templates) == set(kinds)):
            raise DefinitionError(f"formatting.{group} must hold exactly {kinds}")
        for kind, template in templates.items():
            unknown = set(PLACEHOLDER.findall(template)) - allowed
            if unknown:
                raise DefinitionError(f"formatting.{group}.{kind} uses unknown placeholders {sorted(unknown)}")
    for name, allowed in (("text_template", {"day_text"}), ("full_text_template", {"day_text", "year_phrase"})):
        if not isinstance(fmt.get(name), str) or set(PLACEHOLDER.findall(fmt[name])) - allowed:
            raise DefinitionError(f"formatting.{name} must be a string using only {sorted(allowed)}")
    for n, entry in fmt["numbers"].items():
        if not (n.isdigit() and int(n) >= 2):
            raise DefinitionError(f"formatting.numbers key {n!r} must be a number from 2 up")
        for field in NUMBER_FIELDS:
            if not isinstance(entry.get(field), str):
                raise DefinitionError(f"formatting.numbers[{n}].{field} must be a string")
    output_dir(defs)
    validate_time(defs)


def validate_time(defs):
    t = defs["time"]
    for key, value in SUPPORTED_CLOCK.items():
        if t["clock"].get(key) != value:
            raise DefinitionError(f"time.clock.{key} is {t['clock'].get(key)!r}; this generator supports only {value!r}")
    for key, value in SUPPORTED_RELATIVE.items():
        if t["relative"].get(key) != value:
            raise DefinitionError(f"time.relative.{key} is {t['relative'].get(key)!r}; this generator supports only {value!r}")
    poras = t["poras"]
    expected = [(p, h) for p in PERIODS for h in (1, 4, 7, 10)]
    if (not all(is_int(p.get("first_hour")) and is_int(p.get("last_hour")) for p in poras)
            or [(p.get("period"), p.get("first_hour")) for p in poras] != expected):
        raise DefinitionError("time.poras must be 4 day poras then 4 night poras starting at hours 1, 4, 7, 10")
    ids = [p.get("id") for p in poras]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and re.fullmatch(r"[a-z][a-z0-9_]*", i) for i in ids):
        raise DefinitionError("time.poras ids must be unique lowercase identifiers")
    for p in poras:
        if p.get("last_hour") != p["first_hour"] + 2:
            raise DefinitionError(f"time.poras {p['id']}: last_hour must be first_hour + 2")
        for field in PORA_TEXT_FIELDS:
            if not isinstance(p.get(field), str) or not p[field].strip():
                raise DefinitionError(f"time.poras {p['id']}: {field} must be a non-empty string")
    for name, allowed in TIME_TEMPLATE_FIELDS.items():
        template = t["templates"].get(name)
        if not isinstance(template, str):
            raise DefinitionError(f"time.templates.{name} is missing")
        unknown = set(PLACEHOLDER.findall(template)) - allowed
        if unknown:
            raise DefinitionError(f"time.templates.{name} uses unknown placeholders {sorted(unknown)}")
    for unit in ("hour", "minute"):
        forms = t["unit_forms"].get(unit)
        if not (isinstance(forms, list) and len(forms) == 3 and all(isinstance(f, str) and f for f in forms)):
            raise DefinitionError(f"time.unit_forms.{unit} must hold three forms")
    if set(t["event_names"]) != set(SUN_EVENTS) or set(t["periods"]) != set(PERIODS):
        raise DefinitionError("time.event_names and time.periods must cover all events and periods")


def nearest_anchor(target, holidays, year_offsets):
    """Nearest occurrence; ties go to the past one, then to holiday order."""
    best = None
    for delta in year_offsets:
        for order, h in enumerate(holidays):
            anchor = dt.date(target.year + delta, h["month"], h["day"])
            offset = (target - anchor).days
            key = (abs(offset), 0 if offset >= 0 else 1, order)
            if best is None or key < best[0]:
                best = (key, anchor, h, offset)
    return best[1], best[2], best[3]


def day_text(holiday, offset, fmt):
    phrases = fmt["day_phrases"]
    names = {f: holiday[f] for f in HOLIDAY_TEXT_FIELDS}
    if offset == 0:
        return fill(phrases["on_day"], names)
    if offset == -1:
        return fill(phrases["eve"], names)
    if offset == 1:
        return fill(phrases["next_day"], names)
    # Days before count down to the holiday; days after count the holiday as day one.
    n = -offset if offset < 0 else offset + 1
    number = fmt["numbers"].get(str(n))
    if number is None:
        raise DefinitionError(f"formatting.numbers has no entry for {n}")
    kind = "before" if offset < 0 else holiday["after_kind"]
    return fill(phrases[kind], {**names, **{f: number[f] for f in NUMBER_FIELDS}})


def short_text(holiday, offset, era_year, fmt):
    phrases = fmt["short_phrases"]
    kind = ("on_day" if offset == 0 else "eve" if offset == -1 else "before" if offset < 0
            else holiday["after_kind"])
    values = {f: holiday[f] for f in HOLIDAY_TEXT_FIELDS}
    return fill(phrases[kind], {**values, "era_year": str(era_year), "count": str(abs(offset)),
                                "day_number": str(offset + 1)})


def build_year(year, defs):
    era, fmt, holidays = defs["era"], defs["formatting"], defs["holidays"]
    era_year = year + era["year_offset"]
    year_phrase = fill(era["year_phrase_template"], {"era_year": str(era_year)})

    dates = {}
    day = dt.date(year, 1, 1)
    while day.year == year:
        anchor, h, offset = nearest_anchor(day, holidays, fmt["candidate_year_offsets"])
        text_day = day_text(h, offset, fmt)
        dates[day.isoformat()] = {
            "era_year": era_year,
            "anchor_id": h["id"],
            "anchor_date": anchor.isoformat(),
            "anchor_year": anchor.year,
            "offset_days": offset,
            "day_text": text_day,
            "text": fill(fmt["text_template"], {"day_text": text_day}),
            "full_text": fill(fmt["full_text_template"], {"day_text": text_day, "year_phrase": year_phrase}),
            "short": short_text(h, offset, era_year, fmt),
        }
        day += dt.timedelta(days=1)

    leap = calendar.isleap(year)
    return {
        "schema_version": defs["schema_version"],
        "calendar_id": defs["calendar_id"],
        "calendar_version": defs["calendar_version"],
        "language": defs["language"],
        "year": year,
        "era_year": era_year,
        "year_phrase": year_phrase,
        "is_leap": leap,
        "days": 366 if leap else 365,
        "holiday_dates": {h["id"]: dt.date(year, h["month"], h["day"]).isoformat() for h in holidays},
        "dates": dates,
    }


def build_holidays(defs):
    return {
        "schema_version": defs["schema_version"],
        "calendar_id": defs["calendar_id"],
        "calendar_version": defs["calendar_version"],
        "language": defs["language"],
        "note": "Названия и описания праздников для показа. Источники к историческим оговоркам — в ladoga-definitions.json.",
        "holidays": [{f: h[f] for f in PUBLISHED_HOLIDAY_FIELDS} for h in defs["holidays"]],
    }


def load_zone(defs):
    """The pinned time zone file, checked against its SHA-256."""
    tzf = defs["time"]["timezone_file"]
    path = (ROOT / tzf["path"]).resolve()
    if ROOT not in path.parents or not path.is_file():
        raise DefinitionError(f"time zone file {tzf['path']} is missing or outside the repository")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != tzf["sha256"]:
        raise DefinitionError(f"time zone file {tzf['path']} does not match its SHA-256 in the definitions")
    with open(path, "rb") as f:
        return ZoneInfo.from_file(f, key=defs["timezone"])


def fixed_utc_offset(defs, zone):
    """The UTC offset in seconds, which must be the same on every day of the range and its edges.

    Local times in data/time/ are plain HH:MM:SS strings; that is only safe while
    no local time is skipped or repeated.
    """
    b = defs["build"]
    day = dt.date(b["start_year"], 1, 1) - dt.timedelta(days=1)
    last = dt.date(b["end_year"] + 1, 1, 1)
    offsets = set()
    while day <= last:
        start = dt.datetime(day.year, day.month, day.day, tzinfo=zone)
        nxt = day + dt.timedelta(days=1)
        end = dt.datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone)
        if end.timestamp() - start.timestamp() != 86400:
            raise DefinitionError(f"{day} is not 24 hours long in {defs['timezone']}; data/time/ cannot use plain local times")
        offsets.add(int(start.utcoffset().total_seconds()))
        day = nxt
    if len(offsets) != 1:
        raise DefinitionError(f"{defs['timezone']} changes its UTC offset inside the range: {sorted(offsets)}")
    return offsets.pop()


def parse_utc(text):
    return int(dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp())


def load_sun(defs, out_dir):
    """Read data/sun/ written by build_sun.py: {date: {event: unix seconds}}."""
    t = defs["time"]
    b = defs["build"]
    events = {}
    for year in range(b["start_year"], b["end_year"] + 1):
        path = out_dir / SUN_DIR / f"{year}.json"
        if not path.is_file():
            raise DefinitionError(f"{path.relative_to(ROOT)} is missing; run scripts/build_sun.py first")
        with open(path, encoding="utf-8") as f:
            sun = json.load(f, object_pairs_hook=reject_duplicate_keys)
        expected = {
            "schema_version": defs["schema_version"], "model_id": t["model_id"],
            "sun_data_revision": t["sun_data_revision"], "tzif_sha256": t["timezone_file"]["sha256"],
            "solar_events": t["solar_events"],
        }
        for key, value in expected.items():
            if sun.get(key) != value:
                raise DefinitionError(f"{path.relative_to(ROOT)}: {key} does not match the definitions; run scripts/build_sun.py")
        loc = sun.get("reference_location", {})
        if any(loc.get(k) != t["reference_location"][k] for k in ("id", "latitude_deg", "longitude_deg", "elevation_m")):
            raise DefinitionError(f"{path.relative_to(ROOT)}: reference_location does not match; run scripts/build_sun.py")
        if sun.get("ephemeris_engine", {}).get("sha256") != t["ephemeris_engine"]["sha256"]:
            raise DefinitionError(f"{path.relative_to(ROOT)}: ephemeris engine does not match; run scripts/build_sun.py")
        for key, rec in list(sun["edge_events"].items()) + list(sun["dates"].items()):
            events[dt.date.fromisoformat(key)] = {e: parse_utc(rec[e]) for e in SUN_EVENTS}
    day = dt.date(b["start_year"], 1, 1) - dt.timedelta(days=1)
    while day <= dt.date(b["end_year"] + 1, 1, 1):
        if day not in events:
            raise DefinitionError(f"data/sun/ has no events for {day}")
        day += dt.timedelta(days=1)
    return events


def pora_changes(day, events, poras, offset):
    """Local HH:MM:SS times at which the pora changes on `day`; the first entry is the pora at 00:00.

    A boundary lies k/4 of the way through a day or night. It is rounded up to the
    next whole second, so for whole-second clock readings the change is exact.
    """
    prev, cur, nxt = (events[day + dt.timedelta(days=d)] for d in (-1, 0, 1))
    periods = [(prev["sunset"], cur["sunrise"], poras[4:]),
               (cur["sunrise"], cur["sunset"], poras[:4]),
               (cur["sunset"], nxt["sunrise"], poras[4:])]
    boundaries = []
    for start, end, four in periods:
        length = end - start
        for k, p in enumerate(four):
            boundaries.append((start - (-k * length // 4), p["id"]))
    midnight = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp()) - offset
    active = [pid for b, pid in boundaries if b <= midnight][-1]
    changes = [(midnight, active)] + [(b, pid) for b, pid in boundaries if midnight < b < midnight + 86400]

    def local(sec):
        return time_of_day(sec - midnight)
    return [[local(b), pid] for b, pid in changes]


def time_of_day(seconds):
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def build_time_year(year, defs, events, offset):
    t = defs["time"]
    dates = {}
    day = dt.date(year, 1, 1)
    while day.year == year:
        ev = events[day]
        midnight = int(dt.datetime(year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp()) - offset
        rec = {e: dt.datetime.fromtimestamp(ev[e], dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") for e in SUN_EVENTS}
        for e in SUN_EVENTS:
            rec[f"{e}_local"] = time_of_day(ev[e] - midnight)
        rec["poras"] = pora_changes(day, events, t["poras"], offset)
        dates[day.isoformat()] = rec
        day += dt.timedelta(days=1)
    sign = "+" if offset >= 0 else "-"
    return {
        "schema_version": defs["schema_version"],
        "calendar_id": defs["calendar_id"],
        "calendar_version": defs["calendar_version"],
        "model_id": t["model_id"],
        "sun_data_revision": t["sun_data_revision"],
        "year": year,
        "timezone": defs["timezone"],
        "utc_offset": f"{sign}{abs(offset) // 3600:02d}:{abs(offset) // 60 % 60:02d}",
        "days": len(dates),
        "dates": dates,
    }


def build_time_names(defs):
    t = defs["time"]
    return {
        "schema_version": defs["schema_version"],
        "calendar_id": defs["calendar_id"],
        "calendar_version": defs["calendar_version"],
        "language": defs["language"],
        "model_id": t["model_id"],
        "note": "Названия пор, частей суток и солнечных событий, шаблоны подписей времени. Пора находится по id из data/time/<год>.json.",
        "reference_location": {"id": t["reference_location"]["id"], "name": t["reference_location"]["name"]},
        "poras": [{f: p[f] for f in PUBLISHED_PORA_FIELDS} for p in t["poras"]],
        "periods": t["periods"],
        "event_names": t["event_names"],
        "unit_forms": t["unit_forms"],
        "templates": t["templates"],
        "templates_note": t["templates_note"],
    }


def build_index(defs, years):
    b = defs["build"]
    lang = defs["language"]
    return {
        "schema_version": defs["schema_version"],
        "calendar_id": defs["calendar_id"],
        "calendar_version": defs["calendar_version"],
        "name": defs["name"],
        "definitions": f"../{DEFINITIONS.name}",
        "timezone": defs["timezone"],
        "encoding": defs["encoding"],
        "range": {
            "start": f"{b['start_year']}-01-01",
            "end": f"{b['end_year']}-12-31",
            "inclusive": True,
            "year_count": len(years),
            "day_count": sum(y["days"] for y in years),
            "leap_year_count": sum(y["is_leap"] for y in years),
        },
        "languages": [lang],
        "default_language": lang,
        "holidays": {lang: f"{lang}/holidays.json"},
        "files": {lang: {str(y["year"]): f"{lang}/{y['year']}.json" for y in years}},
        "time_model": {"model_id": defs["time"]["model_id"],
                       "sun_data_revision": defs["time"]["sun_data_revision"],
                       "reference_location": defs["time"]["reference_location"]["id"]},
        "time_names": {lang: f"{lang}/time.json"},
        "time": {str(y["year"]): f"{TIME_DIR}/{y['year']}.json" for y in years},
        "sun": {str(y["year"]): f"{SUN_DIR}/{y['year']}.json" for y in years},
        "usage": {
            "paths": "Все пути в этом файле записаны относительно папки, где он лежит.",
            "year_file": "files[language][YYYY] — файл года",
            "holidays_file": "holidays[language] — названия и описания праздников по anchor_id",
            "ready_date": "dates[YYYY-MM-DD].text — подпись дня без года, с точкой",
            "full_date": "dates[YYYY-MM-DD].full_text — подпись дня вместе с годом",
            "day_only": "dates[YYYY-MM-DD].day_text — подпись дня без точки, для вставки во фразу",
            "short_date": "dates[YYYY-MM-DD].short — год цифрами и расстояние до праздника",
            "year_number": "era_year — номер ладожского лета",
            "year_phrase": "year_phrase",
            "holiday_occurrence": "holiday_dates[holiday_id]",
            "time_file": "time[YYYY] — восход, полдень и закат по дням и смены пор по местному времени",
            "current_pora": "time dates[YYYY-MM-DD].poras — пора последней записи, время которой не позже текущего местного времени",
            "pora_names": "time_names[language] — названия пор по id, шаблоны подписей времени",
            "time_phrase": "time_names templates.full_text: {day_text} и {year_phrase} — из файла года, {pora_phrase} — poras[].phrase из time_names. Сезонный час и подпись «за … до восхода» требуют расчёта: scripts/ladoga_time.py",
            "sun_file": "sun[YYYY] — точные моменты восхода, полудня и заката в UTC; edge_events — соседние дни за краями диапазона",
            "outside_range": "Если года нет в files, дата вне диапазона. Если в файле года нет даты, такой даты не существует. Нельзя молча подставлять другой год.",
            "editing": "Файлы в этой папке создаются скриптами scripts/build_sun.py (папка sun/) и scripts/build_calendar.py (остальное) из ladoga-definitions.json. Вручную их не правят.",
        },
    }


def serialize(obj):
    """Indent 2; inside "dates" each daily record stays on one compact line."""
    if "dates" not in obj:
        return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    head = json.dumps({k: v for k, v in obj.items() if k != "dates"}, ensure_ascii=False, indent=2)
    lines = [f"    {json.dumps(k)}: {json.dumps(v, ensure_ascii=False, separators=(',', ':'))}"
             for k, v in obj["dates"].items()]
    return head[:-2] + ',\n  "dates": {\n' + ",\n".join(lines) + "\n  }\n}\n"


def build_all(defs, out_dir):
    """Return {path relative to output dir: file text}."""
    b = defs["build"]
    lang = defs["language"]
    years = [build_year(y, defs) for y in range(b["start_year"], b["end_year"] + 1)]
    out = {"index.json": serialize(build_index(defs, years)),
           f"{lang}/holidays.json": serialize(build_holidays(defs)),
           f"{lang}/time.json": serialize(build_time_names(defs))}
    for y in years:
        out[f"{lang}/{y['year']}.json"] = serialize(y)
    offset = fixed_utc_offset(defs, load_zone(defs))
    events = load_sun(defs, out_dir)
    for y in years:
        out[f"{TIME_DIR}/{y['year']}.json"] = serialize(build_time_year(y["year"], defs, events, offset))
    return out


def managed_files(out_dir, languages):
    """Files this generator owns: index.json and everything inside the language folders and time/."""
    found = set()
    if (out_dir / "index.json").exists():
        found.add("index.json")
    for lang in languages + [TIME_DIR]:
        lang_dir = out_dir / lang
        if lang_dir.is_dir():
            found |= {p.relative_to(out_dir).as_posix() for p in lang_dir.rglob("*") if p.is_file()}
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare with files on disk, write nothing")
    args = parser.parse_args()

    try:
        with open(DEFINITIONS, encoding="utf-8") as f:
            defs = json.load(f, object_pairs_hook=reject_duplicate_keys)
        validate(defs)
        out_dir = output_dir(defs)
        files = build_all(defs, out_dir)
    except (OSError, ValueError, KeyError, TypeError, DefinitionError) as exc:
        print(f"FAILED: {DEFINITIONS.name}: {exc}")
        return 1

    existing = managed_files(out_dir, [defs["language"]])
    stale = sorted(existing - set(files))

    if args.check:
        problems = [f"missing: {p}" for p in sorted(set(files) - existing)]
        problems += [f"unexpected: {p}" for p in stale]
        problems += [f"differs: {p}" for p in sorted(set(files) & existing)
                     if (out_dir / p).read_bytes() != files[p].encode("utf-8")]
        if problems:
            print(f"FAILED: data files do not match {DEFINITIONS.name}")
            for p in problems:
                print("  " + p)
            return 1
        print(f"OK: {len(files)} files match")
        return 0

    # Write each file through a temporary name, then remove stale files.
    for p, text in files.items():
        target = out_dir / p
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    for p in stale:
        (out_dir / p).unlink()
    print(f"wrote {len(files)} files to {out_dir.relative_to(ROOT)}/" + (f", removed {len(stale)}" if stale else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
