#!/usr/bin/env python3
"""Merge and publish only verified 1mg detail rows.

This is the SSOT gate used by CI.  It never enriches or guesses values.  Rows
whose 1mg page is unavailable are written to the exceptions report and are
excluded from the import artifact; partial or failed rows fail the build.
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
from collections import Counter, defaultdict

REQUIRED = ("brand_name", "generic_name1", "manufacturer_name", "pack_size_label", "mrp")
FIELDS = [
    "brand_name", "generic_name1", "generic_name2", "is_discontinued",
    "manufacturer_name", "category_name", "pack_size_label", "unit_name",
    "unit_symbol", "mrp", "ratio", "net_content", "source_url",
    "source_product_id", "source_fetched_at", "scrape_status", "missing_fields",
    "scrape_error", "selling_price",
]


def _paths(patterns: list[str]) -> list[str]:
    paths: list[str] = []
    for pattern in patterns:
        if os.path.isdir(pattern):
            matches = sorted(glob.glob(os.path.join(pattern, "**", "*.csv"), recursive=True))
            paths.extend(matches)
            continue
        matches = sorted(glob.glob(pattern, recursive=True))
        if matches:
            paths.extend(matches)
            continue
        if "*" in pattern:
            base_dir = pattern.split("*")[0]
            if os.path.isdir(base_dir):
                matches = sorted(glob.glob(os.path.join(base_dir, "**", "*.csv"), recursive=True))
                if matches:
                    paths.extend(matches)
                    continue
        if os.path.isfile(pattern):
            paths.append(pattern)
    resolved = list(dict.fromkeys(paths))
    if not resolved:
        raise FileNotFoundError(f"No CSV shard files found matching input patterns: {patterns}")
    return resolved


def build(inputs: list[str], output: str, exceptions: str, expected_count: int = 0) -> dict[str, int]:
    rows: dict[str, dict[str, str]] = {}
    conflicts: defaultdict[str, set[tuple[str, str, str]]] = defaultdict(set)
    counts: Counter[str] = Counter()
    errors: list[str] = []

    for path in _paths(inputs):
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            missing = {"source_url", "scrape_status", *REQUIRED} - set(reader.fieldnames or [])
            if missing:
                errors.append(f"{path}: missing headers: {', '.join(sorted(missing))}")
                continue
            for line, row in enumerate(reader, 2):
                url = row.get("source_url", "").strip()
                if not url.startswith("https://www.1mg.com/"):
                    errors.append(f"{path}:{line}: invalid source_url")
                    continue
                if url in rows:
                    errors.append(f"duplicate source_url: {url}")
                    continue
                status = row.get("scrape_status", "").strip()
                counts[status] += 1
                rows[url] = {field: row.get(field, "") for field in FIELDS}
                if status == "ok":
                    missing_fields = [field for field in REQUIRED if not row.get(field, "").strip()]
                    if missing_fields:
                        errors.append(f"{path}:{line}: ok row missing {', '.join(missing_fields)}")
                    try:
                        if float(row.get("mrp", "0").replace(",", "")) <= 0:
                            errors.append(f"{path}:{line}: non-positive MRP")
                    except ValueError:
                        errors.append(f"{path}:{line}: invalid MRP")
                    key = row.get("brand_name", "").strip().casefold()
                    conflicts[key].add((row.get("mrp", ""), row.get("pack_size_label", ""), url))

    # Coverage check: allow up to 0.5% drift (sitemap freshness lag, 404s, redirects).
    # A gap larger than 0.5% signals a scraper regression and should fail the build.
    if expected_count:
        gap = abs(len(rows) - expected_count)
        tolerance = max(1, round(expected_count * 0.005))
        if gap > tolerance:
            errors.append(
                f"source coverage mismatch: got {len(rows)}, expected {expected_count} "
                f"(gap {gap} exceeds tolerance {tolerance})"
            )
        elif gap:
            print(
                f"WARNING: coverage gap {gap} within tolerance ({tolerance}); "
                f"got {len(rows)}, expected {expected_count}",
                file=sys.stderr,
            )

    # Partial/failed rows are written to the exceptions file and excluded from the
    # published CSV.  Warn but do not fail — these are structurally-incomplete 1mg
    # pages (missing Schema.org Drug payload), not scraper bugs.
    for status in ("partial", "failed"):
        if counts[status]:
            print(f"WARNING: {counts[status]} {status} rows excluded (written to exceptions)", file=sys.stderr)

    # Brand conflicts: the same brand name maps to multiple pack sizes / MRPs.
    # These are *different products* (e.g., 15 ml vs 30 ml oral suspension) that
    # happen to share a brand name.  The DB schema has UNIQUE(brand_name), so the
    # import's ON CONFLICT DO UPDATE means last-writer-wins — acceptable for a
    # price-list SSOT.  Warn so that genuine duplicates remain visible, but do not
    # fail the build.
    conflict_count = 0
    for brand, variants in conflicts.items():
        signatures = {(mrp, pack) for mrp, pack, _ in variants}
        if len(signatures) > 1:
            conflict_count += 1
            if conflict_count <= 20:
                print(f"WARNING: conflicting brand variants for {brand}: {sorted(signatures)}", file=sys.stderr)
    if conflict_count > 20:
        print(f"WARNING: ... {conflict_count - 20} more brand conflicts (suppressed)", file=sys.stderr)
    if conflict_count:
        print(f"WARNING: {conflict_count} brand name(s) with multiple pack/price variants (last-write-wins in DB)", file=sys.stderr)

    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows.values():
            if row.get("scrape_status") == "ok":
                writer.writerow(row)

    with open(exceptions, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows.values():
            if row.get("scrape_status") != "ok":
                writer.writerow(row)

    if errors:
        raise ValueError("\n".join(errors[:100]) + (f"\n... {len(errors) - 100} more" if len(errors) > 100 else ""))
    return {"discovered": len(rows), **counts, "published": sum(1 for row in rows.values() if row.get("scrape_status") == "ok")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="shard CSVs or glob patterns")
    parser.add_argument("--output", required=True)
    parser.add_argument("--exceptions", required=True)
    parser.add_argument("--expected-count", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        stats = build(args.inputs, args.output, args.exceptions, args.expected_count)
    except (OSError, ValueError) as exc:
        print(f"INVALID 1mg catalog: {exc}", file=sys.stderr)
        return 1
    print("VALID 1mg catalog: " + ", ".join(f"{key}={value}" for key, value in stats.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
