"""
Geomagnetic & Satellite Index Acquisition Toolkit
==================================================

A small, dependency-free toolkit for fetching and cleaning geophysical
activity indices from public government/agency archives, and classifying
days by geomagnetic activity level.

Sources handled:
  - Kyoto WDC      (Dst, AE, ASY/SYM)   fixed-width "*.for.request" format
  - NASA OMNIWeb   (1-minute HRO)        whitespace columns, DOY-based time
  - GFZ Potsdam    (Kp / Ap / ap)        JSON web service

Design goals:
  - Robust fetch: each source has a cascade of candidate endpoints
    (provisional -> realtime -> alternate layouts) tried in order.
  - Tolerant parsing: two awkward real-world formats (Kyoto fixed-width,
    OMNIWeb DOY columns) plus a JSON service, each with their own
    missing-data sentinels (9999 / 99999 / 99998).
  - Clean output: per-day aggregates (mean/max/min + valid-sample counts)
    written to CSV, with raw payloads retained for audit.
  - Quiet/active classification via simple, explicit thresholds.

Standard library only (urllib, csv, json, datetime). No API keys required;
all sources used here are openly accessible.

Usage:
    python geomag_data_toolkit.py --start 2024-06-01 --end 2024-06-30
"""

import argparse
import csv
import datetime as dt
import json
import urllib.request
import urllib.error
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TIMEOUT = 120
USER_AGENT = {"User-Agent": "Mozilla/5.0 (data-acquisition-toolkit)"}

GFZ_BASE = "https://kp.gfz.de/app/json/"
KYOTO_BASE = "https://wdc.kugi.kyoto-u.ac.jp"
OMNIWEB_CGI = "https://omniweb.gsfc.nasa.gov/cgi/nx1.cgi"

# Classification thresholds (standard space-weather conventions)
QUIET_KP_MAX = 3.0      # daily-mean Kp below this -> "quiet"
QUIET_DST_MIN = -30.0   # daily-min Dst above this -> "quiet"


# ---------------------------------------------------------------------------
# Generic HTTP helper
# ---------------------------------------------------------------------------
def http_get_text(url, min_chars=200):
    """Fetch a URL and return decoded text, or None on failure / too-short body."""
    try:
        req = urllib.request.Request(url, headers=USER_AGENT)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            text = resp.read().decode("ascii", errors="ignore")
        if len(text) >= min_chars:
            return text
    except urllib.error.HTTPError as e:
        print(f"    HTTP {e.code}: {e.reason}")
    except Exception as e:
        print(f"    err: {e}")
    return None


def http_get_json(url):
    """Fetch a URL and parse JSON, or return None on failure."""
    try:
        req = urllib.request.Request(url, headers=USER_AGENT)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"    err: {e}")
    return None


# ---------------------------------------------------------------------------
# Source 1 — Kyoto WDC fixed-width hourly indices (Dst / AE / AU / AL)
# ---------------------------------------------------------------------------
def fetch_kyoto_index(index_label, year, month):
    """Fetch a Kyoto WDC hourly index, trying provisional then realtime.

    The Kyoto WDC exposes Dst, AE-family and ASY/SYM under a shared
    fixed-width '.for.request' layout. Provisional data 404s for very
    recent months, so we fall back to the realtime path.
    """
    yy = year % 100
    lbl = index_label.lower()
    candidates = [
        f"{KYOTO_BASE}/{lbl}_provisional/{year}{month:02d}/{lbl}{yy:02d}{month:02d}.for.request",
        f"{KYOTO_BASE}/{lbl}_realtime/{year}{month:02d}/{lbl}{yy:02d}{month:02d}.for.request",
        f"http://{KYOTO_BASE.split('//')[1]}/{lbl}_realtime/{year}{month:02d}/{lbl}{yy:02d}{month:02d}.for.request",
    ]
    for url in candidates:
        print(f"  GET {url}")
        text = http_get_text(url)
        if text:
            print(f"    -> OK ({len(text)} chars)")
            return text
    return None


def parse_kyoto_fixed_width(text, index_label):
    """Parse Kyoto WDC fixed-width hourly format.

    Each data line begins with the index tag (e.g. 'AE', 'DST'), followed by
    a 2-digit year, month and day, then 24 hourly integers in 4-char fields
    starting at column 20. Sentinels 9999 / 99999 mark missing samples.

    Returns: {date_str: [up to 24 hourly values, None for missing]}
    """
    out = {}
    tag = index_label[:2].upper()
    for line in text.splitlines():
        if len(line) < 80 or not line[:3].upper().startswith(tag):
            continue
        try:
            year = 2000 + int(line[3:5])
            month = int(line[5:7])
            day = int(line[8:10])
            date_str = f"{year:04d}-{month:02d}-{day:02d}"
            hourly = []
            for i in range(24):
                col = 20 + i * 4
                token = line[col:col + 4].strip()
                if token and token not in ("9999", "99999"):
                    try:
                        hourly.append(int(token))
                    except ValueError:
                        hourly.append(None)
                else:
                    hourly.append(None)
            if any(v is not None for v in hourly):
                out[date_str] = hourly
        except (ValueError, IndexError):
            continue
    return out


# ---------------------------------------------------------------------------
# Source 2 — NASA OMNIWeb 1-minute HRO (SYM-H, AE, ...)
# ---------------------------------------------------------------------------
# OMNIWeb 1-minute variable codes: AE=37, AL=38, AU=39, ASY-D=40, SYM-H=41
OMNIWEB_VARS = {"AE": 37, "AL": 38, "AU": 39, "ASY-D": 40, "SYM-H": 41}


def fetch_omniweb_minute(var_name, start_date, end_date):
    """Fetch a 1-minute OMNIWeb HRO variable over a date range.

    Tries the documented variable code first, then a couple of neighbours,
    since OMNIWeb's variable numbering has shifted across deployments.
    """
    sd, ed = start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d")
    primary = OMNIWEB_VARS.get(var_name, 41)
    for code in (primary, primary - 1, primary + 1):
        url = (f"{OMNIWEB_CGI}?activity=retrieve&res=min&spacecraft=omni_min"
               f"&start_date={sd}&end_date={ed}&vars={code}")
        print(f"  GET {url}")
        text = http_get_text(url, min_chars=500)
        if text:
            print(f"    -> OK ({len(text)} chars, var code {code})")
            return text
    return None


def parse_omniweb_minute(text, reject_above=99998):
    """Parse OMNIWeb 1-minute output: 'YEAR DOY HR MN value' per row.

    Time is day-of-year based; we convert to calendar dates. Sentinel values
    at or above `reject_above` (e.g. 99999) are dropped.

    Returns: {date_str: [1-minute values]}
    """
    out = defaultdict(list)
    for line in text.splitlines():
        s = line.strip()
        if not s or not s[0].isdigit():
            continue
        parts = s.split()
        if len(parts) < 5:
            continue
        try:
            year, doy = int(parts[0]), int(parts[1])
            value = float(parts[4])
            if value > reject_above:
                continue
            date = dt.date(year, 1, 1) + dt.timedelta(days=doy - 1)
            out[date.strftime("%Y-%m-%d")].append(value)
        except (ValueError, IndexError):
            continue
    return out


# ---------------------------------------------------------------------------
# Source 3 — GFZ Potsdam Kp / Ap JSON web service
# ---------------------------------------------------------------------------
def fetch_gfz_index(index_name, start_iso, end_iso):
    """Fetch a GFZ index (Kp, ap, Ap) from the JSON web service.

    Tries 'definitive' status first, then unrestricted, returning the first
    non-empty series.
    """
    for status in ("def", ""):
        url = f"{GFZ_BASE}?start={start_iso}&end={end_iso}&index={index_name}"
        if status:
            url += f"&status={status}"
        print(f"  GET {url}")
        data = http_get_json(url)
        if data and len(data.get(index_name, [])) > 0:
            print(f"    -> {len(data[index_name])} values (status='{status or 'all'}')")
            return data
    return None


def gfz_to_daily(data, index_name):
    """Group a GFZ timestamped series into {date_str: [values]}."""
    out = defaultdict(list)
    if not data:
        return out
    for ts, value in zip(data.get("datetime", []), data.get(index_name, [])):
        if value is not None:
            out[ts[:10]].append(value)
    return out


# ---------------------------------------------------------------------------
# Aggregation & classification
# ---------------------------------------------------------------------------
def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def classify_day(kp_mean, dst_min):
    """Label a day 'quiet' or 'active' from daily Kp mean and Dst minimum.

    Requires both available indices to confirm 'quiet'; otherwise 'active'
    (conservative: an unknown day is not assumed calm).
    """
    if kp_mean is None or dst_min is None:
        return "unknown"
    if kp_mean < QUIET_KP_MAX and dst_min > QUIET_DST_MIN:
        return "quiet"
    return "active"


def build_table(start, end, kp_daily, dst_daily, ae_daily, symh_daily):
    """Assemble one summary row per day across all available indices."""
    rows = []
    for d in daterange(start, end):
        ds = d.strftime("%Y-%m-%d")
        kp = [v for v in kp_daily.get(ds, []) if isinstance(v, (int, float))]
        dst = [v for v in dst_daily.get(ds, []) if isinstance(v, (int, float))]
        ae = [v for v in ae_daily.get(ds, []) if v is not None]
        sh = [v for v in symh_daily.get(ds, []) if v is not None]

        kp_mean = round(sum(kp) / len(kp), 3) if kp else None
        dst_min = min(dst) if dst else None

        rows.append({
            "date": ds,
            "Kp_mean": kp_mean,
            "Kp_max": max(kp) if kp else None,
            "Dst_min": dst_min,
            "AE_max": max(ae) if ae else None,
            "AE_mean": round(sum(ae) / len(ae), 1) if ae else None,
            "SYMH_min": min(sh) if sh else None,
            "N_Kp": len(kp), "N_Dst": len(dst), "N_AE": len(ae), "N_SYMH": len(sh),
            "classification": classify_day(kp_mean, dst_min),
        })
    return rows


def write_csv(rows, path):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {path}")


def print_table(rows):
    print("\n" + "=" * 92)
    print("DAILY GEOMAGNETIC ACTIVITY SUMMARY")
    print("=" * 92)
    hdr = (f"{'date':12s} {'class':9s} {'Kp_mean':>8s} {'Kp_max':>7s} "
           f"{'Dst_min':>8s} {'AE_max':>7s} {'SYMH_min':>9s}")
    print(hdr)
    print("-" * len(hdr))
    fmt = lambda v, w: (f"{v:>{w}}" if v is not None else f"{'NA':>{w}}")
    for r in rows:
        print(f"{r['date']:12s} {r['classification']:9s} "
              f"{fmt(r['Kp_mean'], 8)} {fmt(r['Kp_max'], 7)} {fmt(r['Dst_min'], 8)} "
              f"{fmt(r['AE_max'], 7)} {fmt(r['SYMH_min'], 9)}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def acquire(start, end, outdir):
    """Run the full acquisition pipeline for a [start, end] date range."""
    outdir.mkdir(parents=True, exist_ok=True)
    start_iso = start.strftime("%Y-%m-%dT00:00:00Z")
    end_iso = end.strftime("%Y-%m-%dT23:59:59Z")

    print("=" * 72)
    print(f"Geomagnetic index acquisition: {start} .. {end}")
    print("=" * 72)

    # Kp / Ap from GFZ JSON service
    print("\n[1/4] GFZ Kp (3-hourly)...")
    kp_daily = gfz_to_daily(fetch_gfz_index("Kp", start_iso, end_iso), "Kp")

    # Dst from Kyoto (month-by-month over the range)
    print("\n[2/4] Kyoto Dst (hourly)...")
    dst_daily = {}
    for year, month in months_in_range(start, end):
        text = fetch_kyoto_index("Dst", year, month)
        if text:
            (outdir / f"raw_dst_{year}{month:02d}.txt").write_text(text)
            dst_daily.update(parse_kyoto_fixed_width(text, "DST"))

    # AE from Kyoto, OMNIWeb fallback
    print("\n[3/4] AE (Kyoto hourly, OMNIWeb 1-min fallback)...")
    ae_daily = {}
    for year, month in months_in_range(start, end):
        text = fetch_kyoto_index("AE", year, month)
        if text:
            (outdir / f"raw_ae_{year}{month:02d}.txt").write_text(text)
            ae_daily.update(parse_kyoto_fixed_width(text, "AE"))
    if not ae_daily:
        print("  Kyoto AE unavailable -> OMNIWeb 1-min fallback")
        text = fetch_omniweb_minute("AE", start, end)
        if text:
            (outdir / "raw_ae_omniweb.txt").write_text(text)
            ae_daily = parse_omniweb_minute(text)

    # SYM-H from OMNIWeb 1-minute
    print("\n[4/4] SYM-H (OMNIWeb 1-min)...")
    symh_daily = {}
    text = fetch_omniweb_minute("SYM-H", start, end)
    if text:
        (outdir / "raw_symh_omniweb.txt").write_text(text)
        symh_daily = parse_omniweb_minute(text)

    rows = build_table(start, end, kp_daily, dst_daily, ae_daily, symh_daily)
    write_csv(rows, outdir / "geomag_indices_summary.csv")
    print_table(rows)

    quiet = [r["date"] for r in rows if r["classification"] == "quiet"]
    print(f"\nClassified {len(quiet)} quiet day(s) out of {len(rows)}.")
    return rows


def months_in_range(start, end):
    """Yield (year, month) covering the range, inclusive of endpoints."""
    seen, d = [], dt.date(start.year, start.month, 1)
    while d <= end:
        seen.append((d.year, d.month))
        d = dt.date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
    return seen


def parse_args():
    today = dt.date.today()
    default_start = today - dt.timedelta(days=30)
    p = argparse.ArgumentParser(description="Geomagnetic index acquisition toolkit")
    p.add_argument("--start", default=default_start.isoformat(), help="YYYY-MM-DD")
    p.add_argument("--end", default=today.isoformat(), help="YYYY-MM-DD")
    p.add_argument("--outdir", default="geomag_output", help="output directory")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    acquire(
        dt.date.fromisoformat(args.start),
        dt.date.fromisoformat(args.end),
        Path(args.outdir),
    )
