#!/usr/bin/env python3
"""Build the Ladoga calendar data files from ladoga-definitions.json.

Writes, per language, one file per civil year and a holidays file, plus an index:
    data/index.json
    data/<language>/holidays.json
    data/<language>/<YYYY>.json

The output is deterministic: the same definitions always give byte-identical
files. Nothing is written until the whole output has been built in memory.
Files are removed only inside data/<language>/ for the languages being built.

Usage:
    python3 scripts/build_calendar.py           # (re)write the data files
    python3 scripts/build_calendar.py --check   # compare files on disk, write nothing
Exit code: 0 on success, 1 if --check finds differences or the input is invalid.
"""
import argparse
import calendar
import collections
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

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

    for n, entry in fmt["numbers"].items():
        if not (n.isdigit() and int(n) >= 2):
            raise DefinitionError(f"formatting.numbers key {n!r} must be a number from 2 up")
        for field in NUMBER_FIELDS:
            if not isinstance(entry.get(field), str):
                raise DefinitionError(f"formatting.numbers[{n}].{field} must be a string")
    output_dir(defs)


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
    number = fmt["numbers"].get(str(abs(offset)))
    if number is None:
        raise DefinitionError(f"formatting.numbers has no entry for {abs(offset)}")
    kind = "before" if offset < 0 else "after"
    return fill(phrases[kind], {**names, **{f: number[f] for f in NUMBER_FIELDS}})


def build_year(year, defs):
    era, fmt, holidays = defs["era"], defs["formatting"], defs["holidays"]
    era_year = year + era["year_offset"]
    year_phrase = fill(era["year_phrase_template"], {"era_year": str(era_year)})
    suffix = fmt["offset_suffix"]

    dates = {}
    day = dt.date(year, 1, 1)
    while day.year == year:
        anchor, h, offset = nearest_anchor(day, holidays, fmt["candidate_year_offsets"])
        text_day = day_text(h, offset, fmt)
        offset_suffix = suffix["zero"] if offset == 0 else fill(suffix["nonzero"], {"signed_offset": f"{offset:+d}"})
        dates[day.isoformat()] = {
            "era_year": era_year,
            "anchor_id": h["id"],
            "anchor_date": anchor.isoformat(),
            "anchor_year": anchor.year,
            "offset_days": offset,
            "day_text": text_day,
            "text": fill(fmt["full_text_template"], {"day_text": text_day, "year_phrase": year_phrase}),
            "short": fill(fmt["short_template"], {"era_year": str(era_year), "short_name": h["short_name"],
                                                  "offset_suffix": offset_suffix}),
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
        "usage": {
            "paths": "Все пути в этом файле записаны относительно папки, где он лежит.",
            "year_file": "files[language][YYYY] — файл года",
            "holidays_file": "holidays[language] — названия и описания праздников по anchor_id",
            "ready_date": "dates[YYYY-MM-DD].text",
            "day_only": "dates[YYYY-MM-DD].day_text",
            "short_date": "dates[YYYY-MM-DD].short",
            "year_number": "era_year — номер ладожского лета",
            "year_phrase": "year_phrase",
            "holiday_occurrence": "holiday_dates[holiday_id]",
            "outside_range": "Если года нет в files, дата вне диапазона. Если в файле года нет даты, такой даты не существует. Нельзя молча подставлять другой год.",
            "editing": "Файлы в этой папке создаются скриптом scripts/build_calendar.py из ladoga-definitions.json. Вручную их не правят.",
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


def build_all(defs):
    """Return {path relative to output dir: file text}."""
    b = defs["build"]
    lang = defs["language"]
    years = [build_year(y, defs) for y in range(b["start_year"], b["end_year"] + 1)]
    out = {"index.json": serialize(build_index(defs, years)),
           f"{lang}/holidays.json": serialize(build_holidays(defs))}
    for y in years:
        out[f"{lang}/{y['year']}.json"] = serialize(y)
    return out


def managed_files(out_dir, languages):
    """Files this generator owns: index.json and everything inside the language folders."""
    found = set()
    if (out_dir / "index.json").exists():
        found.add("index.json")
    for lang in languages:
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
        files = build_all(defs)
    except (OSError, ValueError, KeyError, TypeError, DefinitionError) as exc:
        print(f"FAILED: {DEFINITIONS.name}: {exc}")
        return 1

    existing = managed_files(out_dir, [defs["language"]])
    stale = sorted(existing - set(files))

    if args.check:
        problems = [f"missing: {p}" for p in sorted(set(files) - existing)]
        problems += [f"unexpected: {p}" for p in stale]
        problems += [f"differs: {p}" for p in sorted(set(files) & existing)
                     if (out_dir / p).read_text(encoding="utf-8") != files[p]]
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
