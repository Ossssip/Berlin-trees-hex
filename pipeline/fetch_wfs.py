"""
pipeline/fetch_wfs.py
---------------------
Paginated WFS 2.0 fetcher for Berlin Geoportal sources.

Usage:
    python pipeline/fetch_wfs.py --source baumbestand_anlagen
    python pipeline/fetch_wfs.py --source baumbestand_anlagen --max-features 10000
    python pipeline/fetch_wfs.py --source baumbestand_anlagen --out data/raw/test.parquet

All sources are defined in pipeline/sources.yml.
Output is saved in native CRS (EPSG:25833); reprojection to EPSG:4326 happens in aggregate_h3.py.
"""

import argparse
import io
import logging
import time
from pathlib import Path

import geopandas as gpd
import requests
import yaml

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = REPO_ROOT / "pipeline" / "sources.yml"
RAW_DIR = REPO_ROOT / "data" / "raw"

PAGE_SIZE = 1000           # features per WFS request
MAX_RETRIES = 5           # attempts per page before giving up
RETRY_BACKOFF_BASE = 2.0  # exponential backoff base (seconds)
REQUEST_TIMEOUT = 60      # seconds
CHECKPOINT_INTERVAL = 100_000  # write .partial.parquet every N features

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WFS helpers
# ---------------------------------------------------------------------------


def wfs_hits(session: requests.Session, url: str, layer: str) -> int:
    """Return total feature count via resultType=hits (fast, no geometry returned)."""
    params = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": layer,
        "resultType": "hits",
    }
    r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    # Response is a minimal FeatureCollection with numberMatched attribute
    import re

    m = re.search(r'numberMatched="(\d+)"', r.text)
    if m:
        return int(m.group(1))
    # Fallback: some servers put it in numberReturned
    m = re.search(r'numberReturned="(\d+)"', r.text)
    if m:
        return int(m.group(1))
    raise ValueError(f"Could not parse feature count from hits response:\n{r.text[:500]}")


def fetch_page(
    session: requests.Session,
    url: str,
    layer: str,
    start_index: int,
    count: int,
    srs_name: str = "EPSG:25833",
) -> gpd.GeoDataFrame:
    """Fetch a single page of features as a GeoDataFrame."""
    params = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": layer,
        "outputFormat": "application/json",
        "SRSNAME": srs_name,
        "count": count,
        "startIndex": start_index,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            gdf = gpd.read_file(io.BytesIO(r.content))
            return gdf
        except Exception as exc:
            wait = RETRY_BACKOFF_BASE**attempt
            if attempt == MAX_RETRIES:
                raise RuntimeError(
                    f"Page startIndex={start_index} failed after {MAX_RETRIES} attempts: {exc}"
                ) from exc
            log.warning(
                "  Page startIndex=%d attempt %d/%d failed (%s). Retrying in %.0fs…",
                start_index,
                attempt,
                MAX_RETRIES,
                exc,
                wait,
            )
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Main fetch loop
# ---------------------------------------------------------------------------


def fetch_source(source_key: str, max_features: int | None, out_path: Path) -> gpd.GeoDataFrame:
    with open(SOURCES_FILE) as f:
        sources = yaml.safe_load(f)

    if source_key not in sources:
        raise KeyError(
            f"Source '{source_key}' not found in {SOURCES_FILE}. " f"Available: {list(sources)}"
        )

    cfg = sources[source_key]
    url = cfg["url"]
    layer = cfg["layer"]
    crs = cfg.get("crs", "EPSG:25833")

    log.info("Source  : %s", source_key)
    log.info("URL     : %s", url)
    log.info("Layer   : %s", layer)

    checkpoint_path = out_path.with_suffix(".partial.parquet")

    with requests.Session() as session:
        # Step 1: count total features
        log.info("Querying resultType=hits …")
        total = wfs_hits(session, url, layer)
        log.info("Total features available: %d", total)

        if max_features is not None:
            fetch_limit = min(total, max_features)
            log.info("Limiting fetch to %d features (--max-features)", fetch_limit)
        else:
            fetch_limit = total

        # Step 2: resume from checkpoint if available
        pages: list[gpd.GeoDataFrame] = []
        start = 0

        if checkpoint_path.exists():
            checkpoint_gdf = gpd.read_parquet(checkpoint_path)
            if not checkpoint_gdf.empty and len(checkpoint_gdf) < fetch_limit:
                pages = [checkpoint_gdf]
                start = len(checkpoint_gdf)
                log.info("Resuming from checkpoint: %d / %d features", start, fetch_limit)

        # Step 3: paginate
        next_checkpoint = ((start // CHECKPOINT_INTERVAL) + 1) * CHECKPOINT_INTERVAL
        page_num = start // PAGE_SIZE

        while start < fetch_limit:
            page_count = min(PAGE_SIZE, fetch_limit - start)
            page_num += 1
            log.info(
                "  Page %d  startIndex=%d  count=%d  (%.1f%%)",
                page_num,
                start,
                page_count,
                100.0 * start / fetch_limit if fetch_limit else 100,
            )
            page = fetch_page(session, url, layer, start, page_count, crs)

            if page.empty:
                log.info("  Empty page returned — assuming end of data.")
                break

            pages.append(page)
            returned = len(page)
            start += returned

            if start >= next_checkpoint:
                _write_checkpoint(pages, checkpoint_path)
                next_checkpoint += CHECKPOINT_INTERVAL

            # If server returned fewer than requested, we've hit the end
            if returned < page_count:
                log.info(
                    "  Server returned %d < %d requested — end of data.",
                    returned,
                    page_count,
                )
                break

    if not pages:
        raise RuntimeError("No features fetched — check URL and layer name.")

    # Step 4: concatenate and set CRS
    log.info("Concatenating %d page(s) …", len(pages))
    gdf = gpd.GeoDataFrame(
        gpd.pd.concat(pages, ignore_index=True),
    )

    # Ensure CRS is set correctly (GeoJSON from GDI-BE sometimes lacks explicit CRS)
    if gdf.crs is None:
        log.warning("CRS not set in response — assuming %s", crs)
        gdf = gdf.set_crs(crs)
    elif gdf.crs.to_epsg() != int(crs.split(":")[1]):
        log.info("Reprojecting from %s to %s", gdf.crs, crs)
        gdf = gdf.to_crs(crs)

    return gdf


def _write_checkpoint(pages: list[gpd.GeoDataFrame], checkpoint_path: Path) -> None:
    gdf = gpd.GeoDataFrame(gpd.pd.concat(pages, ignore_index=True))
    gdf.to_parquet(checkpoint_path, index=False)
    log.info("Checkpoint saved: %d features → %s", len(gdf), checkpoint_path)


def print_stats(gdf: gpd.GeoDataFrame, source_key: str) -> None:
    log.info("─" * 60)
    log.info("Source      : %s", source_key)
    log.info("Rows        : %d", len(gdf))
    log.info("CRS         : %s", gdf.crs)
    log.info("Geometry    : %s", gdf.geometry.geom_type.value_counts().to_dict())
    log.info("Columns (%d): %s", len(gdf.columns), list(gdf.columns))
    null_rates = (gdf.isnull().mean() * 100).round(1)
    high_null = null_rates[null_rates > 20]
    if not high_null.empty:
        log.info("High-null columns (>20%%):\n%s", high_null.to_string())
    log.info("─" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Paginated WFS 2.0 fetcher for Berlin tree sources.")
    p.add_argument("--source", required=True, help="Key in pipeline/sources.yml")
    p.add_argument(
        "--max-features",
        type=int,
        default=None,
        help="Stop after N features (for testing). Omit to fetch all.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output path (default: data/raw/{source}.parquet)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    out_path = args.out or (RAW_DIR / f"{args.source}.parquet")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    log.info("=" * 60)
    log.info("fetch_wfs.py  →  %s", out_path)
    log.info("=" * 60)

    gdf = fetch_source(args.source, args.max_features, out_path)
    print_stats(gdf, args.source)

    log.info("Writing %s …", out_path)
    gdf.to_parquet(out_path, index=False)
    size_mb = out_path.stat().st_size / 1024 / 1024
    log.info("Saved: %s  (%.2f MB, %d rows)", out_path, size_mb, len(gdf))

    checkpoint_path = out_path.with_suffix(".partial.parquet")
    if checkpoint_path.exists():
        checkpoint_path.unlink()
        log.info("Checkpoint removed.")


if __name__ == "__main__":
    main()
