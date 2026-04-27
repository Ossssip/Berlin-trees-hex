"""
pipeline/check_updates.py
--------------------------
Lightweight WFS change-detection script.

For each source in pipeline/sources.yml, fires a resultType=hits request
and compares the feature count against data/checksums.json.

Exit codes:
    0  — no change detected (CI can skip full pipeline)
    1  — at least one source has changed (CI should run full pipeline)
    2  — at least one source returned an error or a suspiciously low count
         (CI should fail the job rather than silently skip or run on bad data)

Usage:
    python pipeline/check_updates.py           # check all sources
    python pipeline/check_updates.py --update  # also write new counts to checksums.json
"""

import argparse
import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = REPO_ROOT / "pipeline" / "sources.yml"
CHECKSUMS_FILE = REPO_ROOT / "data" / "checksums.json"

REQUEST_TIMEOUT = 30
# If a new count is less than this fraction of the stored count, treat it as
# a suspicious result rather than a legitimate update (avoids running the
# pipeline on a temporarily empty or broken WFS response).
SUSPICIOUS_DROP_THRESHOLD = 0.10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def wfs_hits(session: requests.Session, url: str, layer: str) -> int:
    """Return total feature count via resultType=hits."""
    params = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": layer,
        "resultType": "hits",
    }
    r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    m = re.search(r'numberMatched="(\d+)"', r.text)
    if m:
        return int(m.group(1))
    m = re.search(r'numberReturned="(\d+)"', r.text)
    if m:
        return int(m.group(1))
    raise ValueError(f"Could not parse feature count from:\n{r.text[:300]}")


def load_checksums() -> dict:
    if CHECKSUMS_FILE.exists():
        return json.loads(CHECKSUMS_FILE.read_text())
    return {}


def save_checksums(data: dict) -> None:
    CHECKSUMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKSUMS_FILE.write_text(json.dumps(data, indent=2) + "\n")
    log.info("Updated %s", CHECKSUMS_FILE)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--update",
        action="store_true",
        help="Write new counts to checksums.json after checking.",
    )
    p.add_argument(
        "--source",
        default=None,
        help="Check only this source key (default: all sources).",
    )
    args = p.parse_args()

    with open(SOURCES_FILE) as f:
        sources = yaml.safe_load(f)

    if args.source:
        if args.source not in sources:
            log.error("Unknown source '%s'. Available: %s", args.source, list(sources))
            sys.exit(2)
        sources = {args.source: sources[args.source]}

    checksums = load_checksums()
    now_iso = datetime.now(UTC).isoformat()

    # ── Check each source ─────────────────────────────────────────────────
    rows = []  # (key, old_count, new_count, status)  status: ok|changed|error|suspicious
    changed_any = False
    error_any = False

    with requests.Session() as session:
        for key, cfg in sources.items():
            old_entry = checksums.get(key, {"count": 0, "last_checked": None})
            old_count = old_entry.get("count", 0)

            try:
                new_count = wfs_hits(session, cfg["url"], cfg["layer"])
            except Exception as exc:
                log.error("  [%s] hits request failed: %s", key, exc)
                rows.append((key, old_count, "ERROR", "error"))
                error_any = True
                continue

            # Guard: a new count that is less than 10% of the stored count is
            # almost certainly a broken or empty WFS response, not a real update.
            if old_count > 0 and new_count < old_count * SUSPICIOUS_DROP_THRESHOLD:
                log.error(
                    "  [%s] suspicious count drop: %d → %d (< %.0f%% of stored count)",
                    key,
                    old_count,
                    new_count,
                    SUSPICIOUS_DROP_THRESHOLD * 100,
                )
                rows.append((key, old_count, new_count, "suspicious"))
                error_any = True
                continue

            changed = new_count != old_count
            if changed:
                changed_any = True
            rows.append((key, old_count, new_count, "changed" if changed else "ok"))

            checksums[key] = {
                "count": new_count,
                "last_checked": now_iso,
            }

    # ── Print summary table ───────────────────────────────────────────────
    col = 30
    log.info("─" * 70)
    log.info("%-*s  %10s  %10s  %s", col, "Source", "Old count", "New count", "Status")
    log.info("─" * 70)
    for key, old, new, status in rows:
        label = {
            "ok": "ok",
            "changed": "CHANGED ⚠",
            "error": "ERROR ✗",
            "suspicious": "SUSPICIOUS ✗",
        }.get(status, status)
        log.info("%-*s  %10s  %10s  %s", col, key, old, new, label)
    log.info("─" * 70)

    if error_any:
        log.error("Result: one or more sources returned an error or suspicious count — aborting.")
    elif changed_any:
        log.info("Result: CHANGES DETECTED — full pipeline run needed.")
    else:
        log.info("Result: no changes detected — pipeline can be skipped.")

    # ── Optionally persist new counts ────────────────────────────────────
    # Only write back counts that passed validation (suspicious/error rows
    # were skipped via 'continue' above and are not in checksums).
    if args.update and not error_any:
        save_checksums(checksums)

    if error_any:
        sys.exit(2)
    sys.exit(1 if changed_any else 0)


if __name__ == "__main__":
    main()
