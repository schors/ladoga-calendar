#!/usr/bin/env python3
"""Independent validator for the Ladoga calendar data.

Checks ladoga-definitions.json and the generated files in data/.
The validator does not use scripts/build_calendar.py and does not trust the
definitions: it has its own reference table of holidays (dates and name forms),
its own Russian number words and its own phrase rules. The definitions are
compared with these references, and every daily record is recomputed from them.

Usage: python3 scripts/check_calendar.py
Exit code: 0 if all checks pass, 1 otherwise.
"""
import calendar
import collections
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFINITIONS = ROOT / "ladoga-definitions.json"
DATA_DIR_NAME = "data"
DATA_DIR = ROOT / DATA_DIR_NAME
ERA_OFFSET = 752
LANGUAGE = "ru"

# Reference holidays in the required order:
# (id, month, day, name, short_name, name_genitive, on_day_phrase).
REFERENCE_HOLIDAYS = [
    ("hearth", 1, 12, "День домашнего огня", "Домашний огонь", "Домашнего огня", "В день Домашнего огня"),
    ("midwinter", 2, 2, "Середина зимы", "Середина зимы", "Середины зимы", "В день Середины зимы"),
    ("gulf_ice", 2, 13, "Ледяной залив", "Ледяной залив", "Ледяного залива", "В день Ледяного залива"),
    ("veles", 2, 24, "Велесов день", "Велес", "Велесова дня", "В Велесов день"),
    ("spring_turn", 3, 20, "Весенний поворот", "Весенний поворот", "Весеннего поворота", "В день Весеннего поворота"),
    ("free_water", 4, 12, "День вольной воды", "Вольная вода", "Вольной воды", "В день Вольной воды"),
    ("smelt", 4, 26, "Корюшкин день", "Корюшка", "Корюшкиного дня", "В Корюшкин день"),
    ("spring_day", 5, 6, "Вешний день", "Вешний день", "Вешнего дня", "В Вешний день"),
    ("boats", 5, 24, "Лодейный день", "Лодейный день", "Лодейного дня", "В Лодейный день"),
    ("white_nights", 6, 10, "Белые ночи", "Белые ночи", "Белых ночей", "В праздник Белых ночей"),
    ("summer_turn", 6, 21, "Солнцеворот", "Солнцеворот", "Солнцеворота", "В день Солнцеворота"),
    ("kupala", 7, 7, "Купала", "Купала", "Купалы", "На Купалу"),
    ("lake_wind", 7, 20, "День ладожского ветра", "Ладожский ветер", "Ладожского ветра", "В день Ладожского ветра"),
    ("perun", 8, 2, "Перунов день", "Перун", "Перунова дня", "В Перунов день"),
    ("hot_rocks", 8, 10, "Горячие скалы", "Горячие скалы", "Горячих скал", "В день Горячих скал"),
    ("first_fruits", 8, 19, "Первые плоды", "Первые плоды", "Первых плодов", "В праздник Первых плодов"),
    ("rowan_fire", 9, 5, "Рябиновый огонь", "Рябиновый огонь", "Рябинового огня", "В день Рябинового огня"),
    ("oseniny", 9, 22, "Осенины", "Осенины", "Осенин", "На Осенины"),
    ("west_wind", 10, 3, "День западного ветра", "Западный ветер", "Западного ветра", "В день Западного ветра"),
    ("earth_closing", 10, 14, "Закрытие земли", "Закрытие земли", "Закрытия земли", "В день Закрытия земли"),
    ("last_boat", 10, 24, "Последняя ладья", "Последняя ладья", "Последней ладьи", "В день Последней ладьи"),
    ("ancestors", 11, 7, "Осенние Деды", "Деды", "Дедов", "На Осенние Деды"),
    ("dark_water", 11, 21, "Тёмная вода", "Тёмная вода", "Тёмной воды", "В день Тёмной воды"),
    ("vainamoinen", 12, 7, "Вечер Вяйнемяйнена", "Вяйнемяйнен", "Вечера Вяйнемяйнена", "В вечер Вяйнемяйнена"),
    ("kolyada", 12, 21, "Коляда", "Коляда", "Коляды", "На Коляду"),
]
REFERENCE_FIELDS = ["id", "month", "day", "name", "short_name", "name_genitive", "on_day_phrase"]
REFERENCE = [dict(zip(REFERENCE_FIELDS, row)) for row in REFERENCE_HOLIDAYS]

# Russian reference number words, written independently of the definitions.
ORDINAL = {2: "второй", 3: "третий", 4: "четвёртый", 5: "пятый", 6: "шестой",
           7: "седьмой", 8: "восьмой", 9: "девятый", 10: "десятый",
           11: "одиннадцатый", 12: "двенадцатый"}
CARDINAL = {2: "два", 3: "три", 4: "четыре", 5: "пять", 6: "шесть", 7: "семь",
            8: "восемь", 9: "девять", 10: "десять", 11: "одиннадцать",
            12: "двенадцать"}

# Rule values the reference algorithm below implements.
RULES = {"anchor_selection": "nearest", "tie_breaker": "past_then_holiday_order",
         "candidate_year_offsets": [-1, 0, 1]}

# Control cases from AGENTS.md: date -> (era_year, anchor_id, anchor_date, offset).
CONTROL_CASES = {
    "2025-01-01": (1273, "kolyada", "2024-12-21", 11),
    "2026-03-08": (1274, "veles", "2026-02-24", 12),
    "2026-06-29": (1274, "summer_turn", "2026-06-21", 8),
    "2026-11-23": (1274, "dark_water", "2026-11-21", 2),
    "2026-12-10": (1274, "vainamoinen", "2026-12-07", 3),
    "2027-01-01": (1275, "kolyada", "2026-12-21", 11),
    "2028-02-29": (1276, "veles", "2028-02-24", 5),
    "2028-03-08": (1276, "spring_turn", "2028-03-20", -12),
    "2038-12-31": (1286, "kolyada", "2038-12-21", 10),
}
# Full expected strings for a few dates.
CONTROL_TEXTS = {
    "2026-11-21": ("В день Тёмной воды, в лето 1274-е от ладожского начала.", "1274 · Тёмная вода"),
    "2026-12-10": ("На третий день после Вечера Вяйнемяйнена, в лето 1274-е от ладожского начала.",
                   "1274 · Вяйнемяйнен +3"),
    "2028-03-08": ("За двенадцать дней до Весеннего поворота, в лето 1276-е от ладожского начала.",
                   "1276 · Весенний поворот -12"),
    "2026-02-18": ("На пятый день после Ледяного залива, в лето 1274-е от ладожского начала.",
                   "1274 · Ледяной залив +5"),
    "2026-08-10": ("В день Горячих скал, в лето 1274-е от ладожского начала.", "1274 · Горячие скалы"),
    "2026-10-02": ("В канун Западного ветра, в лето 1274-е от ладожского начала.", "1274 · Западный ветер -1"),
}

DAILY_FIELDS = ["era_year", "anchor_id", "anchor_date", "anchor_year", "offset_days",
                "day_text", "text", "short"]
YEAR_FIELDS = ["schema_version", "calendar_id", "calendar_version", "language", "year",
               "era_year", "year_phrase", "is_leap", "days", "holiday_dates", "dates"]
INDEX_FIELDS = ["schema_version", "calendar_id", "calendar_version", "name", "definitions",
                "timezone", "encoding", "range", "languages", "default_language", "holidays",
                "files", "usage"]
USAGE_KEYS = ["paths", "year_file", "holidays_file", "ready_date", "day_only", "short_date",
              "year_number", "year_phrase", "holiday_occurrence", "outside_range", "editing"]
HOLIDAYS_FILE_FIELDS = ["schema_version", "calendar_id", "calendar_version", "language", "note", "holidays"]
PUBLISHED_HOLIDAY_FIELDS = ["id", "month", "day", "name", "short_name", "name_genitive",
                            "on_day_phrase", "aliases", "meaning", "suggested_custom",
                            "historical_note"]
SHARED_FIELDS = ["schema_version", "calendar_id", "calendar_version"]


class CheckError(Exception):
    pass


def reject_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    dups = [k for k, c in collections.Counter(keys).items() if c > 1]
    if dups:
        raise CheckError(f"duplicate JSON keys: {dups[:5]}")
    return dict(pairs)


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f, object_pairs_hook=reject_duplicate_keys)
    except (OSError, ValueError, CheckError) as exc:
        raise CheckError(f"{path.relative_to(ROOT)}: {exc}") from None


def same(a, b):
    """Equal values with equal types, so 3 vs 3.0 or True vs 1 do not pass."""
    if isinstance(a, dict) and isinstance(b, dict):
        return list(a) == list(b) and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b and type(a) is type(b)


def day_plural(n):
    if 11 <= n % 100 <= 14:
        return "дней"
    return {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(n % 10, "дней")


def number_words(n):
    if n not in ORDINAL:
        raise CheckError(f"offset {n} has no reference number word; extend ORDINAL and CARDINAL in this script")
    return CARDINAL[n], day_plural(n), ORDINAL[n]


def expected_record(target):
    """Recompute a daily record from the reference holidays only."""
    candidates = []
    for year in (target.year - 1, target.year, target.year + 1):
        for order, h in enumerate(REFERENCE):
            anchor = dt.date(year, h["month"], h["day"])
            offset = (target - anchor).days
            # Sort key: distance, then past before future, then holiday order.
            candidates.append(((abs(offset), 0 if offset >= 0 else 1, order), anchor, h, offset))
    _, anchor, h, offset = min(candidates, key=lambda c: c[0])

    gen = h["name_genitive"]
    if offset == 0:
        day_text = h["on_day_phrase"]
    elif offset == -1:
        day_text = f"В канун {gen}"
    elif offset == 1:
        day_text = f"На следующий день после {gen}"
    elif offset < 0:
        cardinal, days_word, _ = number_words(-offset)
        day_text = f"За {cardinal} {days_word} до {gen}"
    else:
        _, _, ordinal = number_words(offset)
        day_text = f"На {ordinal} день после {gen}"

    era_year = target.year - ERA_OFFSET
    short = f"{era_year} · {h['short_name']}" + ("" if offset == 0 else f" {offset:+d}")
    return {
        "era_year": era_year,
        "anchor_id": h["id"],
        "anchor_date": anchor.isoformat(),
        "anchor_year": anchor.year,
        "offset_days": offset,
        "day_text": day_text,
        "text": f"{day_text}, в лето {era_year}-е от ладожского начала.",
        "short": short,
    }


def collect_source_ids(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "source_ids":
                out.extend(v)
            else:
                collect_source_ids(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_source_ids(v, out)


def check_definitions(defs, errors):
    holidays = defs["holidays"]
    ids = [h.get("id") for h in holidays]
    if ids != [r["id"] for r in REFERENCE]:
        errors.append(f"definitions: holiday ids or their order differ from the reference: {ids}")
    for h, ref in zip(holidays, REFERENCE):
        for field in REFERENCE_FIELDS:
            if not same(h.get(field), ref[field]):
                errors.append(f"definitions: {ref['id']}.{field} is {h.get(field)!r}, reference {ref[field]!r}")

    source_ids = [s["id"] for s in defs["sources"]]
    if len(source_ids) != len(set(source_ids)):
        errors.append("definitions: source ids are not unique")
    referenced = []
    collect_source_ids(defs, referenced)
    for sid in sorted(set(referenced) - set(source_ids)):
        errors.append(f"definitions: unresolved source_id {sid}")
    for sid in sorted(set(source_ids) - set(referenced)):
        errors.append(f"definitions: unused source {sid}")

    era = defs["era"]
    if not (same(era.get("year_offset"), -ERA_OFFSET) and same(era.get("epoch_civil_year"), ERA_OFFSET + 1)):
        errors.append("definitions: era offset or epoch differs from the reference")
    if defs.get("language") != LANGUAGE:
        errors.append(f"definitions: language is {defs.get('language')!r}, validator knows only {LANGUAGE!r}")
    if defs["build"].get("output_dir") != DATA_DIR_NAME:
        errors.append(f"definitions: build.output_dir is {defs['build'].get('output_dir')!r}, expected {DATA_DIR_NAME!r}")

    fmt = defs["formatting"]
    for key, value in RULES.items():
        if fmt.get(key) != value:
            errors.append(f"definitions: formatting.{key} is {fmt.get(key)!r}, the reference rule is {value!r}")

    # Number words in the definitions must match the independent reference.
    numbers = fmt["numbers"]
    if set(numbers) != {str(n) for n in ORDINAL}:
        errors.append(f"definitions: formatting.numbers covers {sorted(numbers, key=int)}, reference covers 2..{max(ORDINAL)}")
    for n in ORDINAL:
        entry = numbers.get(str(n), {})
        exp = {"cardinal": CARDINAL[n], "days_word": day_plural(n), "ordinal": ORDINAL[n]}
        if entry != exp:
            errors.append(f"definitions: formatting.numbers[{n}] is {entry}, expected {exp}")

    obs = defs["observed_events"]
    obs_ids = [e["id"] for e in obs["definitions"]]
    if len(obs_ids) != len(set(obs_ids)):
        errors.append("definitions: observed event ids are not unique")
    for i, o in enumerate(obs["observations"]):
        where = f"definitions: observations[{i}]"
        if set(o) != set(obs["observation_fields"]):
            errors.append(f"{where}: fields {sorted(o)}")
        if o.get("event_id") not in obs_ids:
            errors.append(f"{where}: unknown event {o.get('event_id')!r}")
        try:
            dt.date.fromisoformat(o.get("date"))
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", o.get("date")):
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{where}: date {o.get('date')!r} is not a real YYYY-MM-DD date")
        for field in ("location", "observer_note"):
            if not isinstance(o.get(field), str) or not o[field].strip():
                errors.append(f"{where}: {field} must be a non-empty string")
    if obs["included_in_date_index"] is not False:
        errors.append("definitions: observed events must not be in the date index yet")


def check_record_lines(text, year, records, errors):
    """Each daily record must sit on exactly one line: `    "YYYY-MM-DD": {...}`."""
    by_date = collections.defaultdict(list)
    for line in text.split("\n"):
        m = re.match(r'^    "(\d{4}-\d{2}-\d{2})": (\{.*\}),?$', line)
        if m:
            by_date[m.group(1)].append(m.group(2))
    for key, record in records.items():
        lines = by_date.get(key, [])
        if len(lines) != 1 or json.loads(lines[0]) != record:
            errors.append(f"{year}: record {key} is not on exactly one line of its own")
    for key in set(by_date) - set(records):
        errors.append(f"{year}: line for unexpected date {key}")


def check_data(defs, errors):
    b = defs["build"]
    start, end = b["start_year"], b["end_year"]
    if not (isinstance(start, int) and isinstance(end, int) and 2 <= start <= end <= 9998):
        raise CheckError(f"build range {start!r}-{end!r} is empty or invalid")
    years = list(range(start, end + 1))
    index = load(DATA_DIR / "index.json")

    expected_files = {LANGUAGE: {str(y): f"{LANGUAGE}/{y}.json" for y in years}}
    expected_holidays = {LANGUAGE: f"{LANGUAGE}/holidays.json"}
    if list(index) != INDEX_FIELDS:
        errors.append(f"index: fields are {list(index)}")
    for field in SHARED_FIELDS + ["name", "timezone", "encoding"]:
        if not same(index.get(field), defs[field]):
            errors.append(f"index: {field} differs from definitions")
    for field, value in [("definitions", f"../{DEFINITIONS.name}"), ("languages", [LANGUAGE]),
                         ("default_language", LANGUAGE), ("holidays", expected_holidays),
                         ("files", expected_files)]:
        if not same(index.get(field), value):
            errors.append(f"index: {field} is {index.get(field)!r}, expected {value!r}")
    usage = index.get("usage")
    if not (isinstance(usage, dict) and list(usage) == USAGE_KEYS
            and all(isinstance(v, str) and v for v in usage.values())):
        errors.append("index: usage must hold non-empty texts for " + ", ".join(USAGE_KEYS))

    on_disk = {p.relative_to(DATA_DIR).as_posix() for p in DATA_DIR.rglob("*") if p.is_file()}
    expected_disk = {"index.json", expected_holidays[LANGUAGE]} | set(expected_files[LANGUAGE].values())
    for p in sorted(on_disk - expected_disk):
        errors.append(f"data: unexpected file {p}")

    hol = load(DATA_DIR / expected_holidays[LANGUAGE])
    if list(hol) != HOLIDAYS_FILE_FIELDS:
        errors.append(f"holidays file: fields are {list(hol)}")
    for field in SHARED_FIELDS:
        if not same(hol.get(field), defs[field]):
            errors.append(f"holidays file: {field} differs from definitions")
    if hol.get("language") != LANGUAGE or not isinstance(hol.get("note"), str):
        errors.append("holidays file: language or note is wrong")
    exp_hol = [{f: h[f] for f in PUBLISHED_HOLIDAY_FIELDS} for h in defs["holidays"]]
    if not same(hol.get("holidays"), exp_hol):
        errors.append("holidays file: holidays differ from definitions")

    all_dates = {}
    max_offset = 0
    mismatches = collections.Counter()
    leap_count = 0
    for y in years:
        path = DATA_DIR / LANGUAGE / f"{y}.json"
        if not path.exists():
            errors.append(f"data: missing {path.relative_to(ROOT)}")
            continue
        yd = load(path)
        if list(yd) != YEAR_FIELDS:
            errors.append(f"{y}: fields are {list(yd)}")
        leap = calendar.isleap(y)
        leap_count += leap
        exp_year = {
            "schema_version": defs["schema_version"], "calendar_id": defs["calendar_id"],
            "calendar_version": defs["calendar_version"], "language": LANGUAGE, "year": y,
            "era_year": y - ERA_OFFSET,
            "year_phrase": f"в лето {y - ERA_OFFSET}-е от ладожского начала",
            "is_leap": leap, "days": 366 if leap else 365,
            "holiday_dates": {h["id"]: dt.date(y, h["month"], h["day"]).isoformat() for h in REFERENCE},
        }
        for field, value in exp_year.items():
            if not same(yd.get(field), value):
                errors.append(f"{y}: {field} is wrong")

        first = dt.date(y, 1, 1)
        keys = [(first + dt.timedelta(i)).isoformat() for i in range(exp_year["days"])]
        dates = yd.get("dates", {})
        if list(dates) != keys:
            errors.append(f"{y}: dates are not a continuous ordered year")
        check_record_lines(path.read_text(encoding="utf-8"), y, dates, errors)
        for key in keys:
            record = dates.get(key)
            if record is None:
                continue
            all_dates[key] = record
            if list(record) != DAILY_FIELDS:
                errors.append(f"{key}: fields are {list(record)}")
            exp = expected_record(dt.date.fromisoformat(key))
            for field, value in exp.items():
                if not same(record.get(field), value):
                    mismatches[field] += 1
                    if mismatches[field] <= 3:
                        errors.append(f"{key}.{field}: got {record.get(field)!r}, expected {value!r}")
            max_offset = max(max_offset, abs(exp["offset_days"]))
    for field, count in mismatches.items():
        errors.append(f"{field}: {count} mismatching records")

    exp_range = {"start": f"{start}-01-01", "end": f"{end}-12-31", "inclusive": True,
                 "year_count": len(years), "day_count": len(all_dates), "leap_year_count": leap_count}
    if not same(index.get("range"), exp_range):
        errors.append(f"index: range is {index.get('range')}, expected {exp_range}")

    for key, (ey, aid, adate, off) in CONTROL_CASES.items():
        r = all_dates.get(key)
        if r is None:
            errors.append(f"control case {key}: no record (build range is {start}-{end})")
        elif (r["era_year"], r["anchor_id"], r["anchor_date"], r["offset_days"]) != (ey, aid, adate, off):
            errors.append(f"control case {key} failed: {r}")
    for key, (text, short) in CONTROL_TEXTS.items():
        r = all_dates.get(key)
        if r is None:
            errors.append(f"control text {key}: no record (build range is {start}-{end})")
        elif (r["text"], r["short"]) != (text, short):
            errors.append(f"control text {key} failed: {r}")
    # Non-existent date and the days just outside the range.
    for key in ["2026-02-29", f"{start - 1}-12-31", f"{end + 1}-01-01"]:
        if key in all_dates:
            errors.append(f"{key} must not exist")

    return {"years": len(years), "dates": len(all_dates), "leap_years": leap_count,
            "holidays": len(defs["holidays"]), "max_abs_offset": max_offset}


def main():
    errors = []
    try:
        defs = load(DEFINITIONS)
        check_definitions(defs, errors)
        summary = check_data(defs, errors)
    except Exception as exc:  # report any failure as a readable line, not a traceback
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1
    for k, v in summary.items():
        print(f"{k}: {v}")
    if errors:
        print(f"FAILED: {len(errors)} problem(s)")
        for e in errors[:50]:
            print("  " + e)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
