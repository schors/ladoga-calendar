#!/usr/bin/env python3
"""Draw the README pictures (SVG) from the files in data/.

Usage:
    python3 scripts/build_images.py          # write docs/img/*.svg
    python3 scripts/build_images.py --check  # compare with the files on disk

Only the standard library is used. The pictures are illustrations: the
calendar itself lives in data/, and these files are drawn from it.
"""
import argparse
import base64
import hashlib
import datetime as dt
import json
import math
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "docs" / "img"
MSK = dt.timezone(dt.timedelta(hours=3))

# Moments shown on the day cards (Moscow time).
CARD_MOMENTS = ["2026-11-24T22:34:00+03:00", "2026-06-24T13:30:00+03:00"]
WHEEL_YEAR = 2026

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
          "сентябрь", "октябрь", "ноябрь", "декабрь"]
# Season tint of each month, winter to autumn.
MONTH_FILL = ["#d9e3ec", "#d9e3ec", "#dfe8d8", "#dfe8d8", "#dfe8d8", "#f1e6c6",
              "#f1e6c6", "#f1e6c6", "#ecd9c6", "#ecd9c6", "#ecd9c6", "#d9e3ec"]
INK = "#23313d"
SOFT = "#5f6f7c"
PAPER = "#f7f4ec"
ACCENT = "#a3472b"
DAY_FILL = ["#f3e3a6", "#f0d77f", "#e9c766", "#e3b25a"]
NIGHT_FILL = ["#6c7d96", "#3f4d66", "#2a3449", "#56657f"]
FONT = "font-family=\"Kurale, Georgia, 'Times New Roman', serif\""
# Kurale by Eduardo Tunni, SIL Open Font License 1.1 (fonts/kurale/OFL.txt).
# It is embedded into each picture: an SVG shown as an image cannot load fonts from outside.
FONT_FILE = ROOT / "fonts" / "kurale" / "Kurale-Regular.ttf"
FONT_SHA256 = "6055aaca6870ca64210dd81fa47f8bb71595a9c8fbe570351ed45a15f1a0820d"


def load(path):
    return json.loads((DATA / path).read_text(encoding="utf-8"))


def text(x, y, s, size=16, fill=INK, anchor="start", weight="normal", style="normal", extra=""):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
            f'font-weight="{weight}" font-style="{style}" {FONT} {extra}>{escape(s)}</text>')


def rhomb(x, y, r, fill="none", stroke=ACCENT, width=1.2):
    return (f'<path d="M{x:.1f},{y - r:.1f} L{x + r:.1f},{y:.1f} L{x:.1f},{y + r:.1f} L{x - r:.1f},{y:.1f} Z" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{width}"/>')


def band(x0, x1, y):
    """A thin ornament row: open rhombs with a dot, small solid rhombs between them.

    A light hint of North Russian and Karelian embroidery, not a copy of any piece.
    """
    step = 28
    n = int((x1 - x0) // step)
    pad = (x1 - x0 - n * step) / 2
    parts = [f'<g opacity="0.55">',
             f'<line x1="{x0:.1f}" y1="{y - 9:.1f}" x2="{x1:.1f}" y2="{y - 9:.1f}" stroke="{ACCENT}" stroke-width="0.8"/>',
             f'<line x1="{x0:.1f}" y1="{y + 9:.1f}" x2="{x1:.1f}" y2="{y + 9:.1f}" stroke="{ACCENT}" stroke-width="0.8"/>']
    for k in range(n):
        cx = x0 + pad + step * (k + 0.5)
        parts.append(rhomb(cx, y, 6))
        parts.append(f'<circle cx="{cx:.1f}" cy="{y:.1f}" r="1.6" fill="{ACCENT}"/>')
        if k < n - 1:
            parts.append(rhomb(cx + step / 2, y, 2.4, fill=ACCENT, stroke="none"))
    parts.append("</g>")
    return "\n".join(parts)


def font_face():
    raw = FONT_FILE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != FONT_SHA256:
        raise SystemExit(f"FAILED: {FONT_FILE.relative_to(ROOT)} does not match its SHA-256")
    data = base64.b64encode(raw).decode("ascii")
    return (f"<style>@font-face{{font-family:Kurale;src:url(data:font/ttf;base64,{data}) "
            f"format('truetype');}}</style>\n")


def svg(width, height, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">\n' + font_face() +
            f'<rect width="{width}" height="{height}" rx="18" fill="{PAPER}"/>\n'
            + "\n".join(body) + "\n</svg>\n")


def wheel():
    """The year as a circle: months as sectors, holidays as dots with labels at the sides."""
    holidays = load("ru/holidays.json")["holidays"]
    width, height, cx, cy = 1260, 860, 630, 430
    r_in, r_out, r_dot, r_label = 120, 225, 238, 300
    days = 366 if WHEEL_YEAR % 4 == 0 else 365
    start = dt.date(WHEEL_YEAR, 1, 1)

    def angle(date):
        # January 1 at the top, clockwise.
        return 2 * math.pi * (date - start).days / days - math.pi / 2

    def point(a, r):
        return cx + r * math.cos(a), cy + r * math.sin(a)

    body = []
    for m in range(12):
        a0 = angle(dt.date(WHEEL_YEAR, m + 1, 1))
        a1 = angle(dt.date(WHEEL_YEAR, m + 2, 1)) if m < 11 else 1.5 * math.pi
        x0, y0 = point(a0, r_out)
        x1, y1 = point(a1, r_out)
        x2, y2 = point(a1, r_in)
        x3, y3 = point(a0, r_in)
        body.append(f'<path d="M{x0:.1f},{y0:.1f} A{r_out},{r_out} 0 0 1 {x1:.1f},{y1:.1f} '
                    f'L{x2:.1f},{y2:.1f} A{r_in},{r_in} 0 0 0 {x3:.1f},{y3:.1f} Z" '
                    f'fill="{MONTH_FILL[m]}" stroke="{PAPER}" stroke-width="3"/>')
        mx, my = point((a0 + a1) / 2, (r_in + r_out) / 2)
        body.append(text(mx, my + 5, MONTHS[m], 14, SOFT, "middle"))

    # Labels go to the left or right column and are spread so they never overlap.
    sides = {"left": [], "right": []}
    for h in holidays:
        a = angle(dt.date(WHEEL_YEAR, h["month"], h["day"]))
        side = "left" if math.cos(a) < -1e-9 or (abs(math.cos(a)) < 1e-9 and math.sin(a) > 0) else "right"
        sides[side].append((point(a, r_label)[1], a, h))
    gap = 25
    for side, items in sides.items():
        items.sort(key=lambda t: t[0])
        ys = [t[0] for t in items]
        for k in range(1, len(ys)):
            ys[k] = max(ys[k], ys[k - 1] + gap)
        overflow = ys[-1] - (height - 30)
        if overflow > 0:
            ys = [y - overflow for y in ys]
            for k in range(len(ys) - 2, -1, -1):
                ys[k] = min(ys[k], ys[k + 1] - gap)
        for (y0, a, h), y in zip(items, ys):
            x, yd = point(a, r_dot)
            body.append(f'<circle cx="{x:.1f}" cy="{yd:.1f}" r="5" fill="{ACCENT}"/>')
            lx = cx + (r_label + 40) * (1 if side == "right" else -1)
            ex, ey = point(a, r_dot + 10)
            body.append(f'<polyline points="{ex:.1f},{ey:.1f} {lx - (8 if side == "right" else -8):.1f},{y:.1f} '
                        f'{lx:.1f},{y:.1f}" fill="none" stroke="{SOFT}" stroke-width="0.8" opacity="0.6"/>')
            date = f'{h["day"]}.{h["month"]:02d}'
            tx = lx + (6 if side == "right" else -6)
            anchor = "start" if side == "right" else "end"
            label = f"{date}  {h['name']}" if side == "right" else f"{h['name']}  {date}"
            body.append(text(tx, y + 5, label, 15, INK, anchor, extra='xml:space="preserve"'))
    # A ring of small rhombs inside the months, like an embroidered rosette.
    ring = ['<g opacity="0.5">']
    for k in range(36):
        a = 2 * math.pi * k / 36
        x, y = point(a, r_in - 14)
        ring.append(rhomb(x, y, 3.2, fill=ACCENT if k % 3 == 0 else "none", width=1))
    ring.append("</g>")
    body.append("\n".join(ring))
    body.append(band(40, width - 40, 34))
    body.append(band(40, width - 40, height - 34))
    era = WHEEL_YEAR - 752
    body.append(text(cx, cy - 16, "Ладожский", 26, INK, "middle"))
    body.append(text(cx, cy + 10, "календарь", 26, INK, "middle"))
    body.append(text(cx, cy + 38, f"{WHEEL_YEAR} — лето {era}-е", 14, SOFT, "middle"))
    return svg(width, height, body)


def card(moment):
    """One moment: day phrase, year, pora and the 24 unequal hours of its solar day."""
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "ladoga_time.py"), moment, "--json"],
                         check=True, capture_output=True, text=True).stdout
    r = json.loads(out)
    civil = dt.date.fromisoformat(r["civil_date"])
    year = load(f"ru/{civil.year}.json")
    day = year["dates"][civil.isoformat()]
    holidays = {h["id"]: h for h in load("ru/holidays.json")["holidays"]}
    anchor = holidays[day["anchor_id"]]
    names = load("ru/time.json")

    # The solar day of the picture: from the sunrise that starts it to the next sunrise.
    sun = {}
    for y in {civil.year - 1, civil.year, civil.year + 1}:
        path = DATA / "sun" / f"{y}.json"
        if path.exists():
            sun.update(json.loads(path.read_text(encoding="utf-8"))["dates"])
    when = dt.datetime.fromisoformat(moment)

    def ts(d, e):
        return dt.datetime.fromisoformat(sun[d.isoformat()][e].replace("Z", "+00:00")).astimezone(MSK)
    start_date = dt.date.fromisoformat(r["period"]["start_civil_date"])
    # A night belongs to the civil date of its sunset, so both kinds start at that date's sunrise.
    rise0, set0, rise1 = ts(start_date, "sunrise"), ts(start_date, "sunset"), ts(start_date + dt.timedelta(1), "sunrise")
    total = (rise1 - rise0).total_seconds()

    width, height = 900, 400
    x0, bar_w, bar_y, bar_h = 40, 820, 262, 34
    body = []
    body.append(text(x0, 62, f'{civil.day} {["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"][civil.month - 1]} {civil.year}, {r["civil_time"][:5]} по московскому времени', 15, SOFT))
    body.append(text(x0, 108, day["text"], 36, INK))
    body.append(text(x0, 142, f'в лето {day["era_year"]}-е · {anchor["name"]}', 18, ACCENT))
    disp = r["display"]
    body.append(text(x0, 184, disp["pora"], 22, INK))
    body.append(text(x0, 212, f'{disp["part"]} · {disp["seasonal"]} · {disp["relative"]}', 16, SOFT))

    poras = names["poras"]
    for i in range(24):
        a, b = (rise0, set0) if i < 12 else (set0, rise1)
        k = i % 12
        t0 = a + (b - a) * k / 12
        t1 = a + (b - a) * (k + 1) / 12
        sx = x0 + bar_w * (t0 - rise0).total_seconds() / total
        ex = x0 + bar_w * (t1 - rise0).total_seconds() / total
        fill = (DAY_FILL if i < 12 else NIGHT_FILL)[k // 3]
        body.append(f'<rect x="{sx:.1f}" y="{bar_y}" width="{ex - sx:.1f}" height="{bar_h}" fill="{fill}" '
                    f'stroke="{PAPER}" stroke-width="{1.5 if k % 3 else 3}"/>')
    for p_i, p in enumerate(poras):
        a, b = (rise0, set0) if p_i < 4 else (set0, rise1)
        k = p_i % 4
        t0 = a + (b - a) * k / 4
        t1 = a + (b - a) * (k + 1) / 4
        mid = x0 + bar_w * (((t0 - rise0) + (t1 - rise0)) / 2).total_seconds() / total
        span = bar_w * (t1 - t0).total_seconds() / total
        if span > 7.2 * len(p["short_name"]) + 10:
            body.append(text(mid, bar_y + bar_h + 22, p["short_name"], 13, SOFT, "middle"))
    mx = x0 + bar_w * (when - rise0).total_seconds() / total
    body.append(f'<path d="M{mx:.1f},{bar_y - 4} l-8,-14 h16 z" fill="{ACCENT}"/>')
    body.append(f'<line x1="{mx:.1f}" y1="{bar_y - 4}" x2="{mx:.1f}" y2="{bar_y + bar_h}" stroke="{ACCENT}" stroke-width="2.5"/>')
    body.append(text(x0, bar_y - 12, f'восход {rise0:%H:%M}', 13, SOFT))
    sx = x0 + bar_w * (set0 - rise0).total_seconds() / total
    body.append(text(sx, bar_y - 12, f'закат {set0:%H:%M}', 13, SOFT, "middle"))
    body.append(text(x0 + bar_w, bar_y - 12, f'восход {rise1:%H:%M}', 13, SOFT, "end"))
    body.append(text(x0, height - 46, f'{anchor["name"]}: {anchor["meaning"]}', 14, SOFT))
    body.append(band(24, width - 24, 22))
    body.append(band(24, width - 24, height - 20))
    return svg(width, height, body)


def build():
    files = {"year-wheel.svg": wheel()}
    for i, m in enumerate(CARD_MOMENTS, 1):
        files[f"day-{i}.svg"] = card(m)
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    files = build()
    if args.check:
        bad = [n for n, s in files.items() if not (OUT / n).exists() or (OUT / n).read_text(encoding="utf-8") != s]
        if bad:
            print("FAILED: pictures differ: " + ", ".join(bad))
            return 1
        print(f"OK: {len(files)} pictures match")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for name, s in files.items():
        (OUT / name).write_text(s, encoding="utf-8")
    print(f"wrote {len(files)} pictures to {OUT.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
