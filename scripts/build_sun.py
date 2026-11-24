#!/usr/bin/env python3
"""Compute sunrise, solar noon and sunset for the Ladoga time of day.

Writes one file per civil year:
    data/sun/<YYYY>.json

Rules come from the "time" section of ladoga-definitions.json. The solar
events are computed with the vendored Astronomy Engine file and grouped by
civil date in the vendored Europe/Moscow time zone file; both are checked
against their SHA-256 sums before anything is computed.

This is the only script in the project that needs Astronomy Engine. The
calendar generator reads the finished data/sun/ files and does not need it.

Usage:
    python3 scripts/build_sun.py           # (re)write data/sun/
    python3 scripts/build_sun.py --check   # compare files on disk, write nothing
Exit code: 0 on success, 1 if --check finds differences or the input is invalid.
"""
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_calendar import (DEFINITIONS, ROOT, DefinitionError, is_number, output_dir,  # noqa: E402
                            reject_duplicate_keys, serialize, validate)

SUN_DIR = "sun"
EVENTS = ["sunrise", "solar_noon", "sunset"]
# Astronomy Engine counts time in days from 2000-01-01 12:00 UTC.
J2000_UNIX = 946728000
SUPPORTED_SOLAR_EVENTS = {
    "sunrise_set_altitude_deg": -50 / 60,
    "altitude_kind": "geometric_solar_center",
    "additional_refraction": False,
    "solar_noon_kind": "upper_meridian_transit",
    "timestamp_timezone": "UTC",
    "timestamp_precision": "second",
    "rounding": "nearest_second_ties_toward_future",
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pinned_file(entry, what):
    """Return the path of a vendored file after checking its SHA-256."""
    path = (ROOT / entry["path"]).resolve()
    if ROOT not in path.parents:
        raise DefinitionError(f"{what} path {entry['path']!r} is outside the repository")
    if not path.is_file():
        raise DefinitionError(f"{what} file {entry['path']} is missing")
    actual = sha256(path)
    if actual != entry["sha256"]:
        raise DefinitionError(f"{what} file {entry['path']} has SHA-256 {actual}, definitions expect {entry['sha256']}")
    return path


def load_engine(time_defs):
    path = pinned_file(time_defs["ephemeris_engine"], "ephemeris engine")
    spec = importlib.util.spec_from_file_location("astronomy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if time_defs["ephemeris_engine"]["delta_t"] != "DeltaT_EspenakMeeus":
        raise DefinitionError("only the built-in DeltaT_EspenakMeeus model is supported")
    return module


def load_zone(defs):
    with open(pinned_file(defs["time"]["timezone_file"], "time zone"), "rb") as f:
        return ZoneInfo.from_file(f, key=defs["timezone"])


def to_engine_time(astro, unix_seconds):
    # Astronomy Engine treats UTC as UT1; the difference is under 0.9 s.
    return astro.Time((unix_seconds - J2000_UNIX) / 86400)


def to_unix_second(t):
    """Round an engine time to the nearest whole second; an exact half goes to the later second."""
    return math.floor(t.ut * 86400 + J2000_UNIX + 0.5)


def iso_z(unix_seconds):
    return dt.datetime.fromtimestamp(unix_seconds, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_window(day, zone):
    """Unix seconds of local midnight at the start of `day` and of the next day."""
    start = dt.datetime(day.year, day.month, day.day, tzinfo=zone)
    nxt = day + dt.timedelta(days=1)
    end = dt.datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone)
    return int(start.timestamp()), int(end.timestamp())


def solar_events(day, astro, observer, zone, altitude):
    start, end = day_window(day, zone)
    t0 = to_engine_time(astro, start)
    span = (end - start) / 86400
    rise = astro.SearchAltitude(astro.Body.Sun, observer, astro.Direction.Rise, t0, span, altitude)
    sset = astro.SearchAltitude(astro.Body.Sun, observer, astro.Direction.Set, t0, span, altitude)
    noon = astro.SearchHourAngle(astro.Body.Sun, observer, 0.0, t0, +1)
    found = {"sunrise": rise, "solar_noon": noon.time if noon else None, "sunset": sset}
    result = {}
    for name in EVENTS:
        if found[name] is None:
            raise DefinitionError(f"{day}: no {name} found")
        sec = to_unix_second(found[name])
        if not start <= sec < end:
            raise DefinitionError(f"{day}: {name} {iso_z(sec)} is outside the civil date")
        result[name] = sec
    if not result["sunrise"] < result["solar_noon"] < result["sunset"]:
        raise DefinitionError(f"{day}: events are out of order: {result}")
    return result


def validate_sun(defs):
    t = defs["time"]
    if t["solar_events"] != SUPPORTED_SOLAR_EVENTS:
        raise DefinitionError(f"time.solar_events is {t['solar_events']!r}; this script supports only {SUPPORTED_SOLAR_EVENTS!r}")
    loc = t["reference_location"]
    # A rough guard only. The real guard is the per-day check below: every sunrise,
    # noon and sunset must exist and fall inside its own civil date.
    if not (is_number(loc.get("latitude_deg")) and abs(loc["latitude_deg"]) <= 62):
        raise DefinitionError("reference latitude must be a number within 62 degrees of the equator")
    if not (is_number(loc.get("longitude_deg")) and -180 <= loc["longitude_deg"] <= 180):
        raise DefinitionError("reference longitude must be a number from -180 to 180")
    if not is_number(loc.get("elevation_m")):
        raise DefinitionError("reference elevation must be a number")
    if not isinstance(t["sun_data_revision"], int) or isinstance(t["sun_data_revision"], bool):
        raise DefinitionError("time.sun_data_revision must be an integer")


def build_sun(defs):
    """Return {path relative to output dir: file text}."""
    validate(defs)
    validate_sun(defs)
    t = defs["time"]
    astro = load_engine(t)
    zone = load_zone(defs)
    loc = t["reference_location"]
    observer = astro.Observer(loc["latitude_deg"], loc["longitude_deg"], loc["elevation_m"])
    altitude = t["solar_events"]["sunrise_set_altitude_deg"]
    b = defs["build"]
    start_year, end_year = b["start_year"], b["end_year"]

    first = dt.date(start_year, 1, 1) - dt.timedelta(days=1)
    last = dt.date(end_year + 1, 1, 1)
    events = {}
    day = first
    while day <= last:
        events[day] = solar_events(day, astro, observer, zone, altitude)
        day += dt.timedelta(days=1)

    # Nights between neighbouring days must have positive length.
    for day in sorted(events)[:-1]:
        nxt = day + dt.timedelta(days=1)
        if not events[day]["sunset"] < events[nxt]["sunrise"]:
            raise DefinitionError(f"{day}: night to {nxt} is not positive")

    def record(day):
        return {name: iso_z(events[day][name]) for name in EVENTS}

    engine = t["ephemeris_engine"]
    tzf = t["timezone_file"]
    out = {}
    for year in range(start_year, end_year + 1):
        days = sorted(d for d in events if d.year == year)
        edge = {}
        if year == start_year:
            edge[first.isoformat()] = record(first)
        if year == end_year:
            edge[last.isoformat()] = record(last)
        obj = {
            "schema_version": defs["schema_version"],
            "calendar_id": defs["calendar_id"],
            "model_id": t["model_id"],
            "sun_data_revision": t["sun_data_revision"],
            "year": year,
            "reference_location": {k: loc[k] for k in ("id", "latitude_deg", "longitude_deg", "elevation_m")},
            "timezone": defs["timezone"],
            "tzdb_version": tzf["tzdb_version"],
            "tzif_sha256": tzf["sha256"],
            "ephemeris_engine": {k: engine[k] for k in ("name", "version", "sha256", "delta_t")},
            "solar_events": t["solar_events"],
            "days": len(days),
            "edge_events": edge,
            "dates": {d.isoformat(): record(d) for d in days},
        }
        out[f"{SUN_DIR}/{year}.json"] = serialize(obj)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare with files on disk, write nothing")
    args = parser.parse_args()
    try:
        with open(DEFINITIONS, encoding="utf-8") as f:
            defs = json.load(f, object_pairs_hook=reject_duplicate_keys)
        out_dir = output_dir(defs)
        files = build_sun(defs)
        if not files:
            raise DefinitionError("nothing to build")
    except (OSError, ValueError, KeyError, TypeError, DefinitionError) as exc:
        print(f"FAILED: {exc}")
        return 1

    sun_dir = out_dir / SUN_DIR
    existing = {p.relative_to(out_dir).as_posix() for p in sun_dir.rglob("*") if p.is_file()} if sun_dir.is_dir() else set()
    stale = sorted(existing - set(files))
    if args.check:
        problems = [f"missing: {p}" for p in sorted(set(files) - existing)]
        problems += [f"unexpected: {p}" for p in stale]
        problems += [f"differs: {p}" for p in sorted(set(files) & existing)
                     if (out_dir / p).read_bytes() != files[p].encode("utf-8")]
        if problems:
            print("FAILED: data/sun does not match the definitions")
            for p in problems:
                print("  " + p)
            return 1
        print(f"OK: {len(files)} sun files match")
        return 0

    for p, text in files.items():
        target = out_dir / p
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    for p in stale:
        (out_dir / p).unlink()
    print(f"wrote {len(files)} files to {sun_dir.relative_to(ROOT)}/" + (f", removed {len(stale)}" if stale else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
