#!/usr/bin/env python3
"""Scrape medicine detail pages from Tata 1mg.

The sitemap contains URLs and names only.  This scraper follows every product
URL and reads the page's Schema.org Drug payload plus the rendered pack text.
It is deliberately resumable: a failed or partial row is retained in the
audit file and can be retried with ``--retry-failed``.

No price, pack size, composition, or manufacturer is inferred from another
medicine.  Missing source fields remain empty and are reported in
``missing_fields``.
"""

from __future__ import annotations

import argparse
import csv
import concurrent.futures
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

SITEMAP_INDEX_URL = "https://www.1mg.com/sitemap.xml"
BASE_URL = "https://www.1mg.com"
NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
USER_AGENT = "CredaERP-1mg-detail-catalog/2.0 (+catalog-audit)"
_THREAD_LOCAL = threading.local()

FIELDS = [
    "brand_name", "generic_name1", "generic_name2", "is_discontinued",
    "manufacturer_name", "category_name", "pack_size_label", "unit_name",
    "unit_symbol", "mrp", "ratio", "net_content", "source_url",
    "source_product_id", "source_fetched_at", "scrape_status", "missing_fields",
    "scrape_error", "selling_price",
]

VOLUME_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(ml|millilitres?|l|litres?)\b", re.I)
PACK_RE = re.compile(
    r"\b(strip|bottle|box|pack|packet|tube|vial|ampoule|sachet)\s+of\s+"
    r"(\d+(?:\.\d+)?)\s*(ml|millilitres?|l|litres?|g|gm|kg|tablets?|tabs?|capsules?|caps?|pcs?|pieces?)?",
    re.I,
)
COUNT_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(tablets?|tabs?|capsules?|caps?|pieces?|pcs?)\b", re.I)
MRP_RE = re.compile(r"\bMRP\s*[:₹-]?\s*₹?\s*([\d,]+(?:\.\d+)?)", re.I)
PRICE_RE = re.compile(r"₹\s*([\d,]+(?:\.\d+)?)")
ID_RE = re.compile(r"-(\d+)(?:[/?#]|$)")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " + ".join(x for x in (_text(v) for v in value) if x)
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("legalName") or value.get("value"))
    if hasattr(value, "get_text"):
        return re.sub(r"\s+", " ", value.get_text(" ", strip=True)).strip()
    return re.sub(r"\s+", " ", str(value)).strip()


def _money(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(str(value).replace(',', '')):.2f}"
    except (TypeError, ValueError):
        return ""


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _drug_payload(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for candidate in _walk(payload):
            kind = candidate.get("@type")
            if kind == "Drug" or (isinstance(kind, list) and "Drug" in kind):
                return candidate
            nested = candidate.get("drug")
            if isinstance(nested, dict) and nested.get("name"):
                return nested
    return {}


def _normalise_unit(unit: str) -> tuple[str, str]:
    unit = unit.lower()
    if unit in {"ml", "millilitre", "millilitres", "l", "litre", "litres"}:
        return "ML", "ML"
    if unit in {"g", "gm", "kg"}:
        return "GM", "GM"
    if unit.startswith("tab"):
        return "TABLET", "TAB"
    if unit.startswith("cap"):
        return "CAPSULE", "CAP"
    if unit in {"pcs", "pc", "piece", "pieces"}:
        return "PCS", "PCS"
    return "", ""


def _pack_text(text: str, dosage_form: str = "") -> tuple[str, str, str, str]:
    """Return (pack label, unit name, unit symbol, net content)."""
    volume = VOLUME_RE.search(text)
    if volume and re.search(r"\b(syrup|suspension|solution|drops?|liquid|tonic|mouthwash)\b", text, re.I):
        number, raw_unit = volume.groups()
        canonical = "ml" if raw_unit.lower().startswith("ml") or raw_unit.lower().startswith("mill") else "l"
        pack = f"{number} {canonical} {dosage_form or 'Syrup'}".strip()
        return pack, "ML", "ML", f"{number} {canonical}"

    pack = PACK_RE.search(text)
    if pack:
        container, number, raw_unit = pack.groups()
        label = f"{container.lower()} of {number}"
        unit_name, unit_symbol = "", ""
        net = ""
        if raw_unit:
            unit_name, unit_symbol = _normalise_unit(raw_unit)
            net = f"{number} {raw_unit.lower()}"
            label += f" {raw_unit.lower()}"
        return label, unit_name, unit_symbol, net

    count = COUNT_RE.search(text)
    if count:
        number, raw_unit = count.groups()
        unit_name, unit_symbol = _normalise_unit(raw_unit)
        return f"strip of {number} {raw_unit.lower()}", unit_name, unit_symbol, f"{number} {raw_unit.lower()}"

    if dosage_form:
        unit_name, unit_symbol = _normalise_unit(dosage_form)
        if not unit_name and dosage_form.lower() in {"syrup", "suspension", "solution", "drops"}:
            unit_name, unit_symbol = "ML", "ML"
        return dosage_form, unit_name, unit_symbol, ""
    return "", "", "", ""


def parse_product_page(page_html: str, source_url: str, fetched_at: str | None = None) -> dict[str, str]:
    """Parse one 1mg product page into the canonical catalog row."""
    soup = BeautifulSoup(page_html, "html.parser")
    drug = _drug_payload(soup)
    visible = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    heading = _text(soup.find("h1"))
    name = _text(drug.get("name")) or heading
    dosage_form = _text(drug.get("dosageForm"))
    # Restrict pack detection to product/price text first; this avoids treating
    # composition strengths such as 30mg as a bottle quantity.
    pack_source = " ".join(x for x in (heading, visible[:6000]) if x)
    pack, unit_name, unit_symbol, net_content = _pack_text(pack_source, dosage_form)

    offers = drug.get("offers") if isinstance(drug.get("offers"), dict) else {}
    mrp = ""
    mrp_match = MRP_RE.search(visible)
    if mrp_match:
        mrp = _money(mrp_match.group(1))
    if not mrp:
        mrp = _money(offers.get("price"))

    prices = [_money(x) for x in PRICE_RE.findall(visible)]
    selling_price = prices[0] if prices else ""
    active = _text(drug.get("activeIngredient")) or _text(drug.get("nonProprietaryName"))
    marketer = (drug.get("manufacturer") or drug.get("marketer")
                or offers.get("seller") or {})
    manufacturer = _text(marketer)
    breadcrumbs = [x for x in soup.select('a[href*="therapeutic-classes"]')]
    category = _text(breadcrumbs[-1]) if breadcrumbs else "allopathy"
    product_id_matches = ID_RE.findall(source_url)
    product_id = product_id_matches[-1] if product_id_matches else ""

    row = {field: "" for field in FIELDS}
    row.update({
        "brand_name": name,
        "generic_name1": active,
        "is_discontinued": "FALSE",
        "manufacturer_name": manufacturer,
        "category_name": category,
        "pack_size_label": pack,
        "unit_name": unit_name,
        "unit_symbol": unit_symbol,
        "mrp": mrp,
        "net_content": net_content,
        "source_url": source_url,
        "source_product_id": product_id,
        "source_fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
        "selling_price": selling_price,
    })
    required = ["brand_name", "generic_name1", "manufacturer_name", "pack_size_label", "mrp"]
    missing = [field for field in required if not row.get(field)]
    row["scrape_status"] = "ok" if not missing else "partial"
    row["missing_fields"] = ";".join(missing)
    return row


def sitemap_urls(session: requests.Session, kind: str, timeout: float) -> list[str]:
    response = session.get(SITEMAP_INDEX_URL, timeout=timeout)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    names = {"drugs": "sitemap_drugs_", "otc": "sitemap_otc_", "all": "sitemap_"}
    prefix = names[kind]
    urls: list[str] = []
    for node in root.findall("s:sitemap", NS):
        loc = node.find("s:loc", NS)
        if loc is None or not loc.text:
            continue
        if prefix not in loc.text or (kind == "all" and "sitemap_" not in loc.text):
            continue
        part = session.get(loc.text, timeout=timeout)
        part.raise_for_status()
        part_root = ET.fromstring(part.content)
        urls.extend(x.text.strip() for x in part_root.findall("s:url/s:loc", NS) if x.text)
    return list(dict.fromkeys(urls))


def fetch_product(session: requests.Session, url: str, timeout: float, retries: int) -> tuple[str, str]:
    last_error = ""
    for attempt in range(retries + 1):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            return response.text, ""
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(min(30.0, 2.0 ** attempt))
    return "", last_error


def _worker_session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
        _THREAD_LOCAL.session = session
    return session


def _process_product(item: tuple[str, float, float, int]) -> dict[str, str]:
    url, delay, timeout, retries = item
    if delay > 0:
        time.sleep(delay)
    page, error = fetch_product(_worker_session(), url, timeout, retries)
    if error:
        row = {field: "" for field in FIELDS}
        status = "unavailable" if "/undefined" in error or "404" in error else "failed"
        row.update({"source_url": url, "scrape_status": status, "scrape_error": error,
                    "source_fetched_at": datetime.now(timezone.utc).isoformat()})
        return row
    return parse_product_page(page, url)


def _load_existing(path: str) -> dict[str, dict[str, str]]:
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as handle:
        rows = {}
        for row in csv.DictReader(handle):
            source_url = row.get("source_url", "")
            if not source_url:
                continue
            ids = ID_RE.findall(source_url)
            if ids:
                row["source_product_id"] = ids[-1]
            rows[source_url] = row
        return rows


def _write_rows(path: str, rows: Iterable[dict[str, str]]) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    temp = f"{path}.partial"
    with open(temp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default="medicines_update.csv")
    parser.add_argument("--kind", choices=("drugs", "otc", "all"), default="drugs")
    parser.add_argument("--limit", type=int, default=0, help="only process the first N URLs (for testing)")
    parser.add_argument("--delay", type=float, default=0.8, help="seconds between product requests")
    parser.add_argument("--workers", type=int, default=1, help="parallel detail requests; keep conservative for source load")
    parser.add_argument("--batch-size", type=int, default=500, help="URLs submitted per resumable batch")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--retry-partial", action="store_true")
    parser.add_argument("--shard-index", type=int, default=0,
                        help="zero-based sitemap shard to process")
    parser.add_argument("--shard-count", type=int, default=1,
                        help="total sitemap shards; use with --shard-index in CI")
    args = parser.parse_args(argv)

    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        parser.error("--shard-index must be within [0, --shard-count)")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    urls = sitemap_urls(session, args.kind, args.timeout)
    discovered_count = len(urls)
    if args.shard_count > 1:
        urls = urls[args.shard_index::args.shard_count]
    if args.limit > 0:
        urls = urls[:args.limit]
    print(f"Discovered {discovered_count} unique {args.kind} product URLs; processing {len(urls)} "
          f"(shard {args.shard_index + 1}/{args.shard_count})", flush=True)
    rows = _load_existing(args.output)
    stats = {"ok": 0, "partial": 0, "failed": 0, "unavailable": 0, "skipped": 0}
    pending: list[str] = []
    for url in urls:
        old = rows.get(url)
        if old and old.get("scrape_status") == "ok":
            stats["skipped"] += 1
            continue
        if old and old.get("scrape_status") == "partial" and not args.retry_partial:
            stats["skipped"] += 1
            continue
        if old and old.get("scrape_status") in {"failed", "unavailable"} and not args.retry_failed:
            stats["skipped"] += 1
            continue
        pending.append(url)

    workers = max(1, args.workers)
    batch_size = max(workers, args.batch_size)
    processed = 0
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset:offset + batch_size]
        work = [(url, max(0.0, args.delay), args.timeout, args.retries) for url in batch]
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            for row in executor.map(_process_product, work):
                rows[row["source_url"]] = row
                stats[row["scrape_status"]] += 1
                processed += 1
        _write_rows(args.output, rows.values())
        print(f"[{processed}/{len(pending)} pending] ok={stats['ok']} partial={stats['partial']} failed={stats['failed']} unavailable={stats['unavailable']} skipped={stats['skipped']}", flush=True)

    _write_rows(args.output, rows.values())
    return 0 if stats["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
