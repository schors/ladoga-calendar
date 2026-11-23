#!/usr/bin/env python3
"""Independent validator for the Ladoga calendar JSON.

Recomputes every daily record from the holiday definitions and the rules
described in AGENTS.md, then compares the result with the stored data.
It never uses stored daily records as input for the computation.

Usage: python3 scripts/check_calendar.py [path/to/calendar.json]
Exit code: 0 if all checks pass, 1 otherwise.
"""
import calendar
import collections
import datetime as dt
import json
import sys
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "ladoga-calendar-2025-2038.json"
ERA_OFFSET = 752

# Number words used in day phrases; current data never exceeds 12 days.
ORDINAL = {2: "второй", 3: "третий", 4: "четвёртый", 5: "пятый", 6: "шестой",
           7: "седьмой", 8: "восьмой", 9: "девятый", 10: "десятый",
           11: "одиннадцатый", 12: "двенадцатый"}
CARDINAL = {2: "два", 3: "три", 4: "четыре", 5: "пять", 6: "шесть", 7: "семь",
            8: "восемь", 9: "девять", 10: "десять", 11: "одиннадцать",
            12: "двенадцать"}

# Control cases from AGENTS.md: date -> (ladoga_year, anchor_id, anchor_date, offset).
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
# Non-existent date and the days just outside the range.
MISSING_DATES = ["2026-02-29", "2024-12-31", "2039-01-01"]


def reject_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    dups = [k for k, c in collections.Counter(keys).items() if c > 1]
    if dups:
        raise ValueError(f"duplicate JSON keys: {dups[:5]}")
    return dict(pairs)


def day_plural(n):
    if 11 <= n % 100 <= 14:
        return "дней"
    return {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(n % 10, "дней")


def expected_record(target, holidays):
    # Candidates come from the previous, current and next year.
    candidates = []
    for year in (target.year - 1, target.year, target.year + 1):
        for order, h in enumerate(holidays):
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
        n = -offset
        day_text = f"За {CARDINAL[n]} {day_plural(n)} до {gen}"
    else:
        day_text = f"На {ORDINAL[offset]} день после {gen}"

    ladoga_year = target.year - ERA_OFFSET
    short = f"{ladoga_year} · {h['short_name']}" + ("" if offset == 0 else f" {offset:+d}")
    return {
        "ladoga_year": ladoga_year,
        "anchor_id": h["id"],
        "anchor_date": anchor.isoformat(),
        "anchor_year": anchor.year,
        "offset_days": offset,
        "day_text": day_text,
        "text": f"{day_text}, в лето {ladoga_year}-е от ладожского начала.",
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


def check(data):
    errors = []
    holidays = data["holidays"]

    # Identifiers and sources.
    ids = [h["id"] for h in holidays]
    if len(ids) != len(set(ids)):
        errors.append("holiday ids are not unique")
    source_ids = [s["id"] for s in data["sources"]]
    if len(source_ids) != len(set(source_ids)):
        errors.append("source ids are not unique")
    referenced = []
    collect_source_ids({k: v for k, v in data.items() if k != "dates"}, referenced)
    for sid in sorted(set(referenced) - set(source_ids)):
        errors.append(f"unresolved source_id: {sid}")
    unused = sorted(set(source_ids) - set(referenced))

    # Range and daily records.
    rng = data["range"]
    start = dt.date.fromisoformat(rng["start"])
    end = dt.date.fromisoformat(rng["end"])
    expected_keys = [(start + dt.timedelta(i)).isoformat() for i in range((end - start).days + 1)]
    dates = data["dates"]
    if list(dates) != expected_keys:
        errors.append("dates keys are not a continuous ordered range")

    mismatches = collections.Counter()
    max_offset = 0
    for key in expected_keys:
        record = dates.get(key)
        if record is None:
            errors.append(f"missing date {key}")
            continue
        exp = expected_record(dt.date.fromisoformat(key), holidays)
        if set(record) != set(exp):
            errors.append(f"{key}: unexpected field set {sorted(record)}")
        for field, value in exp.items():
            got = record.get(field)
            # Compare types too, so 3 and 3.0 or "3" are not accepted.
            if got != value or type(got) is not type(value):
                mismatches[field] += 1
                if mismatches[field] <= 3:
                    errors.append(f"{key}.{field}: got {got!r}, expected {value!r}")
        max_offset = max(max_offset, abs(exp["offset_days"]))
    for field, count in mismatches.items():
        errors.append(f"{field}: {count} mismatching records")

    # Years.
    years = data["years"]
    expected_years = [str(y) for y in range(start.year, end.year + 1)]
    if list(years) != expected_years:
        errors.append("years keys do not match the range")
    leap_count = 0
    for ykey, info in years.items():
        y = int(ykey)
        leap = calendar.isleap(y)
        leap_count += leap
        exp = {
            "ladoga_year": y - ERA_OFFSET,
            "year_phrase": f"в лето {y - ERA_OFFSET}-е от ладожского начала",
            "is_leap": leap,
            "days": 366 if leap else 365,
            "holiday_dates": {h["id"]: dt.date(y, h["month"], h["day"]).isoformat() for h in holidays},
        }
        for field, value in exp.items():
            if info.get(field) != value or type(info.get(field)) is not type(value):
                errors.append(f"years[{ykey}].{field} is wrong")

    # Range metadata.
    if rng.get("year_count") != len(expected_years):
        errors.append("range.year_count is wrong")
    if rng.get("day_count") != len(expected_keys):
        errors.append("range.day_count is wrong")
    if rng.get("leap_year_count") != leap_count:
        errors.append("range.leap_year_count is wrong")

    # Examples must equal the main index.
    for key, value in data["examples"].items():
        if dates.get(key) != value:
            errors.append(f"examples[{key}] differs from dates[{key}]")

    # Control cases.
    for key, (ly, aid, adate, off) in CONTROL_CASES.items():
        r = dates.get(key)
        if r is None or (r["ladoga_year"], r["anchor_id"], r["anchor_date"], r["offset_days"]) != (ly, aid, adate, off):
            errors.append(f"control case {key} failed: {r}")
    for key in MISSING_DATES:
        if key in dates:
            errors.append(f"{key} must not exist")

    summary = {
        "years": len(years),
        "dates": len(dates),
        "leap_years": leap_count,
        "holidays": len(holidays),
        "max_abs_offset": max_offset,
        "unused_sources": unused,
    }
    return errors, summary


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f, object_pairs_hook=reject_duplicate_keys)
        errors, summary = check(data)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"FAILED: {exc!r}")
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
