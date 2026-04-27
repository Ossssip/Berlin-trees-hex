"""
pipeline/fetch_phylopic.py
--------------------------
Fetch PhyloPic silhouette SVGs for every tree genus in the processed data.

For each genus found in data/processed/trees.parquet:
  1. Queries the PhyloPic v2 API for a matching taxonomic node.
  2. Retrieves the primary silhouette image UUID and contributor attribution.
  3. Downloads the SVG to web/public/icons/{genus}.svg.

Also writes web/public/icons/phylopic_index.json — a manifest consumed by the
map frontend to display the correct silhouette and attribution in hover popups.

The script is idempotent: already-downloaded SVGs are skipped unless --force is
passed. This makes repeated DVC runs cheap.

Run:
    conda run -n berlin_trees python pipeline/fetch_phylopic.py
    conda run -n berlin_trees python pipeline/fetch_phylopic.py --force
"""

import argparse
import json
import logging
import time
from pathlib import Path

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

TREES_PARQUET = Path("data/processed/trees.parquet")
ICONS_DIR = Path("web/public/icons")
INDEX_PATH = ICONS_DIR / "phylopic_index.json"

API_BASE = "https://api.phylopic.org"
IMG_BASE = "https://images.phylopic.org"

# Polite delay between API calls (seconds).  PhyloPic is a volunteer-run project.
API_DELAY = 0.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PhyloPic API helpers
# ---------------------------------------------------------------------------


def get_build(session: requests.Session) -> int:
    """Fetch the current PhyloPic build number (required on every API call)."""
    r = session.get(
        f"{API_BASE}/",
        headers={"Accept": "application/vnd.phylopic.v2+json"},
    )
    r.raise_for_status()
    build = r.json()["build"]
    log.info("PhyloPic API build: %d", build)
    return build


def lookup_genus(session: requests.Session, genus: str, build: int) -> dict | None:
    """
    Look up a genus by Latin name in PhyloPic.

    Returns a dict with keys:
        img_uuid   — PhyloPic image UUID (used to build the SVG URL)
        credit     — contributor display name
        license    — CC license URL

    Returns None if no matching image is found.

    API v2 notes:
    - filter_name is case-sensitive and expects lowercase genus names
    - List endpoints require page=0 to get actual items (in _links.items)
    - Image UUID is extracted from _links.self.href: "/images/{uuid}?build=N"
    - SVG is at https://images.phylopic.org/images/{uuid}/source.svg
    """
    # --- 1. Find taxonomic node by name (page=0 returns _links.items) ---
    r = session.get(
        f"{API_BASE}/nodes",
        params={"filter_name": genus, "build": build, "page": 0},
        headers={"Accept": "application/vnd.phylopic.v2+json"},
    )
    if r.status_code == 404:
        # PhyloPic returns 404 when a page=0 request has no results.
        return None
    r.raise_for_status()
    page_data = r.json()

    items = page_data.get("_links", {}).get("items", [])
    if not items:
        return None

    # Take the first matching node; href looks like "/nodes/{uuid}?build=N"
    node_href: str = items[0]["href"]
    node_uuid = node_href.split("/nodes/")[1].split("?")[0]
    time.sleep(API_DELAY)

    # --- 2. Fetch node with its primary image embedded ---
    r = session.get(
        f"{API_BASE}/nodes/{node_uuid}",
        params={"embed_primaryImage": "true", "build": build},
        headers={"Accept": "application/vnd.phylopic.v2+json"},
    )
    r.raise_for_status()
    node_data = r.json()

    primary_img = node_data.get("_embedded", {}).get("primaryImage")
    if not primary_img:
        return None

    links = primary_img.get("_links", {})

    # Prefer the SVG source file URL returned directly by the API (avoids 404
    # for images that were submitted as raster and have no source.svg).
    source_file = links.get("sourceFile", {})
    if source_file.get("type") == "image/svg+xml":
        svg_url: str = source_file["href"]
    else:
        # No SVG source available for this image.
        return None

    credit: str = links.get("contributor", {}).get("title", "Unknown contributor")
    license_url: str = links.get("license", {}).get("href", "")

    return {"svg_url": svg_url, "credit": credit, "license": license_url}


def download_svg(session: requests.Session, svg_url: str, out_path: Path) -> None:
    r = session.get(svg_url)
    r.raise_for_status()
    out_path.write_bytes(r.content)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(force: bool = False) -> None:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)

    # --- Collect unique genera from the processed tree dataset ---
    log.info("Reading genera from %s", TREES_PARQUET)
    df = pd.read_parquet(TREES_PARQUET, columns=["genus_latin"])
    genera = sorted(g for g in df["genus_latin"].dropna().str.strip().str.lower().unique() if g)
    log.info("Found %d unique genera", len(genera))

    # Load existing index (if any) to avoid redundant fetches.
    existing_index: dict = {}
    if INDEX_PATH.exists():
        import contextlib

        with contextlib.suppress(json.JSONDecodeError):
            existing_index = json.loads(INDEX_PATH.read_text())

    session = requests.Session()
    build = get_build(session)
    time.sleep(API_DELAY)

    index: dict = {}
    found = skipped = missing = errors = 0

    for genus in genera:
        svg_path = ICONS_DIR / f"{genus}.svg"

        # Skip if already fetched (unless --force).
        if not force and svg_path.exists() and genus in existing_index:
            index[genus] = existing_index[genus]
            skipped += 1
            continue

        log.info("Fetching: %s", genus)
        try:
            result = lookup_genus(session, genus, build)
            time.sleep(API_DELAY)
        except requests.RequestException as exc:
            log.warning("  API error for %s: %s", genus, exc)
            index[genus] = None
            errors += 1
            continue

        if result is None:
            log.info("  not found in PhyloPic")
            index[genus] = None
            missing += 1
            continue

        try:
            download_svg(session, result["svg_url"], svg_path)
            time.sleep(API_DELAY)
        except requests.RequestException as exc:
            log.warning("  SVG download failed for %s: %s", genus, exc)
            index[genus] = None
            errors += 1
            continue

        index[genus] = {
            "credit": result["credit"],
            "license": result["license"],
        }
        log.info("  saved  (credit: %s)", result["credit"])
        found += 1

    # Write manifest.
    INDEX_PATH.write_text(json.dumps(index, indent=2, ensure_ascii=False))

    log.info(
        "Done. %d fetched, %d skipped (cached), %d not in PhyloPic, %d errors",
        found,
        skipped,
        missing,
        errors,
    )
    log.info("Manifest: %s  (%d entries)", INDEX_PATH, len(index))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch PhyloPic silhouettes")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch all SVGs even if already cached",
    )
    args = parser.parse_args()
    main(force=args.force)
