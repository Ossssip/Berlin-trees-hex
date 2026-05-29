"""
pipeline/build_tiles.py
-----------------------
Convert dbt mart tables in berlin_trees.duckdb into a single PMTiles file.

Strategy: build separate pmtiles files with scoped zoom ranges, then
merge them with tile-join into one berlin_trees.pmtiles.

    hexes          (agg_h3_res6–9 + agg_bezirke/ortsteile)  z4–z17
    forests        (agg_forests + agg_forest_union)          z4–z17
    trees          (int_trees_unified)                       z6–z17

Run:
    conda run -n berlin_trees python pipeline/build_tiles.py
"""

import logging
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

import duckdb

DB_PATH = Path("data/berlin_trees.duckdb")
TILES_INPUT = Path("data/tiles_input")
WEB_PUBLIC = Path("web/public")
OUT_PMTILES = WEB_PUBLIC / "berlin_trees.pmtiles"
ATTRIBUTION = "Senatsverwaltung Berlin (dl-de/zero-2-0), Grün Berlin GmbH (dl-de/by-2-0)"

# Columns to retain in the individual-tree layer (keeps tile size down).
TREE_COLS = [
    "tree_uuid",
    "species_latin",
    "species_german",
    "genus_latin",
    "planting_year",
    "tree_age",
    "source",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str]) -> None:
    log.info("$ %s", " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            log.info("  %s", line)
    if result.stderr:
        for line in result.stderr.strip().splitlines():
            log.info("  %s", line)
    result.check_returncode()


@contextmanager
def _timed(label: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        log.info("%s finished in %.1fs", label, time.perf_counter() - start)


def _quote(path) -> str:
    return str(path).replace("'", "''")


def _is_fresh(output_path: Path, input_paths: list[Path]) -> bool:
    if not output_path.exists():
        return False
    out_mtime = output_path.stat().st_mtime
    for input_path in input_paths:
        if not input_path.exists():
            return False
        if input_path.stat().st_mtime > out_mtime:
            return False
    return True


def _copy_query_to_flatgeobuf(
    con: duckdb.DuckDBPyConnection,
    out_path: Path,
    input_paths: list[Path],
    select_sql: str,
    label: str,
) -> None:
    if _is_fresh(out_path, input_paths):
        log.info("Skipping %s → %s (cached)", label, out_path.name)
        return

    log.info("Exporting %s → %s (FlatGeobuf)", label, out_path.name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with _timed(f"Export {out_path.name}"):
        con.execute(
            f"""
            COPY ({select_sql})
            TO '{_quote(out_path)}'
            WITH (FORMAT GDAL, DRIVER 'FlatGeobuf')
            """
        )
    mb = out_path.stat().st_size / 1024 / 1024
    log.info("  %.1f MB", mb)


def _col_projection(col_defs: list[tuple], exclude: set[str], geom_expr: str) -> str:
    """Build a SELECT projection, casting HUGEINT → BIGINT (FlatGeobuf supports ≤64-bit ints)."""
    parts = []
    for name, dtype in col_defs:
        if name in exclude:
            continue
        if dtype == "HUGEINT":
            parts.append(f"CAST({name} AS BIGINT) AS {name}")
        else:
            parts.append(name)
    parts.append(geom_expr)
    return ", ".join(parts)


def table_to_flatgeobuf(
    con: duckdb.DuckDBPyConnection,
    table: str,
    out_path: Path,
    cols: list[str] | None = None,
) -> None:
    col_defs = con.execute(f"DESCRIBE {table}").fetchall()
    if cols:
        col_set = set(cols)
        col_defs = [(n, t) for n, t, *_ in col_defs if n in col_set and n != "geometry"]
    else:
        col_defs = [(n, t) for n, t, *_ in col_defs if n != "geometry"]
    projection = _col_projection(col_defs, set(), "geometry")
    select_sql = f"SELECT {projection} FROM {table}"
    _copy_query_to_flatgeobuf(con, out_path, [DB_PATH], select_sql, table)


def table_centroids_to_flatgeobuf(
    con: duckdb.DuckDBPyConnection,
    table: str,
    out_path: Path,
) -> None:
    col_defs = [
        (n, t) for n, t, *_ in con.execute(f"DESCRIBE {table}").fetchall() if n != "geometry"
    ]
    projection = _col_projection(col_defs, set(), "ST_Centroid(geometry) AS geometry")
    select_sql = f"SELECT {projection} FROM {table}"
    _copy_query_to_flatgeobuf(con, out_path, [DB_PATH], select_sql, f"{table} centroids")


def tippecanoe(
    output: Path,
    min_zoom: int,
    max_zoom: int,
    layers: list[tuple[str, Path]],
    extra: list[str] | None = None,
    read_parallel: bool = False,
) -> None:
    cmd = [
        "tippecanoe",
        "--output",
        str(output),
        "--force",
        f"--minimum-zoom={min_zoom}",
        f"--maximum-zoom={max_zoom}",
        "--no-feature-limit",
    ]
    if read_parallel:
        cmd.append("--read-parallel")
    if extra:
        cmd.extend(extra)
    for name, path in layers:
        cmd.append(f"--named-layer={name}:{path}")
    with _timed(f"tippecanoe {output.name}"):
        _run(cmd)



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    TILES_INPUT.mkdir(parents=True, exist_ok=True)
    WEB_PUBLIC.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(DB_PATH), read_only=True)
    con.execute("LOAD spatial; LOAD h3;")

    # --- 1. DuckDB mart tables → FlatGeobuf -----------------------------------

    for res in [6, 7, 8, 9]:
        table_to_flatgeobuf(con, f"agg_h3_res{res}", TILES_INPUT / f"h3_res{res}.fgb")

    table_to_flatgeobuf(con, "agg_bezirke", TILES_INPUT / "admin_bezirke.fgb")
    table_to_flatgeobuf(con, "agg_ortsteile", TILES_INPUT / "admin_ortsteile.fgb")
    table_to_flatgeobuf(con, "agg_berlin", TILES_INPUT / "berlin_summary.fgb")

    for admin in ["bezirke", "ortsteile"]:
        table_centroids_to_flatgeobuf(
            con, f"agg_{admin}", TILES_INPUT / f"admin_{admin}_centroids.fgb"
        )

    # Individual tree points from int_trees_unified (column subset to keep tiles small)
    tree_cols_sql = ", ".join(
        TREE_COLS
        + ["ST_FlipCoordinates(ST_Transform(geometry, 'EPSG:25833', 'EPSG:4326')) AS geometry"]
    )
    _copy_query_to_flatgeobuf(
        con,
        TILES_INPUT / "trees.fgb",
        [DB_PATH],
        f"SELECT {tree_cols_sql} FROM int_trees_unified",
        "int_trees_unified",
    )

    # City border: dissolve all Bezirke geometries into one polygon
    _copy_query_to_flatgeobuf(
        con,
        TILES_INPUT / "berlin_border.fgb",
        [DB_PATH],
        "SELECT ST_Union_Agg(geometry) AS geometry FROM agg_bezirke",
        "Berlin city border",
    )

    # Hex centroid point layers
    for res in [6, 7, 8, 9]:
        table_centroids_to_flatgeobuf(
            con, f"agg_h3_res{res}", TILES_INPUT / f"h3_res{res}_centroids.fgb"
        )

    # --- 2. Forest layers (DuckDB mart tables) ----------------------------------

    table_to_flatgeobuf(con, "agg_forests", TILES_INPUT / "forests.fgb")
    table_to_flatgeobuf(con, "agg_forest_union", TILES_INPUT / "forests_union.fgb")

    # --- 3. tippecanoe: all bands in parallel ---------------------------------
    tmp_dir = Path(tempfile.mkdtemp(prefix="bt_tiles_", dir="/dev/shm"))
    try:
        hexes_pmtiles = tmp_dir / "hexes.pmtiles"
        hex_centroids_pmtiles = tmp_dir / "hex_centroids.pmtiles"
        admin_pmtiles = tmp_dir / "admin.pmtiles"
        admin_centroids_pmtiles = tmp_dir / "admin_centroids.pmtiles"
        forests_pmtiles = tmp_dir / "forests.pmtiles"
        trees_pmtiles = tmp_dir / "trees.pmtiles"

        tippecanoe_jobs = {
            "hexes": lambda: tippecanoe(
                hexes_pmtiles,
                min_zoom=4,
                max_zoom=17,
                layers=[
                    (f"hexes_res{res}", TILES_INPUT / f"h3_res{res}.fgb") for res in [6, 7, 8, 9]
                ],
                extra=["--no-tile-size-limit", "--no-tile-stats"],
                read_parallel=True,
            ),
            "hex_centroids": lambda: tippecanoe(
                hex_centroids_pmtiles,
                min_zoom=4,
                max_zoom=17,
                layers=[
                    (f"hexes_res{res}_centroids", TILES_INPUT / f"h3_res{res}_centroids.fgb")
                    for res in [6, 7, 8, 9]
                ],
                extra=["--no-feature-limit", "--no-tile-size-limit", "--drop-rate=0", "--no-tile-stats"],
                read_parallel=True,
            ),
            "admin": lambda: tippecanoe(
                admin_pmtiles,
                min_zoom=4,
                max_zoom=17,
                layers=[
                    ("admin_bezirke", TILES_INPUT / "admin_bezirke.fgb"),
                    ("admin_ortsteile", TILES_INPUT / "admin_ortsteile.fgb"),
                    ("berlin_border", TILES_INPUT / "berlin_border.fgb"),
                    ("agg_berlin", TILES_INPUT / "berlin_summary.fgb"),
                ],
                extra=["--no-tile-size-limit", "--no-simplification-of-shared-nodes", "--no-tile-stats"],
                read_parallel=True,
            ),
            "admin_centroids": lambda: tippecanoe(
                admin_centroids_pmtiles,
                min_zoom=4,
                max_zoom=17,
                layers=[
                    ("admin_bezirke_centroids", TILES_INPUT / "admin_bezirke_centroids.fgb"),
                    ("admin_ortsteile_centroids", TILES_INPUT / "admin_ortsteile_centroids.fgb"),
                ],
                extra=["--no-feature-limit", "--no-tile-size-limit", "--drop-rate=0", "--no-tile-stats"],
                read_parallel=True,
            ),
            "forests": lambda: tippecanoe(
                forests_pmtiles,
                min_zoom=4,
                max_zoom=17,
                layers=[
                    ("forests", TILES_INPUT / "forests.fgb"),
                    ("forests_union", TILES_INPUT / "forests_union.fgb"),
                ],
                extra=["--no-tile-size-limit", "--no-simplification-of-shared-nodes", "--no-tile-stats"],
                read_parallel=True,
            ),
            "trees": lambda: tippecanoe(
                trees_pmtiles,
                min_zoom=6,
                max_zoom=17,
                layers=[("trees", TILES_INPUT / "trees.fgb")],
                extra=["--no-tile-size-limit", "--drop-densest-as-needed", "--no-tile-stats"],
                read_parallel=True,
            ),
        }

        log.info("Running %d tippecanoe jobs in parallel ...", len(tippecanoe_jobs))
        with (
            _timed("tippecanoe (all jobs)"),
            ThreadPoolExecutor(max_workers=len(tippecanoe_jobs)) as executor,
        ):
            futures = {executor.submit(fn): name for name, fn in tippecanoe_jobs.items()}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    raise RuntimeError(f"tippecanoe job '{name}' failed") from exc

        # Fix capitalised Null in tippecanoe JSON metadata before tile-join reads it

        # --- 4. tile-join: merge all bands into one pmtiles -------------------
        assembled_pmtiles = tmp_dir / OUT_PMTILES.name
        log.info("Merging with tile-join in tmpfs → %s", assembled_pmtiles)
        with _timed("tile-join berlin_trees.pmtiles"):
            _run(
                [
                    "tile-join",
                    "--output",
                    str(assembled_pmtiles),
                    "--force",
                    "--no-tile-size-limit",
                    "--attribution",
                    ATTRIBUTION,
                    str(hexes_pmtiles),
                    str(hex_centroids_pmtiles),
                    str(admin_pmtiles),
                    str(admin_centroids_pmtiles),
                    str(forests_pmtiles),
                    str(trees_pmtiles),
                ]
            )

        final_copy_tmp = OUT_PMTILES.with_name(f"{OUT_PMTILES.name}.tmp")
        with _timed(f"Copy final PMTiles to {OUT_PMTILES}"):
            WEB_PUBLIC.mkdir(parents=True, exist_ok=True)
            shutil.copy2(assembled_pmtiles, final_copy_tmp)
            final_copy_tmp.replace(OUT_PMTILES)
            OUT_PMTILES.with_name(f"{OUT_PMTILES.name}-journal").unlink(missing_ok=True)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        con.close()

    # --- 5. Validate ----------------------------------------------------------
    mb = OUT_PMTILES.stat().st_size / 1024 / 1024
    log.info("Output: %s  (%.1f MB)", OUT_PMTILES, mb)
    if mb > 200:
        log.warning(
            "File is %.1f MB — consider reducing attribute payload or capping zoom to z16",
            mb,
        )

    try:
        result = subprocess.run(
            ["pmtiles", "show", str(OUT_PMTILES)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log.info("pmtiles show:\n%s", result.stdout)
    except FileNotFoundError:
        log.info("(pmtiles CLI not found — skipping inspection)")

    log.info("Done.")


if __name__ == "__main__":
    main()
