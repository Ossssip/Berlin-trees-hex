"""
pipeline/build_tiles.py
-----------------------
Convert processed GeoParquet files into a single PMTiles file.

Strategy: build separate pmtiles files with scoped zoom ranges, then
merge them with tile-join into one berlin_trees.pmtiles.

    hexes          (h3_res6–9 + admin)           z4–z17   hex/admin layers (all zooms)
    forests        (forstbetriebskarte.parquet)  z4–z17   forest stand polygons
    trees          (trees.parquet)               z6–z17   individual tree points

Run:
    conda run -n berlin_trees python pipeline/build_tiles.py
"""

import json
import logging
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

import duckdb
import geopandas as gpd
import numpy as np
from shapely.geometry import MultiPolygon, Polygon

PROCESSED = Path("data/processed")
TILES_INPUT = Path("data/tiles_input")
WEB_PUBLIC = Path("web/public")
OUT_PMTILES = WEB_PUBLIC / "berlin_trees.pmtiles"
OUT_SUMMARY = WEB_PUBLIC / "dataset_summary.json"
ATTRIBUTION = "Senatsverwaltung Berlin, dl-de/by-2-0"

# Columns to retain in the individual-tree layer (keeps tile size down).
# height_m / crown_diameter_m / district were dropped in Phase 2 transform.
TREE_COLS = [
    "tree_uuid",
    "species_latin",
    "species_german",
    "genus_latin",
    "planting_year",
    "tree_age",
    "source",
]

# Forest stand columns useful for map popups.
# The raw parquet has 90 columns (multi-species layer detail); keep the essentials.
FOREST_COLS = [
    "id",
    "lage",  # stand location name
    "bezirk",  # district
    "betrkl",  # age class (Betriebsklasse)
    "grpalter",  # stand age group
    "gis_area",  # area (m²)
    "s1_1_ba",  # dominant species layer 1 (latin abbreviation)
    "s1_1_deuts",  # dominant species layer 1 (german)
    "s1_1_misch",  # mixing proportion (%)
    "s1_1_bhd",  # diameter at breast height (cm)
    "s1_1_hoehe",  # height (m)
]

RAW = Path("data/raw")

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


def _quote(path: Path) -> str:
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


def _parquet_columns(con: duckdb.DuckDBPyConnection, parquet_path: Path) -> list[str]:
    query = f"DESCRIBE SELECT * FROM read_parquet('{_quote(parquet_path)}')"
    return [row[0] for row in con.execute(query).fetchall()]


def write_dataset_summary(
    con: duckdb.DuckDBPyConnection,
    out_path: Path,
    input_paths: list[Path],
) -> None:
    if _is_fresh(out_path, input_paths):
        log.info("Skipping dataset summary → %s (cached)", out_path.name)
        return

    log.info("Building dataset summary → %s", out_path.name)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    trees_path = PROCESSED / "trees.parquet"
    admin_path = PROCESSED / "admin_bezirke.parquet"

    tree_count, genus_count = con.execute(
        f"""
        SELECT
            COUNT(*) AS tree_count,
            COUNT(DISTINCT genus) AS genus_count
        FROM (
            SELECT COALESCE(
                NULLIF(NULLIF(LOWER(TRIM(genus_latin)), ''), 'unbekannt'),
                NULLIF(LOWER(SPLIT_PART(TRIM(species_latin), ' ', 1)), '')
            ) AS genus
            FROM read_parquet('{_quote(trees_path)}')
        )
        """
    ).fetchone()

    top_genera = [
        {"genus": genus, "count": count, "share": share}
        for genus, count, share in con.execute(
            f"""
            WITH genus_counts AS (
                SELECT
                    COALESCE(
                        NULLIF(NULLIF(LOWER(TRIM(genus_latin)), ''), 'unbekannt'),
                        NULLIF(LOWER(SPLIT_PART(TRIM(species_latin), ' ', 1)), '')
                    ) AS genus,
                    COUNT(*) AS genus_count
                FROM read_parquet('{_quote(trees_path)}')
                WHERE COALESCE(
                    NULLIF(NULLIF(LOWER(TRIM(genus_latin)), ''), 'unbekannt'),
                    NULLIF(LOWER(SPLIT_PART(TRIM(species_latin), ' ', 1)), '')
                ) IS NOT NULL
                GROUP BY 1
            )
            SELECT
                genus,
                genus_count,
                ROUND(genus_count * 100.0 / SUM(genus_count) OVER (), 1) AS genus_share
            FROM genus_counts
            WHERE genus != ''
            ORDER BY genus_count DESC, genus ASC
            LIMIT 10
            """
        ).fetchall()
    ]

    top_genera_total_share = sum(g["share"] for g in top_genera)
    top_genera_other_count = tree_count - sum(g["count"] for g in top_genera)
    top_genera_other_share = round(max(0.0, 100.0 - top_genera_total_share), 1)

    source_rows = con.execute(
        f"""
        SELECT source, COUNT(*) AS n
        FROM read_parquet('{_quote(trees_path)}')
        GROUP BY source
        ORDER BY n DESC, source ASC
        """
    ).fetchall()

    berlin_area_km2, forest_area_km2, non_forest_area_km2, district_count = con.execute(
        f"""
        SELECT
            ROUND(SUM(berlin_area_km2), 2) AS berlin_area_km2,
            ROUND(SUM(forest_area_km2), 2) AS forest_area_km2,
            ROUND(SUM(non_forest_area_km2), 2) AS non_forest_area_km2,
            COUNT(*) AS district_count
        FROM read_parquet('{_quote(admin_path)}')
        """
    ).fetchone()

    subdistrict_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{_quote(PROCESSED / 'admin_ortsteile.parquet')}')"
    ).fetchone()[0]

    density_ranges = {}
    for res in [6, 7, 8, 9]:
        p = PROCESSED / f"h3_res{res}.parquet"
        if p.exists():
            import pandas as pd

            d = pd.read_parquet(p)["tree_density_km2"].dropna()
            density_ranges[f"res{res}"] = {
                "p50": int(np.percentile(d, 50)),
                "p95": int(np.percentile(d, 95)),
            }
    for admin in ["bezirke", "ortsteile"]:
        p = PROCESSED / f"admin_{admin}.parquet"
        if p.exists():
            import pandas as pd

            d = pd.read_parquet(p)["tree_density_km2"].dropna()
            density_ranges[admin] = {
                "p50": int(np.percentile(d, 50)),
                "p95": int(np.percentile(d, 95)),
            }

    summary = {
        "tree_count": int(tree_count),
        "genus_count": int(genus_count),
        "district_count": int(district_count),
        "subdistrict_count": int(subdistrict_count),
        "berlin_area_km2": float(berlin_area_km2),
        "forest_area_km2": float(forest_area_km2),
        "non_forest_area_km2": float(non_forest_area_km2),
        "forest_cover_pct": round((forest_area_km2 / berlin_area_km2 * 100.0), 1)
        if berlin_area_km2
        else 0.0,
        "tree_density_km2": round((tree_count / non_forest_area_km2), 1)
        if non_forest_area_km2
        else None,
        "sources": [{"source": source, "count": int(count)} for source, count in source_rows],
        "top_genera": top_genera,
        "top_genera_other_count": int(top_genera_other_count),
        "top_genera_other_share": top_genera_other_share,
        "density_ranges": density_ranges,
        "notes": {
            "density": "Registered trees per non-forest km²",
            "forest": "Forest share from clipped forest-stand polygons",
        },
    }

    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


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


def parquet_to_flatgeobuf(
    con: duckdb.DuckDBPyConnection,
    parquet_path: Path,
    out_path: Path,
    cols: list[str] | None = None,
) -> None:
    available = _parquet_columns(con, parquet_path)
    selected = cols if cols else [col for col in available if col != "geometry"]
    keep = [col for col in selected if col in available and col != "geometry"]
    projection = ", ".join(keep + ["geometry"])
    select_sql = f"SELECT {projection} FROM read_parquet('{_quote(parquet_path)}')"
    _copy_query_to_flatgeobuf(con, out_path, [parquet_path], select_sql, parquet_path.name)


def centroid_parquet_to_flatgeobuf(
    con: duckdb.DuckDBPyConnection,
    parquet_path: Path,
    out_path: Path,
) -> None:
    columns = [col for col in _parquet_columns(con, parquet_path) if col != "geometry"]
    projection = ", ".join(columns + ["ST_Centroid(geometry) AS geometry"])
    select_sql = f"SELECT {projection} FROM read_parquet('{_quote(parquet_path)}')"
    _copy_query_to_flatgeobuf(
        con, out_path, [parquet_path], select_sql, f"{parquet_path.name} centroids"
    )


def geopandas_to_vector(
    out_path: Path,
    input_paths: list[Path],
    builder,
    label: str,
    driver: str = "FlatGeobuf",
) -> None:
    if _is_fresh(out_path, input_paths):
        log.info("Skipping %s → %s (cached)", label, out_path.name)
        return

    log.info("Building %s → %s (%s)", label, out_path.name, driver)
    with _timed(f"Build {out_path.name}"):
        gdf = builder()
        gdf.to_file(out_path, driver=driver)
    mb = out_path.stat().st_size / 1024 / 1024
    log.info("  %d features, %.1f MB", len(gdf), mb)


def _as_multipolygon(geometry):
    if geometry is None or geometry.is_empty:
        return geometry
    if isinstance(geometry, Polygon):
        return MultiPolygon([geometry])
    return geometry


def _normalize_multipolygons(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    normalized = gdf.copy()
    normalized.geometry = normalized.geometry.map(_as_multipolygon)
    return normalized


def tippecanoe(
    output: Path,
    min_zoom: int,
    max_zoom: int,
    layers: list[tuple[str, Path]],
    extra: list[str] | None = None,
    read_parallel: bool = False,
) -> None:
    """Run tippecanoe for one zoom band.

    layers: [(layer_name, vector_path), ...]  — multiple entries with the same
            name are merged into one vector layer by tippecanoe.
    """
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


def _inject_summary_into_pmtiles(pmtiles_path: Path, summary_path: Path) -> None:
    """Inject dataset_summary JSON into the PMTiles metadata block."""
    tmp = pmtiles_path.with_suffix(".meta.json")
    try:
        result = subprocess.run(
            ["pmtiles", "show", "--metadata", str(pmtiles_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        meta = json.loads(result.stdout)
        meta["dataset_summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        subprocess.run(
            ["pmtiles", "edit", f"--metadata={tmp}", str(pmtiles_path)],
            capture_output=True,
            check=True,
        )
        log.info("Injected dataset_summary into PMTiles metadata")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log.warning("Could not inject metadata into PMTiles: %s", e)
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    TILES_INPUT.mkdir(parents=True, exist_ok=True)
    WEB_PUBLIC.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("LOAD spatial")

    write_dataset_summary(
        con,
        OUT_SUMMARY,
        [
            PROCESSED / "trees.parquet",
            PROCESSED / "admin_bezirke.parquet",
            PROCESSED / "admin_ortsteile.parquet",
        ],
    )

    # --- 1. Parquet → vector export -------------------------------------------
    for res in [6, 7, 8, 9]:
        parquet_to_flatgeobuf(
            con,
            PROCESSED / f"h3_res{res}.parquet",
            TILES_INPUT / f"h3_res{res}.fgb",
        )

    # Admin boundary layers: bezirke and ortsteile (written in WGS84 by aggregate_admin.py)
    parquet_to_flatgeobuf(
        con,
        PROCESSED / "admin_bezirke.parquet",
        TILES_INPUT / "admin_bezirke.fgb",
    )
    parquet_to_flatgeobuf(
        con,
        PROCESSED / "admin_ortsteile.parquet",
        TILES_INPUT / "admin_ortsteile.fgb",
    )

    # Admin centroid point layers — one point per admin area carrying all properties.
    # Symbol layers use these so each area gets exactly one icon regardless of how
    # the polygon is clipped across tile boundaries (same fix as hex centroid layers).
    for admin in ["bezirke", "ortsteile"]:
        centroid_parquet_to_flatgeobuf(
            con,
            PROCESSED / f"admin_{admin}.parquet",
            TILES_INPUT / f"admin_{admin}_centroids.fgb",
        )

    parquet_to_flatgeobuf(
        con,
        PROCESSED / "trees.parquet",
        TILES_INPUT / "trees.fgb",
        cols=TREE_COLS,
    )

    # City border: single polygon dissolved from all Bezirke.
    geopandas_to_vector(
        TILES_INPUT / "berlin_border.fgb",
        [PROCESSED / "admin_bezirke.parquet"],
        lambda: gpd.GeoDataFrame(
            geometry=gpd.GeoSeries(
                [gpd.read_parquet(PROCESSED / "admin_bezirke.parquet").union_all()],
                crs="EPSG:4326",
            )
        ),
        "Berlin city border",
    )

    forest_inputs = [RAW / "forstbetriebskarte.parquet", RAW / "alkis_ortsteile.parquet"]
    forests_out = TILES_INPUT / "forests.geojson"
    forests_union_out = TILES_INPUT / "forests_union.geojson"
    need_city_boundary = not (
        _is_fresh(forests_out, forest_inputs) and _is_fresh(forests_union_out, forest_inputs)
    )
    city_boundary = None
    if need_city_boundary:
        # City boundary: dissolve all Ortsteile polygons into one polygon (EPSG:25833).
        # Used to clip forest layers so they don't bleed past the administrative border.
        log.info("Building city boundary ...")
        gdf_ortsteile = gpd.read_parquet(RAW / "alkis_ortsteile.parquet")
        city_boundary = gdf_ortsteile.union_all()  # already EPSG:25833

    # Forest individual stands: clip raw polygons to city boundary before export.
    geopandas_to_vector(
        forests_out,
        forest_inputs,
        lambda: (
            lambda gdf_forest: _normalize_multipolygons(
                gdf_forest[[c for c in FOREST_COLS if c in gdf_forest.columns] + ["geometry"]]
            ).to_crs("EPSG:4326")
        )(gpd.clip(gpd.read_parquet(RAW / "forstbetriebskarte.parquet"), city_boundary)),
        "forest stands",
        driver="GeoJSON",
    )

    # Forest union: morphological closing (buffer +50 m → merge → buffer −50 m)
    # merges stands within ~100 m of each other and smooths jagged edges.
    # Result is then intersected with city boundary to remove any buffer overshoot.
    geopandas_to_vector(
        forests_union_out,
        forest_inputs,
        lambda: (
            lambda smoothed: _normalize_multipolygons(
                gpd.GeoDataFrame(
                    geometry=gpd.GeoSeries([smoothed], crs="EPSG:25833").explode(index_parts=False),
                    crs="EPSG:25833",
                )
            ).to_crs("EPSG:4326")
        )(
            gpd.clip(gpd.read_parquet(RAW / "forstbetriebskarte.parquet"), city_boundary)
            .geometry.buffer(50)
            .union_all()
            .buffer(-50)
            .intersection(city_boundary)
        ),
        "forest union",
        driver="GeoJSON",
    )

    # Centroid point layers: one point per hex, carrying all hex properties.
    # Symbol layers in MapLibre use these instead of the polygon layer so each
    # hex gets exactly one symbol — polygon features clipped across tile
    # boundaries generate multiple centroids, causing duplicate icons.
    for res in [6, 7, 8, 9]:
        centroid_parquet_to_flatgeobuf(
            con,
            PROCESSED / f"h3_res{res}.parquet",
            TILES_INPUT / f"h3_res{res}_centroids.fgb",
        )

    # --- 2. tippecanoe: all bands in parallel ---------------------------------
    tmp_dir = Path(tempfile.mkdtemp(prefix="bt_tiles_", dir="/dev/shm"))
    try:
        hexes_pmtiles = tmp_dir / "hexes.pmtiles"
        hex_centroids_pmtiles = tmp_dir / "hex_centroids.pmtiles"
        admin_pmtiles = tmp_dir / "admin.pmtiles"
        admin_centroids_pmtiles = tmp_dir / "admin_centroids.pmtiles"
        forests_pmtiles = tmp_dir / "forests.pmtiles"
        trees_pmtiles = tmp_dir / "trees.pmtiles"

        # Each job is independent — different inputs, different output paths, all
        # writing to /dev/shm. ThreadPoolExecutor is sufficient because each job
        # is a subprocess (tippecanoe), so there is no GIL contention.
        tippecanoe_jobs = {
            "hexes": lambda: tippecanoe(
                hexes_pmtiles,
                min_zoom=4,
                max_zoom=17,
                layers=[
                    (f"hexes_res{res}", TILES_INPUT / f"h3_res{res}.fgb") for res in [6, 7, 8, 9]
                ],
                extra=["--no-tile-size-limit"],
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
                extra=["--no-feature-limit", "--no-tile-size-limit", "--drop-rate=0"],
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
                ],
                extra=["--no-tile-size-limit", "--no-simplification-of-shared-nodes"],
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
                extra=["--no-feature-limit", "--no-tile-size-limit", "--drop-rate=0"],
                read_parallel=True,
            ),
            "forests": lambda: tippecanoe(
                forests_pmtiles,
                min_zoom=4,
                max_zoom=17,
                layers=[
                    ("forests", forests_out),
                    ("forests_union", forests_union_out),
                ],
                extra=["--no-tile-size-limit", "--no-simplification-of-shared-nodes"],
                read_parallel=True,
            ),
            "trees": lambda: tippecanoe(
                trees_pmtiles,
                min_zoom=6,
                max_zoom=17,
                layers=[("trees", TILES_INPUT / "trees.fgb")],
                extra=["--no-tile-size-limit", "--drop-densest-as-needed"],
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

        # --- 3. tile-join: merge all bands into one pmtiles -------------------
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

        # Inject dataset_summary (including density_ranges) into PMTiles metadata.
        # Saves one HTTP request — the web app reads it via p.getMetadata().
        _inject_summary_into_pmtiles(assembled_pmtiles, OUT_SUMMARY)

        final_copy_tmp = OUT_PMTILES.with_name(f"{OUT_PMTILES.name}.tmp")
        with _timed(f"Copy final PMTiles to {OUT_PMTILES}"):
            WEB_PUBLIC.mkdir(parents=True, exist_ok=True)
            shutil.copy2(assembled_pmtiles, final_copy_tmp)
            final_copy_tmp.replace(OUT_PMTILES)
            OUT_PMTILES.with_name(f"{OUT_PMTILES.name}-journal").unlink(missing_ok=True)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        con.close()

    # --- 4. Validate ----------------------------------------------------------
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
