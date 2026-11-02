#!/usr/bin/env python3
"""Fail-closed validation for a detail-scraped 1mg catalog CSV."""

import argparse
import csv
import sys
from collections import defaultdict

REQUIRED = ("brand_name", "generic_name1", "manufacturer_name", "pack_size_label", "mrp")


def validate(path: str, allow_source_gaps: bool = False) -> tuple[list[str], dict[str, int]]:
    errors: list[str] = []
    counts = defaultdict(int)
    duplicate_values = defaultdict(set)
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing_headers = {"source_url", "scrape_status", *REQUIRED} - set(reader.fieldnames or [])
        if missing_headers:
            return [f"missing headers: {', '.join(sorted(missing_headers))}"], {}
        for line, row in enumerate(reader, 2):
            status = row.get("scrape_status", "")
            counts[status] += 1
            source_gap = allow_source_gaps and status == "unavailable"
            if status != "ok" and not source_gap:
                errors.append(f"line {line}: scrape_status={row.get('scrape_status', '')}")
            if not source_gap:
                missing = [field for field in REQUIRED if not row.get(field, "").strip()]
                if missing:
                    errors.append(f"line {line}: missing {', '.join(missing)}")
            if not row.get("source_url", "").startswith("https://www.1mg.com/"):
                errors.append(f"line {line}: invalid 1mg source_url")
            if not source_gap:
                try:
                    if float(row.get("mrp", "0")) <= 0:
                        errors.append(f"line {line}: non-positive MRP")
                except ValueError:
                    errors.append(f"line {line}: invalid MRP {row.get('mrp', '')!r}")
            key = row.get("brand_name", "").strip().upper()
            duplicate_values[key].add((row.get("mrp", ""), row.get("pack_size_label", "")))
    conflict_count = 0
    for brand, values in duplicate_values.items():
        if len(values) > 1:
            conflict_count += 1
            if conflict_count <= 20:
                print(f"WARNING: conflicting variants for {brand}: {sorted(values)}", file=sys.stderr)
    if conflict_count > 20:
        print(f"WARNING: ... {conflict_count - 20} more brand conflicts (suppressed)", file=sys.stderr)
    if conflict_count:
        print(f"WARNING: {conflict_count} brand(s) with multiple pack/price variants in output CSV (last-write-wins in DB)", file=sys.stderr)
    return errors, dict(counts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path")
    parser.add_argument(
        "--allow-source-gaps",
        action="store_true",
        help="accept unavailable 1mg pages as auditable source gaps; partial/failed rows still fail",
    )
    args = parser.parse_args(argv)
    errors, counts = validate(args.csv_path, args.allow_source_gaps)
    if errors:
        print(f"INVALID: {len(errors)} issue(s)", file=sys.stderr)
        print("\n".join(errors[:100]), file=sys.stderr)
        if len(errors) > 100:
            print(f"... {len(errors) - 100} more", file=sys.stderr)
        return 1
    summary = ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
    print(f"VALID: {args.csv_path} ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
