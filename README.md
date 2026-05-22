# Geophysical Index Acquisition Toolkit

A dependency-free Python toolkit for fetching, cleaning, and classifying
geomagnetic activity indices from public government and agency archives.

## Problem

Geomagnetic and satellite-data analyses depend on a clean, well-characterised
record of geomagnetic activity. The relevant indices are scattered across
several public archives — each with a different access protocol, file format,
and missing-data convention — and naive parsing silently corrupts results by
averaging in sentinel values such as `9999` or `99999`.

## What it does

Retrieves indices from three structurally different sources and normalises them
into a single daily table:

| Source | Indices | Format |
|--------|---------|--------|
| Kyoto WDC | Dst, AE / AU / AL | fixed-width hourly `.for.request` |
| NASA OMNIWeb (HRO) | SYM-H, AE (1-minute) | day-of-year column text |
| GFZ Potsdam | Kp / ap / Ap | JSON web service |

## Design

- **Robust acquisition** — each source is queried through a cascade of candidate
  endpoints (definitive → provisional → realtime → alternate layouts), so the
  pipeline degrades gracefully when the most authoritative product is not yet
  published for a recent interval.
- **Tolerant parsing** — each format is parsed with its own missing-data
  sentinels correctly rejected rather than imputed; every daily aggregate
  carries the count of valid samples behind it.
- **Auditable output** — per-day aggregates are written to a CSV, with the raw
  payloads retained so any derived number can be traced back to its source.
- **Explicit classification** — days are labelled quiet or active by transparent
  thresholds on daily-mean Kp and daily-minimum Dst.

## Usage

```bash
python geomag_data_toolkit.py --start 2024-06-01 --end 2024-06-30 --outdir geomag_output
```

Produces `geomag_indices_summary.csv` (one row per day: Kp, Dst, AE and SYM-H
aggregates plus an activity label) alongside the retained raw files.

## Requirements

Python standard library only (`urllib`, `csv`, `json`, `datetime`). No
third-party dependencies and no credentials required.
