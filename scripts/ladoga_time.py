#!/usr/bin/env python3
"""Show a moment in Ladoga time of day, using only the files in data/.

Usage:
    python3 scripts/ladoga_time.py                              # now, as a pora name
    python3 scripts/ladoga_time.py 2026-11-24T12:00:00+03:00 --style seasonal
    python3 scripts/ladoga_time.py now --style full             # calendar date and pora together
    python3 scripts/ladoga_time.py now --json                   # every part as JSON

Styles: pora (default), seasonal, relative, full.
A timestamp must carry a UTC offset or Z; a bare local time is refused.
Needs Python 3.11 or newer (for "Z" in datetime.fromisoformat).
Exit code: 0 on success, 2 on an error (printed as "error: <code>: <message>").
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EVENTS = ["sunrise", "solar_noon", "sunset"]
STYLES = ["pora", "seasonal", "relative", "full"]
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


class TimeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def to_ms(moment):
    """Aware datetime -> Unix milliseconds, rounding down."""
    delta = moment - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def iso_ms(ms, offset_s=0):
    zone = dt.timezone(dt.timedelta(seconds=offset_s))
    moment = EPOCH + dt.timedelta(milliseconds=ms)
    text = moment.astimezone(zone).isoformat(timespec="milliseconds")
    return text.replace("+00:00", "Z") if offset_s == 0 else text


def parse_instant(text):
    if text == "now":
        return to_ms(dt.datetime.now(dt.timezone.utc))
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        raise TimeError("invalid_timestamp", f"{text!r} is not an ISO 8601 timestamp") from None
    if moment.tzinfo is None:
        raise TimeError("invalid_timestamp", f"{text!r} has no UTC offset or Z")
    try:
        return to_ms(moment)
    except OverflowError:
        raise TimeError("out_of_range", f"{text!r} is far outside the calendar range") from None


def plural(n, forms):
    """Russian form for a number: 1 минуту, 2 минуты, 5 минут; 11-14 take the third form."""
    if 11 <= n % 100 <= 14:
        return forms[2]
    return forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]


def fill(template, values):
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def describe(instant_ms, events, offset_s, names):
    """Compute every part of the result.

    events: {date: {"sunrise"|"solar_noon"|"sunset": Unix milliseconds}}, must hold the
    civil date of the moment and both neighbouring dates.
    offset_s: the fixed UTC offset of the calendar time zone in seconds.
    names: the contents of data/<language>/time.json.
    """
    local_ms = instant_ms + offset_s * 1000
    civil = dt.date(1970, 1, 1) + dt.timedelta(days=local_ms // 86400000)
    days = {d: civil + dt.timedelta(days=d) for d in (-1, 0, 1)}
    for d in days.values():
        if d not in events:
            raise TimeError("no_record", f"no solar events for {d}")
    prev, cur, nxt = (events[days[d]] for d in (-1, 0, 1))

    # Day or night period, start included, end excluded.
    if instant_ms < cur["sunrise"]:
        kind, start, end, start_date = "night", prev["sunset"], cur["sunrise"], days[-1]
    elif instant_ms < cur["sunset"]:
        kind, start, end, start_date = "day", cur["sunrise"], cur["sunset"], days[0]
    else:
        kind, start, end, start_date = "night", cur["sunset"], nxt["sunrise"], days[0]
    duration, elapsed = end - start, instant_ms - start
    if not (duration > 0 and 0 <= elapsed < duration):
        raise TimeError("invalid_solar_interval", f"moment is outside {kind} {start}..{end}")
    hour_index = 12 * elapsed // duration
    remainder = 12 * elapsed % duration
    pora = [p for p in names["poras"] if p["period"] == kind][hour_index // 3]
    period = names["periods"][kind]

    # Nearest solar event of the three days; equal distance goes to the past one.
    candidates = []
    for d in (-1, 0, 1):
        for order, e in enumerate(EVENTS):
            off = instant_ms - events[days[d]][e]
            candidates.append(((abs(off), 0 if off >= 0 else 1, order), e, events[days[d]][e], off))
    _, event, event_at, offset_ms = min(candidates)

    t = names["templates"]
    hour = hour_index + 1
    seasonal_text = fill(t["seasonal"], {"hour": hour, "period_adjective": period["adjective"]})
    seasonal_short = fill(t["seasonal_short"], {"period_letter": period["letter"], "hour_2d": f"{hour:02d}"})
    minutes = (abs(offset_ms) + 30000) // 60000
    genitive = names["event_names"][event]["genitive"]
    if minutes == 0:
        relative_text = fill(t["relative_near"], {"event_genitive": genitive})
    else:
        h, m = divmod(minutes, 60)
        parts = []
        if h:
            parts.append(f"{h} {plural(h, names['unit_forms']['hour'])}")
        if m:
            parts.append(f"{m} {plural(m, names['unit_forms']['minute'])}")
        key = "relative_before" if offset_ms < 0 else "relative_after"
        relative_text = fill(t[key], {"duration": " ".join(parts), "event_genitive": genitive})

    return {
        "instant": iso_ms(instant_ms),
        "civil_date": civil.isoformat(),
        "civil_time": iso_ms(instant_ms, offset_s)[11:19],
        "period": {"kind": kind, "start": iso_ms(start), "end": iso_ms(end),
                   "start_civil_date": start_date.isoformat(), "elapsed_ms": elapsed, "duration_ms": duration},
        "seasonal": {"hour": hour, "hour_progress": remainder / duration,
                     "progress_percent": 100 * remainder // duration, "short": seasonal_short},
        "pora": {k: pora[k] for k in ("id", "short_name", "name", "phrase")},
        "relative": {"event": event, "event_at": iso_ms(event_at), "offset_ms": offset_ms},
        "display": {"pora": pora["name"], "seasonal": seasonal_text, "relative": relative_text},
    }


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_data(data_dir=DATA_DIR, language=None):
    index = read_json(data_dir / "index.json")
    lang = language or index["default_language"]
    names = read_json(data_dir / index["time_names"][lang])
    events, offset_s = {}, None
    for year, rel in index["sun"].items():
        sun = read_json(data_dir / rel)
        for key, rec in list(sun["edge_events"].items()) + list(sun["dates"].items()):
            events[dt.date.fromisoformat(key)] = {
                e: to_ms(dt.datetime.fromisoformat(rec[e])) for e in EVENTS}
    first_time = read_json(data_dir / next(iter(index["time"].values())))
    sign = -1 if first_time["utc_offset"].startswith("-") else 1
    hh, mm = first_time["utc_offset"][1:].split(":")
    offset_s = sign * (int(hh) * 3600 + int(mm) * 60)
    return index, lang, names, events, offset_s


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("timestamp", nargs="?", default="now")
    parser.add_argument("--style", default="pora")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON (all styles at once)")
    args = parser.parse_args(argv)
    try:
        if args.style not in STYLES:
            raise TimeError("invalid_style", f"unknown style {args.style!r}; use one of {', '.join(STYLES)}")
        instant = parse_instant(args.timestamp)
        index, lang, names, events, offset_s = load_data()
        try:
            civil = (dt.date(1970, 1, 1) + dt.timedelta(days=(instant + offset_s * 1000) // 86400000)).isoformat()
        except OverflowError:
            raise TimeError("out_of_range", "the moment is far outside the calendar range") from None
        if not index["range"]["start"] <= civil <= index["range"]["end"]:
            raise TimeError("out_of_range", f"{civil} is outside {index['range']['start']}..{index['range']['end']}")
        result = describe(instant, events, offset_s, names)
        cal = read_json(DATA_DIR / index["files"][lang][civil[:4]])
        day = cal["dates"][civil]
        result["display"]["full"] = fill(names["templates"]["full_text"], {
            "day_text": day["day_text"], "year_phrase": cal["year_phrase"],
            "pora_phrase": result["pora"]["phrase"]})
    except TimeError as exc:
        print(f"error: {exc.code}: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print(f"error: incompatible_data: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["display"][args.style])
    return 0


if __name__ == "__main__":
    sys.exit(main())
