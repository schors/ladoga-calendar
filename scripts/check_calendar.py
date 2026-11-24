#!/usr/bin/env python3
"""Independent validator for the Ladoga calendar data.

Checks ladoga-definitions.json and the generated files in data/.
The validator does not use scripts/build_calendar.py and does not trust the
definitions: it has its own reference table of holidays (dates and name forms),
its own Russian number words and its own phrase rules. The definitions are
compared with these references, and every daily record is recomputed from them.

For the time of day it has its own table of poras and name forms, its own
low-precision solar formula (Meeus / NOAA style) that every sunrise, solar noon
and sunset in data/sun/ must match within SUN_TOLERANCE_S, its own computation
of the pora changes in data/time/, and control cases for scripts/ladoga_time.py.

Usage: python3 scripts/check_calendar.py
Exit code: 0 if all checks pass, 1 otherwise.
"""
import calendar
import collections
import contextlib
import datetime as dt
import hashlib
import importlib.util
import io
import json
import math
import re
import sys
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

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
    ("midwinter", 2, 2, "Перелом зимы", "Перелом зимы", "Перелома зимы", "В день Перелома зимы"),
    ("gulf_ice", 2, 13, "Ледяной залив", "Ледяной залив", "Ледяного залива", "В день Ледяного залива"),
    ("veles", 2, 24, "Волосов день", "Волос", "Волосова дня", "В Волосов день"),
    ("spring_turn", 3, 20, "Равноденье", "Равноденье", "Равноденья", "На Равноденье"),
    ("free_water", 4, 13, "День вольной воды", "Вольная вода", "Вольной воды", "В день Вольной воды"),
    ("smelt", 4, 26, "Корюшкин день", "Корюшка", "Корюшкина дня", "В Корюшкин день"),
    ("spring_day", 5, 6, "Вешний день", "Вешний день", "Вешнего дня", "В Вешний день"),
    ("boats", 5, 24, "Лодейный день", "Лодейный день", "Лодейного дня", "В Лодейный день"),
    ("white_nights", 6, 16, "Светлые ночи", "Светлые ночи", "Светлых ночей", "На Светлые ночи"),
    ("kupala", 7, 7, "Купала", "Купала", "Купалы", "На Купалу"),
    ("lake_wind", 7, 20, "День ладожского ветра", "Ладожский ветер", "Ладожского ветра", "В день Ладожского ветра"),
    ("perun", 8, 2, "Перунов день", "Перун", "Перунова дня", "В Перунов день"),
    ("hot_rocks", 8, 10, "Горячие скалы", "Горячие скалы", "Горячих скал", "В день Горячих скал"),
    ("first_fruits", 8, 19, "Первые плоды", "Первые плоды", "Первых плодов", "В праздник Первых плодов"),
    ("rowan_fire", 9, 5, "Рябиновый огонь", "Рябиновый огонь", "Рябинового огня", "В день Рябинового огня"),
    ("lamprey_night", 9, 14, "Миножья ночь", "Миножья ночь", "Миножьей ночи", "На Миножью ночь"),
    ("oseniny", 9, 21, "Осенины", "Осенины", "Осенин", "На Осенины"),
    ("west_wind", 10, 3, "День западного ветра", "Западный ветер", "Западного ветра", "В день Западного ветра"),
    ("horse_stone", 10, 9, "Конь-камень", "Конь-камень", "Конь-камня", "В день Конь-камня"),
    ("earth_closing", 10, 14, "Затвор земли", "Затвор земли", "Затвора земли", "В день Затвора земли"),
    ("last_boat", 10, 24, "Последняя сойма", "Последняя сойма", "Последней соймы", "В день Последней соймы"),
    ("ancestors", 11, 7, "Осенние Деды", "Деды", "Осенних Дедов", "На Осенние Деды"),
    ("dark_water", 11, 21, "Тёмная вода", "Тёмная вода", "Тёмной воды", "В день Тёмной воды"),
    ("vainamoinen", 12, 7, "Вечер Вяйнемяйнена", "Вяйнемяйнен", "Вечера Вяйнемяйнена", "На Вечер Вяйнемяйнена"),
    ("kolyada", 12, 21, "Коляда", "Коляда", "Коляды", "На Коляду"),
]
REFERENCE_FIELDS = ["id", "month", "day", "name", "short_name", "name_genitive", "on_day_phrase"]
REFERENCE = [dict(zip(REFERENCE_FIELDS, row)) for row in REFERENCE_HOLIDAYS]
# Holidays that are a moment or whose names already hold "day", "night" or
# "evening": the days after them read "two days after X" (plain count).
# The others last, and their days read "the third day of X" (folk count).
COUNT_AFTER_HOLIDAYS = {"veles", "smelt", "spring_day", "boats", "perun", "lamprey_night",
                        "vainamoinen", "spring_turn", "midwinter", "horse_stone", "last_boat"}

# Russian reference number words, written independently of the definitions.
ORDINAL = {2: "второй", 3: "третий", 4: "четвёртый", 5: "пятый", 6: "шестой",
           7: "седьмой", 8: "восьмой", 9: "девятый", 10: "десятый",
           11: "одиннадцатый", 12: "двенадцатый", 13: "тринадцатый"}
CARDINAL = {2: "два", 3: "три", 4: "четыре", 5: "пять", 6: "шесть", 7: "семь",
            8: "восемь", 9: "девять", 10: "десять", 11: "одиннадцать",
            12: "двенадцать", 13: "тринадцать"}

# Rule values the reference algorithm below implements.
RULES = {"anchor_selection": "nearest", "tie_breaker": "past_then_holiday_order",
         "candidate_year_offsets": [-1, 0, 1]}

# Control cases from AGENTS.md: date -> (era_year, anchor_id, anchor_date, offset).
CONTROL_CASES = {
    "2025-01-01": (1273, "kolyada", "2024-12-21", 11),
    "2026-03-08": (1274, "veles", "2026-02-24", 12),
    "2026-10-06": (1274, "west_wind", "2026-10-03", 3),
    "2026-11-23": (1274, "dark_water", "2026-11-21", 2),
    "2026-12-10": (1274, "vainamoinen", "2026-12-07", 3),
    "2027-01-01": (1275, "kolyada", "2026-12-21", 11),
    "2028-02-29": (1276, "veles", "2028-02-24", 5),
    "2028-03-08": (1276, "spring_turn", "2028-03-20", -12),
    "2038-12-31": (1286, "kolyada", "2038-12-21", 10),
}
# Full expected strings for a few dates.
CONTROL_TEXTS = {
    "2026-11-21": ("В день Тёмной воды.", "Тёмная вода · лето 1274"),
    "2026-12-10": ("Через три дня после Вечера Вяйнемяйнена.", "Через 3 дн. после Вечера Вяйнемяйнена · лето 1274"),
    "2028-03-08": ("За двенадцать дней до Равноденья.", "За 12 дн. до Равноденья · лето 1276"),
    "2026-02-18": ("В шестой день Ледяного залива.", "6-й д. Ледяного залива · лето 1274"),
    "2026-11-23": ("В третий день Тёмной воды.", "3-й д. Тёмной воды · лето 1274"),
    "2026-11-22": ("На другой день после Тёмной воды.", "2-й д. Тёмной воды · лето 1274"),
    "2026-03-08": ("Через двенадцать дней после Волосова дня.", "Через 12 дн. после Волосова дня · лето 1274"),
    "2026-06-24": ("В девятый день Светлых ночей.", "9-й д. Светлых ночей · лето 1274"),
    "2026-03-30": ("Через десять дней после Равноденья.", "Через 10 дн. после Равноденья · лето 1274"),
    "2026-11-05": ("За два дня до Осенних Дедов.", "За 2 дн. до Осенних Дедов · лето 1274"),
    "2026-08-10": ("В день Горячих скал.", "Горячие скалы · лето 1274"),
    "2026-09-10": ("За четыре дня до Миножьей ночи.", "За 4 дн. до Миножьей ночи · лето 1274"),
    "2026-09-14": ("На Миножью ночь.", "Миножья ночь · лето 1274"),
    "2026-10-02": ("В канун Западного ветра.", "Канун Западного ветра · лето 1274"),
    "2026-10-10": ("На другой день после Конь-камня.", "Назавтра после Конь-камня · лето 1274"),
}

DAILY_FIELDS = ["era_year", "anchor_id", "anchor_date", "anchor_year", "offset_days",
                "day_text", "text", "full_text", "short"]
YEAR_FIELDS = ["schema_version", "calendar_id", "calendar_version", "language", "year",
               "era_year", "year_phrase", "is_leap", "days", "holiday_dates", "dates"]
INDEX_FIELDS = ["schema_version", "calendar_id", "calendar_version", "name", "definitions",
                "timezone", "encoding", "range", "languages", "default_language", "holidays",
                "files", "time_model", "time_names", "time", "sun", "usage"]
USAGE_KEYS = ["paths", "year_file", "holidays_file", "ready_date", "full_date", "day_only", "short_date",
              "year_number", "year_phrase", "holiday_occurrence", "time_file", "current_pora",
              "pora_names", "time_phrase", "sun_file", "outside_range", "editing"]
HOLIDAYS_FILE_FIELDS = ["schema_version", "calendar_id", "calendar_version", "language", "note", "holidays"]
PUBLISHED_HOLIDAY_FIELDS = ["id", "month", "day", "name", "short_name", "name_genitive",
                            "on_day_phrase", "aliases", "meaning", "suggested_custom",
                            "historical_note"]
SHARED_FIELDS = ["schema_version", "calendar_id", "calendar_version"]

# --- Time of day references ---
SUN_DIR, TIME_DIR = "sun", "time"
# (id, period, first_hour, short_name, name, name_genitive, phrase)
REFERENCE_PORAS = [
    ("morning", "day", 1, "Утро", "Утро на Неве", "Утра на Неве", "утром на Неве", "утром"),
    ("forenoon", "day", 4, "Высокое солнце", "Высокое солнце над Ладогой", "Высокого солнца над Ладогой", "при высоком солнце над Ладогой", "при высоком солнце"),
    ("afternoon", "day", 7, "Паужна", "Паужна на островах", "Паужны на островах", "в паужну на островах", "в паужну"),
    ("before_sunset", "day", 10, "Низкое солнце", "Низкое солнце над заливом", "Низкого солнца над заливом", "при низком солнце над заливом", "при низком солнце"),
    ("dusk", "night", 1, "Сумерки", "Сумерки у рыбацких костров", "Сумерек у рыбацких костров", "в сумерки у рыбацких костров", "в сумерки"),
    ("before_midnight", "night", 4, "Первый сон", "Первый сон в шхерах", "Первого сна в шхерах", "в первый сон в шхерах", "в первый сон"),
    ("after_midnight", "night", 7, "Глухая пора", "Глухая пора на Вуоксе", "Глухой поры на Вуоксе", "в глухую пору на Вуоксе", "в глухую пору"),
    ("predawn", "night", 10, "Заря", "Заря за Свирью", "Зари за Свирью", "на заре за Свирью", "на заре"),
]
PORA_FIELDS = ["id", "period", "first_hour", "short_name", "name", "name_genitive", "phrase", "short_phrase"]
PORAS = [dict(zip(PORA_FIELDS, row)) for row in REFERENCE_PORAS]
REFERENCE_LOCATION = {"id": "neva_source", "latitude_deg": 59.95, "longitude_deg": 31.04, "elevation_m": 0.0}
SUN_ALTITUDE_DEG = -50 / 60
# Europe/Moscow has been UTC+3 all year since 2014-10-26.
REFERENCE_UTC_OFFSET = 3 * 3600
# Observed maximum difference is about 6 s; 20 s still catches a wrong horizon of about 2'.
SUN_TOLERANCE_S = 20
REFERENCE_TIME_TEMPLATES = {
    "seasonal": "{hour}-й {period_adjective} час",
    "seasonal_short": "{period_letter}{hour_2d}",
    "relative_before": "За {duration} до {event_genitive}",
    "relative_after": "Через {duration} после {event_genitive}",
    "relative_soon_before": "Незадолго до {event_genitive}",
    "relative_soon_after": "Вскоре после {event_genitive}",
    "relative_near": "{near_phrase}",
    "full_text": "{day_text}, {year_phrase}, {pora_short_phrase}.",
}
REFERENCE_CLOCK = {"day_hours": 12, "night_hours": 12, "hours_per_pora": 3,
                   "interval_bounds": "start_included_end_excluded",
                   "local_boundary_rounding": "up_to_whole_second"}
REFERENCE_RELATIVE = {"events": ["sunrise", "solar_noon", "sunset"], "candidate_day_offsets": [-1, 0, 1],
                      "selection": "nearest", "tie_breaker": "past", "display_unit": "hour",
                      "rounding": "nearest_hour_half_up", "near_minutes": 15, "soon_minutes": 60}
REFERENCE_TIME_NAMES = {
    "periods": {"day": {"name": "день", "adjective": "дневной", "letter": "Д"},
                "night": {"name": "ночь", "adjective": "ночной", "letter": "Н"}},
    "event_names": {"sunrise": {"name": "восход", "genitive": "восхода", "near_phrase": "На восходе"},
                    "solar_noon": {"name": "полдень", "genitive": "полудня", "near_phrase": "В полдень"},
                    "sunset": {"name": "закат", "genitive": "заката", "near_phrase": "На закате"}},
    "hour_words": {"1": "час", "2": "два часа", "3": "три часа", "4": "четыре часа", "5": "пять часов",
                   "6": "шесть часов", "7": "семь часов", "8": "восемь часов", "9": "девять часов",
                   "10": "десять часов", "11": "одиннадцать часов", "12": "двенадцать часов"},
}
SUN_EVENTS = ["sunrise", "solar_noon", "sunset"]
SUN_FIELDS = ["schema_version", "calendar_id", "model_id", "sun_data_revision", "year", "reference_location",
              "timezone", "tzdb_version", "tzif_sha256", "ephemeris_engine", "solar_events", "days",
              "edge_events", "dates"]
TIME_FIELDS = ["schema_version", "calendar_id", "calendar_version", "model_id", "sun_data_revision", "year",
               "timezone", "utc_offset", "days", "dates"]
TIME_RECORD_FIELDS = SUN_EVENTS + [f"{e}_local" for e in SUN_EVENTS] + ["poras"]
TIME_NAMES_FIELDS = ["schema_version", "calendar_id", "calendar_version", "language", "model_id", "note",
                     "reference_location", "poras", "periods", "event_names", "hour_words", "templates",
                     "templates_note"]
PUBLISHED_PORA_FIELDS = ["id", "period", "first_hour", "last_hour", "short_name", "name", "name_genitive",
                         "phrase", "short_phrase", "meaning"]


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
        day_text = f"На другой день после {gen}"
    elif offset < 0:
        cardinal, days_word, _ = number_words(-offset)
        day_text = f"За {cardinal} {days_word} до {gen}"
    elif h["id"] in COUNT_AFTER_HOLIDAYS:
        cardinal, days_word, _ = number_words(offset)
        day_text = f"Через {cardinal} {days_word} после {gen}"
    else:
        # Folk counting: the holiday itself is the first day.
        _, _, ordinal = number_words(offset + 1)
        day_text = f"В {ordinal} день {gen}"

    era_year = target.year - ERA_OFFSET
    if offset == 0:
        short = h["name"]
    elif offset == -1:
        short = f"Канун {gen}"
    elif offset < 0:
        short = f"За {-offset} дн. до {gen}"
    elif h["id"] not in COUNT_AFTER_HOLIDAYS:
        short = f"{offset + 1}-й д. {gen}"
    elif offset == 1:
        short = f"Назавтра после {gen}"
    else:
        short = f"Через {offset} дн. после {gen}"
    short = f"{short} · лето {era_year}"
    return {
        "era_year": era_year,
        "anchor_id": h["id"],
        "anchor_date": anchor.isoformat(),
        "anchor_year": anchor.year,
        "offset_days": offset,
        "day_text": day_text,
        "text": f"{day_text}.",
        "full_text": f"{day_text}, в лето {era_year}-е.",
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
        kind = "after" if ref["id"] in COUNT_AFTER_HOLIDAYS else "own_day"
        if h.get("after_kind") != kind:
            errors.append(f"definitions: {ref['id']}.after_kind is {h.get('after_kind')!r}, reference {kind!r}")

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
    expected_time = {str(y): f"{TIME_DIR}/{y}.json" for y in years}
    expected_sun = {str(y): f"{SUN_DIR}/{y}.json" for y in years}
    time_model = {"model_id": defs["time"]["model_id"], "sun_data_revision": defs["time"]["sun_data_revision"],
                  "reference_location": REFERENCE_LOCATION["id"]}
    for field, value in [("definitions", f"../{DEFINITIONS.name}"), ("languages", [LANGUAGE]),
                         ("default_language", LANGUAGE), ("holidays", expected_holidays),
                         ("files", expected_files), ("time_model", time_model),
                         ("time_names", {LANGUAGE: f"{LANGUAGE}/time.json"}),
                         ("time", expected_time), ("sun", expected_sun)]:
        if not same(index.get(field), value):
            errors.append(f"index: {field} is {index.get(field)!r}, expected {value!r}")
    usage = index.get("usage")
    if not (isinstance(usage, dict) and list(usage) == USAGE_KEYS
            and all(isinstance(v, str) and v for v in usage.values())):
        errors.append("index: usage must hold non-empty texts for " + ", ".join(USAGE_KEYS))

    on_disk = {p.relative_to(DATA_DIR).as_posix() for p in DATA_DIR.rglob("*") if p.is_file()}
    expected_disk = ({"index.json", expected_holidays[LANGUAGE], f"{LANGUAGE}/time.json"}
                     | set(expected_files[LANGUAGE].values()) | set(expected_time.values())
                     | set(expected_sun.values()))
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
            "year_phrase": f"в лето {y - ERA_OFFSET}-е",
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


# ---------------------------------------------------------------------------
# Time of day


def sun_position(unix):
    """Low-precision solar declination, right ascension and sidereal time, degrees (Meeus ch. 25).

    For simplicity UT stands in for TT and mean sidereal time is used with apparent
    right ascension; together these cost about a second, well inside the tolerance.
    """
    jd = unix / 86400 + 2440587.5
    t = (jd - 2451545) / 36525
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    m = math.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))
    c = ((1.914602 - t * (0.004817 + 0.000014 * t)) * math.sin(m)
         + (0.019993 - 0.000101 * t) * math.sin(2 * m) + 0.000289 * math.sin(3 * m))
    omega = math.radians(125.04 - 1934.136 * t)
    lam = math.radians(l0 + c - 0.00569 - 0.00478 * math.sin(omega))
    eps = math.radians(23.439291 - 0.0130042 * t + 0.00256 * math.cos(omega))
    dec = math.degrees(math.asin(math.sin(eps) * math.sin(lam)))
    ra = math.degrees(math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam)))
    gmst = (280.46061837 + 360.98564736629 * (jd - 2451545)) % 360
    return dec, ra, gmst


def hour_angle(unix, lon):
    dec, ra, gmst = sun_position(unix)
    return (gmst + lon - ra + 180) % 360 - 180, dec


def reference_sun_events(day, offset):
    """Sunrise, solar noon and sunset of a civil date as Unix seconds (float), by Newton steps on the hour angle."""
    lat, lon = math.radians(REFERENCE_LOCATION["latitude_deg"]), REFERENCE_LOCATION["longitude_deg"]
    midnight = (day - dt.date(1970, 1, 1)).days * 86400 - offset
    rate = 360.98564736629 / 86400  # degrees of hour angle per second, close enough for Newton steps
    noon = midnight + 43200 - lon * 240 + offset
    for _ in range(5):
        noon -= hour_angle(noon, lon)[0] / rate
    out = {"solar_noon": noon}
    for name, sign in (("sunrise", -1), ("sunset", 1)):
        t = noon
        for _ in range(8):
            h, dec = hour_angle(t, lon)
            d = math.radians(dec)
            cos_h0 = ((math.sin(math.radians(SUN_ALTITUDE_DEG)) - math.sin(lat) * math.sin(d))
                      / (math.cos(lat) * math.cos(d)))
            t += (sign * math.degrees(math.acos(cos_h0)) - h) / rate
        out[name] = t
    return out


def reference_pora_changes(day, events, offset):
    """Pora changes on `day` as local HH:MM:SS, the first one at 00:00 (independent of the generator)."""
    prev, cur, nxt = (events[day + dt.timedelta(days=d)] for d in (-1, 0, 1))
    spans = [(prev["sunset"], cur["sunrise"], "night"), (cur["sunrise"], cur["sunset"], "day"),
             (cur["sunset"], nxt["sunrise"], "night")]
    marks = []
    for start, end, period in spans:
        ids = [p["id"] for p in PORAS if p["period"] == period]
        for k in range(4):
            marks.append((math.ceil(start + Fraction(k * (end - start), 4)), ids[k]))
    midnight = (day - dt.date(1970, 1, 1)).days * 86400 - offset
    at_midnight = max((m for m in marks if m[0] <= midnight), key=lambda m: m[0])[1]
    out = [(midnight, at_midnight)] + [m for m in marks if midnight < m[0] < midnight + 86400]

    def hms(sec):
        return f"{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}"
    return [[hms(m - midnight), pid] for m, pid in out]


def parse_z(text):
    if not isinstance(text, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text):
        raise CheckError(f"{text!r} is not a YYYY-MM-DDTHH:MM:SSZ timestamp")
    return int(dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp())


def check_time_definitions(defs, errors):
    t = defs["time"]
    poras = t["poras"]
    if [p.get("id") for p in poras] != [p["id"] for p in PORAS]:
        errors.append(f"definitions: time.poras ids or order differ from the reference: {[p.get('id') for p in poras]}")
    for p, ref in zip(poras, PORAS):
        for field in PORA_FIELDS:
            if not same(p.get(field), ref[field]):
                errors.append(f"definitions: time pora {ref['id']}.{field} is {p.get(field)!r}, reference {ref[field]!r}")
        if p.get("last_hour") != ref["first_hour"] + 2 or not (isinstance(p.get("meaning"), str) and p["meaning"]):
            errors.append(f"definitions: time pora {ref['id']} has a wrong last_hour or no meaning")
    loc = t["reference_location"]
    if any(not same(loc.get(k), v) for k, v in REFERENCE_LOCATION.items()):
        errors.append(f"definitions: time.reference_location is {loc}, reference {REFERENCE_LOCATION}")
    if not same(t["solar_events"].get("sunrise_set_altitude_deg"), SUN_ALTITUDE_DEG):
        errors.append("definitions: time.solar_events.sunrise_set_altitude_deg is not -50/60")
    for key, value in REFERENCE_TIME_NAMES.items():
        if not same(t.get(key), value):
            errors.append(f"definitions: time.{key} differs from the reference")
    for key, value in (("clock", REFERENCE_CLOCK), ("relative", REFERENCE_RELATIVE),
                       ("templates", REFERENCE_TIME_TEMPLATES)):
        if not same(t.get(key), value):
            errors.append(f"definitions: time.{key} differs from the reference {value}")
    for key, what in (("ephemeris_engine", "ephemeris engine"), ("timezone_file", "time zone file")):
        path = ROOT / t[key]["path"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != t[key]["sha256"]:
            errors.append(f"definitions: {what} {t[key]['path']} is missing or does not match its SHA-256")
    if defs["timezone"] != "Europe/Moscow":
        errors.append("definitions: timezone must be Europe/Moscow")


def check_zone(defs, years, errors):
    """The pinned time zone file must give the reference offset on every day, with 24-hour days."""
    path = ROOT / defs["time"]["timezone_file"]["path"]
    with open(path, "rb") as f:
        zone = ZoneInfo.from_file(f, key=defs["timezone"])
    day = dt.date(years[0] - 1, 12, 31)
    while day <= dt.date(years[-1] + 1, 1, 1):
        start = dt.datetime(day.year, day.month, day.day, tzinfo=zone)
        noon = dt.datetime(day.year, day.month, day.day, 12, tzinfo=zone)
        if start.utcoffset().total_seconds() != REFERENCE_UTC_OFFSET or noon.utcoffset().total_seconds() != REFERENCE_UTC_OFFSET:
            errors.append(f"time zone: {day} is not UTC+3 in {path.relative_to(ROOT)}")
            return
        day += dt.timedelta(days=1)


def check_sun(defs, years, errors):
    """Check data/sun/ and return {date: {event: Unix seconds}} including the edge days."""
    t = defs["time"]
    events = {}
    max_diff = 0
    far = collections.Counter()
    for y in years:
        path = DATA_DIR / SUN_DIR / f"{y}.json"
        if not path.exists():
            errors.append(f"data: missing {path.relative_to(ROOT)}")
            continue
        sd = load(path)
        if list(sd) != SUN_FIELDS:
            errors.append(f"sun {y}: fields are {list(sd)}")
        exp = {"schema_version": defs["schema_version"], "calendar_id": defs["calendar_id"],
               "model_id": t["model_id"], "sun_data_revision": t["sun_data_revision"], "year": y,
               "reference_location": REFERENCE_LOCATION, "timezone": defs["timezone"],
               "tzdb_version": t["timezone_file"]["tzdb_version"], "tzif_sha256": t["timezone_file"]["sha256"],
               "ephemeris_engine": {k: t["ephemeris_engine"][k] for k in ("name", "version", "sha256", "delta_t")},
               "solar_events": t["solar_events"], "days": 366 if calendar.isleap(y) else 365}
        for field, value in exp.items():
            if not same(sd.get(field), value):
                errors.append(f"sun {y}: {field} is wrong")
        edges = {}
        if y == years[0]:
            edges[f"{y - 1}-12-31"] = None
        if y == years[-1]:
            edges[f"{y + 1}-01-01"] = None
        if list(sd.get("edge_events", {})) != list(edges):
            errors.append(f"sun {y}: edge_events must hold exactly {list(edges)}")
        first = dt.date(y, 1, 1)
        keys = [(first + dt.timedelta(i)).isoformat() for i in range(exp["days"])]
        dates = sd.get("dates", {})
        if list(dates) != keys:
            errors.append(f"sun {y}: dates are not a continuous ordered year")
        check_record_lines(path.read_text(encoding="utf-8"), f"sun {y}", dates, errors)
        for key, rec in list(sd.get("edge_events", {}).items()) + list(dates.items()):
            try:
                if list(rec) != SUN_EVENTS:
                    raise CheckError(f"fields are {list(rec)}")
                ev = {e: parse_z(rec[e]) for e in SUN_EVENTS}
            except CheckError as exc:
                errors.append(f"sun {key}: {exc}")
                continue
            day = dt.date.fromisoformat(key)
            midnight = (day - dt.date(1970, 1, 1)).days * 86400 - REFERENCE_UTC_OFFSET
            if not midnight <= ev["sunrise"] < ev["solar_noon"] < ev["sunset"] < midnight + 86400:
                errors.append(f"sun {key}: events are out of order or outside the civil date")
            ref = reference_sun_events(day, REFERENCE_UTC_OFFSET)
            for e in SUN_EVENTS:
                diff = abs(ev[e] - ref[e])
                max_diff = max(max_diff, diff)
                if diff > SUN_TOLERANCE_S:
                    far[e] += 1
                    if far[e] <= 3:
                        errors.append(f"sun {key}.{e}: {rec[e]} differs from the reference formula by {diff:.0f} s")
            events[day] = ev
    for e, n in far.items():
        errors.append(f"sun: {n} {e} values outside the {SUN_TOLERANCE_S} s tolerance")
    days = sorted(events)
    for a, b in zip(days, days[1:]):
        if (b - a).days != 1 or not events[a]["sunset"] < events[b]["sunrise"]:
            errors.append(f"sun: {a} -> {b} is not a continuous positive night")
    return events, max_diff


def check_time_files(defs, years, events, errors):
    t = defs["time"]
    for y in years:
        path = DATA_DIR / TIME_DIR / f"{y}.json"
        if not path.exists():
            errors.append(f"data: missing {path.relative_to(ROOT)}")
            continue
        td = load(path)
        if list(td) != TIME_FIELDS:
            errors.append(f"time {y}: fields are {list(td)}")
        exp = {"schema_version": defs["schema_version"], "calendar_id": defs["calendar_id"],
               "calendar_version": defs["calendar_version"], "model_id": t["model_id"],
               "sun_data_revision": t["sun_data_revision"], "year": y, "timezone": defs["timezone"],
               "utc_offset": "+03:00", "days": 366 if calendar.isleap(y) else 365}
        for field, value in exp.items():
            if not same(td.get(field), value):
                errors.append(f"time {y}: {field} is wrong")
        first = dt.date(y, 1, 1)
        keys = [(first + dt.timedelta(i)).isoformat() for i in range(exp["days"])]
        dates = td.get("dates", {})
        if list(dates) != keys:
            errors.append(f"time {y}: dates are not a continuous ordered year")
        check_record_lines(path.read_text(encoding="utf-8"), f"time {y}", dates, errors)
        bad = 0
        for key, rec in dates.items():
            day = dt.date.fromisoformat(key)
            if day not in events or day - dt.timedelta(1) not in events or day + dt.timedelta(1) not in events:
                continue
            ev = events[day]
            midnight = (day - dt.date(1970, 1, 1)).days * 86400 - REFERENCE_UTC_OFFSET
            exp_rec = {e: dt.datetime.fromtimestamp(ev[e], dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                       for e in SUN_EVENTS}
            for e in SUN_EVENTS:
                s = ev[e] - midnight
                exp_rec[f"{e}_local"] = f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"
            exp_rec["poras"] = reference_pora_changes(day, events, REFERENCE_UTC_OFFSET)
            if not same(rec, exp_rec):
                bad += 1
                if bad <= 3:
                    errors.append(f"time {key}: got {rec}, expected {exp_rec}")
        if bad:
            errors.append(f"time {y}: {bad} mismatching records")


def check_time_names(defs, errors):
    path = DATA_DIR / LANGUAGE / "time.json"
    tn = load(path)
    t = defs["time"]
    if list(tn) != TIME_NAMES_FIELDS:
        errors.append(f"time names: fields are {list(tn)}")
    exp = {"schema_version": defs["schema_version"], "calendar_id": defs["calendar_id"],
           "calendar_version": defs["calendar_version"], "language": LANGUAGE, "model_id": t["model_id"],
           "reference_location": {"id": REFERENCE_LOCATION["id"], "name": t["reference_location"]["name"]},
           "poras": [{f: p[f] for f in PUBLISHED_PORA_FIELDS} for p in t["poras"]],
           "periods": REFERENCE_TIME_NAMES["periods"], "event_names": REFERENCE_TIME_NAMES["event_names"],
           "hour_words": REFERENCE_TIME_NAMES["hour_words"], "templates": REFERENCE_TIME_TEMPLATES,
           "templates_note": t["templates_note"]}
    for field, value in exp.items():
        if not same(tn.get(field), value):
            errors.append(f"time names: {field} differs")
    if not (isinstance(tn.get("note"), str) and tn["note"]):
        errors.append("time names: note is empty")
    return tn


def load_formatter():
    path = ROOT / "scripts" / "ladoga_time.py"
    spec = importlib.util.spec_from_file_location("ladoga_time", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_formatter(names, errors):
    """Control cases for scripts/ladoga_time.py with made-up solar events (docs/ladoga-time.md, section 4)."""
    lt = load_formatter()
    msk = dt.timezone(dt.timedelta(seconds=REFERENCE_UTC_OFFSET))

    def ms(day, hh, mm, ss=0, milli=0):
        delta = dt.datetime(2026, 11, day, hh, mm, ss, tzinfo=msk) - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        return (delta.days * 86400 + delta.seconds) * 1000 + milli
    events = {dt.date(2026, 11, d): {"sunrise": ms(d, 9, 15), "solar_noon": ms(d, 12, 42), "sunset": ms(d, 16, 15)}
              for d in (22, 23, 24, 25)}
    # moment -> (pora id, seasonal hour, seasonal text, short, relative text)
    cases = [
        (ms(24, 12, 0), ("forenoon", 5, "5-й дневной час", "Д05", "Незадолго до полудня")),
        (ms(24, 1, 30), ("after_midnight", 7, "7-й ночной час", "Н07", "За восемь часов до восхода")),
        (ms(24, 16, 15), ("dusk", 1, "1-й ночной час", "Н01", "На закате")),
        (ms(24, 16, 14, 59, 999), ("before_sunset", 12, "12-й дневной час", "Д12", "На закате")),
        (ms(24, 16, 14, 40), ("before_sunset", 12, "12-й дневной час", "Д12", "На закате")),
        (ms(24, 9, 15), ("morning", 1, "1-й дневной час", "Д01", "На восходе")),
        (ms(24, 9, 14, 59, 999), ("predawn", 12, "12-й ночной час", "Н12", "На восходе")),
        # First moment of hour 4: a day of 7 h has 35-minute hours, so 09:15 + 105 min.
        (ms(24, 11, 0), ("forenoon", 4, "4-й дневной час", "Д04", "За два часа до полудня")),
        (ms(24, 10, 59, 59, 999), ("morning", 3, "3-й дневной час", "Д03", "За два часа до полудня")),
        # Exactly between noon (12:42) and sunset (16:15): the past event wins.
        (ms(24, 14, 28, 30), ("afternoon", 9, "9-й дневной час", "Д09", "Через два часа после полудня")),
        (ms(24, 13, 43), ("afternoon", 8, "8-й дневной час", "Д08", "Через час после полудня")),
        (ms(24, 0, 0), ("before_midnight", 6, "6-й ночной час", "Н06", "Через восемь часов после заката")),
        # Step edges: 14 min 29 s and 14 min 30 s; 59 min 29 s and 59 min 30 s; 90 min rounds up.
        (ms(24, 16, 29, 29), ("dusk", 1, "1-й ночной час", "Н01", "На закате")),
        (ms(24, 16, 29, 30), ("dusk", 1, "1-й ночной час", "Н01", "Вскоре после заката")),
        (ms(24, 17, 14, 29), ("dusk", 1, "1-й ночной час", "Н01", "Вскоре после заката")),
        (ms(24, 17, 14, 30), ("dusk", 1, "1-й ночной час", "Н01", "Через час после заката")),
        (ms(24, 17, 45), ("dusk", 2, "2-й ночной час", "Н02", "Через два часа после заката")),
    ]
    for moment, (pid, hour, seasonal, short, relative) in cases:
        try:
            r = lt.describe(moment, events, REFERENCE_UTC_OFFSET, names)
            got = (r["pora"]["id"], r["seasonal"]["hour"], r["display"]["seasonal"], r["seasonal"]["short"],
                   r["display"]["relative"])
        except Exception as exc:  # report, do not stop the whole check
            got = f"{type(exc).__name__}: {exc}"
        if got != (pid, hour, seasonal, short, relative):
            errors.append(f"formatter {lt.iso_ms(moment, REFERENCE_UTC_OFFSET)}: got {got}, expected {(pid, hour, seasonal, short, relative)}")
    # The refusal of a bare local time is checked below.
    # The command line on real data, including the full calendar-and-pora sentence.
    real = [
        (["2026-11-24T12:00:00+03:00", "--style", "full"],
         "В четвёртый день Тёмной воды, в лето 1274-е, при высоком солнце."),
        (["2026-06-21T23:50:00+03:00"], "Первый сон в шхерах"),
        (["2026-06-21T23:50:00+03:00", "--style", "seasonal"], "4-й ночной час"),
    ]
    for argv, expected in real:
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = lt.main(argv)
        except SystemExit as exc:
            code = exc.code
        if code != 0 or out.getvalue().strip() != expected:
            errors.append(f"formatter {' '.join(argv)}: got {out.getvalue().strip()!r} (exit {code}), expected {expected!r}")
    try:
        lt.parse_instant("2026-11-24T12:00")
        errors.append("formatter: a timestamp without UTC offset must be refused")
    except lt.TimeError:
        pass
    return len(cases) + len(real)


def check_time(defs, errors):
    b = defs["build"]
    years = list(range(b["start_year"], b["end_year"] + 1))
    check_time_definitions(defs, errors)
    check_zone(defs, years, errors)
    events, max_diff = check_sun(defs, years, errors)
    check_time_files(defs, years, events, errors)
    names = check_time_names(defs, errors)
    cases = check_formatter(names, errors)
    return {"sun_days": len(events), "sun_max_diff_s": round(max_diff), "time_control_cases": cases}


def main():
    errors = []
    try:
        defs = load(DEFINITIONS)
        check_definitions(defs, errors)
        summary = check_data(defs, errors)
        summary.update(check_time(defs, errors))
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
