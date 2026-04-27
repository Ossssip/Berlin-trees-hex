"""
pipeline/fetch_phylopic_curation.py
-------------------------------------
Fetch PhyloPic silhouette SVGs for every genus in the curation priority list.

Downloads to data/icon_curation/icons/{genus}.svg.
Re-uses any SVG already present in web/public/icons/ as a local cache (copy,
no API call).  Only queries PhyloPic for genera not already cached.

Writes data/icon_curation/curation_index.json — a manifest with:
    { genus: { credit, license } | null }

and data/icon_curation/no_silhouette.txt — a plain list of genera for which
PhyloPic has no SVG.

Run:
    conda run -n berlin_trees python pipeline/fetch_phylopic_curation.py
    conda run -n berlin_trees python pipeline/fetch_phylopic_curation.py --force
"""

import argparse
import json
import logging
import shutil
import time
from pathlib import Path

import requests

PRIORITY_LIST = Path("data/icon_curation/genera_priority.txt")
CURATION_DIR = Path("data/icon_curation/icons")
INDEX_PATH = Path("data/icon_curation/curation_index.json")
NO_SVG_PATH = Path("data/icon_curation/no_silhouette.txt")

# Production icons — used as a local cache to avoid re-fetching.
PROD_ICONS = Path("web/public/icons")

API_BASE = "https://api.phylopic.org"
API_DELAY = 0.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Genus list helpers
# ---------------------------------------------------------------------------


def load_genera(path: Path) -> list[str]:
    genera = []
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            genera.append(line.lower())
    return genera


# ---------------------------------------------------------------------------
# PhyloPic API helpers  (same logic as fetch_phylopic.py)
# ---------------------------------------------------------------------------


def get_build(session: requests.Session) -> int:
    r = session.get(f"{API_BASE}/", headers={"Accept": "application/vnd.phylopic.v2+json"})
    r.raise_for_status()
    build = r.json()["build"]
    log.info("PhyloPic API build: %d", build)
    return build


def lookup_genus(session: requests.Session, genus: str, build: int) -> dict | None:
    r = session.get(
        f"{API_BASE}/nodes",
        params={"filter_name": genus, "build": build, "page": 0},
        headers={"Accept": "application/vnd.phylopic.v2+json"},
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    items = r.json().get("_links", {}).get("items", [])
    if not items:
        return None

    node_uuid = items[0]["href"].split("/nodes/")[1].split("?")[0]
    time.sleep(API_DELAY)

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
    source_file = links.get("sourceFile", {})
    if source_file.get("type") != "image/svg+xml":
        return None

    return {
        "svg_url": source_file["href"],
        "credit": links.get("contributor", {}).get("title", "Unknown contributor"),
        "license": links.get("license", {}).get("href", ""),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(force: bool = False) -> None:
    CURATION_DIR.mkdir(parents=True, exist_ok=True)

    genera = load_genera(PRIORITY_LIST)
    log.info("Priority list: %d genera", len(genera))

    # Load existing index.
    import contextlib

    existing_index: dict = {}
    if INDEX_PATH.exists():
        with contextlib.suppress(json.JSONDecodeError):
            existing_index = json.loads(INDEX_PATH.read_text())

    session = requests.Session()
    build = get_build(session)
    time.sleep(API_DELAY)

    index: dict = {}
    found = cached = missing = errors = 0

    for genus in genera:
        out_path = CURATION_DIR / f"{genus}.svg"

        # --- try local production cache first (no API call) ---
        prod_path = PROD_ICONS / f"{genus}.svg"
        if not force and out_path.exists() and genus in existing_index:
            index[genus] = existing_index[genus]
            cached += 1
            continue

        if not force and prod_path.exists():
            shutil.copy2(prod_path, out_path)
            # Copy attribution from production index if available.
            prod_index_path = PROD_ICONS / "phylopic_index.json"
            prod_index: dict = {}
            if prod_index_path.exists():
                with contextlib.suppress(json.JSONDecodeError):
                    prod_index = json.loads(prod_index_path.read_text())
            index[genus] = prod_index.get(
                genus, {"credit": "see phylopic_index.json", "license": ""}
            )
            log.info("cached  %s  (from prod icons)", genus)
            cached += 1
            continue

        # --- fetch from PhyloPic API ---
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
            r = session.get(result["svg_url"])
            r.raise_for_status()
            out_path.write_bytes(r.content)
            time.sleep(API_DELAY)
        except requests.RequestException as exc:
            log.warning("  SVG download failed for %s: %s", genus, exc)
            index[genus] = None
            errors += 1
            continue

        index[genus] = {"credit": result["credit"], "license": result["license"]}
        log.info("  saved  (credit: %s)", result["credit"])
        found += 1

    # Write manifest.
    INDEX_PATH.write_text(json.dumps(index, indent=2, ensure_ascii=False))

    # Write no-silhouette list.
    no_svg = [g for g in genera if index.get(g) is None]
    NO_SVG_PATH.write_text("\n".join(no_svg) + "\n")

    log.info(
        "Done. %d fetched, %d cached/copied, %d not in PhyloPic, %d errors",
        found,
        cached,
        missing,
        errors,
    )
    log.info("SVGs with silhouette: %d / %d", len(genera) - len(no_svg), len(genera))
    log.info("Manifest: %s", INDEX_PATH)
    log.info("No-silhouette list: %s  (%d genera)", NO_SVG_PATH, len(no_svg))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch PhyloPic silhouettes for curation")
    parser.add_argument("--force", action="store_true", help="Re-fetch all SVGs")
    args = parser.parse_args()
    main(force=args.force)
