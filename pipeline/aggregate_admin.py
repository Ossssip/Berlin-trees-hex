"""
pipeline/aggregate_admin.py
---------------------------
Aggregate individual trees into administrative boundary polygons:
  - Bezirke  (12 districts)
  - Ortsteile (97 sub-districts)

For each layer, outputs one GeoParquet file in EPSG:4326:
    data/processed/admin_bezirke.parquet
    data/processed/admin_ortsteile.parquet

Per-area statistics (mirrors aggregate_h3.py):
    tree_count, tree_density_km2,
    berlin_area_km2, forest_area_km2, non_forest_area_km2,
    dominant_genus, genus_count,
    dominant_species (cultivar-stripped), species_count, dominant_species_pct,
    source_strassenbaeume, source_anlagenbaeume, source_gruen_berlin,
    forest_cover_pct,
    forest_genus_1..5, forest_genus_1..5_share, forest_genus_other_share,
    <id columns>, geometry (WGS84 polygon)

Requires dbt models stg_bezirke and stg_ortsteile_detail to exist in the DB
(run `dbt run` first via the transform stage).

Run:
    conda run -n berlin_trees python pipeline/aggregate_admin.py
"""

import logging
import os
import sys
from pathlib import Path

import duckdb
import geopandas as gpd

sys.path.insert(0, str(Path(__file__).parent))
from aggregate_shared import (
    forest_histogram_ctes,
    forest_intersections_cte,
    tree_genus_histogram_ctes,
)

DB_PATH = "data/berlin_trees.duckdb"
OUT_DIR = Path("data/processed")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------


def _trees_agg_sql(admin_table: str, id_col: str, name_col: str) -> str:
    """
    Spatial join trees → admin polygons and compute per-area statistics.

    Trees and admin geometries are both in EPSG:25833 (UTM 33N), so the join
    is metric-accurate without any reprojection.

    The output geometry column is converted to standard (lng, lat) WKT so that
    geopandas can reconstruct it as EPSG:4326.
    """
    tree_genus_hist = tree_genus_histogram_ctes("tree_admin", "area_id")
    return f"""
    WITH trees_coords AS (
        SELECT
            geometry,
            COALESCE(
                NULLIF(NULLIF(LOWER(TRIM(genus_latin)), ''), 'unbekannt'),
                NULLIF(LOWER(SPLIT_PART(TRIM(species_latin), ' ', 1)), '')
            ) AS genus,
            LOWER(TRIM(SPLIT_PART(COALESCE(TRIM(species_latin), ''), '''', 1))) AS species,
            source
        FROM int_trees_unified
    ),
    tree_admin AS (
        SELECT
            a.{id_col}  AS area_id,
            t.genus,
            t.species,
            t.source
        FROM trees_coords t
        JOIN {admin_table} a ON ST_Within(t.geometry, a.geometry)
    ),
    agg AS (
        SELECT
            area_id,
            COUNT(*)                                                    AS tree_count,
            mode(genus)                                                 AS dominant_genus,
            COUNT(DISTINCT genus)                                       AS genus_count,
            mode(NULLIF(species, ''))                                   AS dominant_species,
            COUNT(DISTINCT NULLIF(species, ''))                         AS species_count,
            SUM(CASE WHEN source = 'strassenbaeume' THEN 1 ELSE 0 END) AS source_strassenbaeume,
            SUM(CASE WHEN source = 'anlagenbaeume'  THEN 1 ELSE 0 END) AS source_anlagenbaeume,
            SUM(CASE WHEN source = 'gruen_berlin'   THEN 1 ELSE 0 END) AS source_gruen_berlin
        FROM tree_admin
        GROUP BY area_id
    ),
    species_counts AS (
        SELECT area_id, NULLIF(species, '') AS species, COUNT(*) AS n
        FROM tree_admin
        WHERE species IS NOT NULL AND species != ''
        GROUP BY area_id, species
    ),
    dominant_n AS (
        SELECT area_id, MAX(n) AS max_n
        FROM species_counts
        GROUP BY area_id
    ),
    {tree_genus_hist}
    SELECT
        a.{id_col}                                                          AS area_id,
        a.{name_col}                                                        AS area_name,
        b.tree_count,
        b.dominant_genus,
        b.genus_count,
        b.dominant_species,
        b.species_count,
        ROUND(100.0 * d.max_n / b.tree_count, 1)                          AS dominant_species_pct,
        b.source_strassenbaeume,
        b.source_anlagenbaeume,
        b.source_gruen_berlin,
        tg.tree_genus_1,
        tg.tree_genus_1_count,
        tg.tree_genus_1_share,
        tg.tree_genus_2,
        tg.tree_genus_2_count,
        tg.tree_genus_2_share,
        tg.tree_genus_3,
        tg.tree_genus_3_count,
        tg.tree_genus_3_share,
        tg.tree_genus_4,
        tg.tree_genus_4_count,
        tg.tree_genus_4_share,
        tg.tree_genus_5,
        tg.tree_genus_5_count,
        tg.tree_genus_5_share,
        tg.tree_genus_6,
        tg.tree_genus_6_count,
        tg.tree_genus_6_share,
        tg.tree_genus_7,
        tg.tree_genus_7_count,
        tg.tree_genus_7_share,
        tg.tree_genus_8,
        tg.tree_genus_8_count,
        tg.tree_genus_8_share,
        tg.tree_genus_9,
        tg.tree_genus_9_count,
        tg.tree_genus_9_share,
        tg.tree_genus_10,
        tg.tree_genus_10_count,
        tg.tree_genus_10_share,
        tg.tree_genus_other_count,
        tg.tree_genus_other_share,
        -- Geometry: EPSG:25833 → EPSG:4326, then flip to standard (lng, lat) WKT
        ST_AsText(
            ST_FlipCoordinates(ST_Transform(a.geometry, 'EPSG:25833', 'EPSG:4326'))
        ) AS wkt
    FROM {admin_table} a
    JOIN agg b          ON a.{id_col} = b.area_id
    LEFT JOIN dominant_n d ON a.{id_col} = d.area_id
    LEFT JOIN tree_genus_histogram tg ON a.{id_col} = tg.area_id
    """


def _area_metrics_sql(admin_table: str, id_col: str) -> str:
    """
    Per-admin-area Berlin/forest area metrics plus Hauptbestand genus histogram.

    Admin polygons are already inside Berlin, so berlin_area_km2 is simply the
    polygon area. The density denominator is non-forest Berlin area:

        non_forest_area_km2 = admin_area_km2 - forest_area_km2

    forest_cover_pct is the share of each admin polygon covered by forest.
    forest_genus_1..5 / _share are area-weighted Hauptbestand composition (top 5).
    """
    fi = forest_intersections_cte("admin_geoms", "area_id")
    hist = forest_histogram_ctes("area_id")
    return f"""
    WITH admin_geoms AS (
        SELECT {id_col} AS area_id, geometry FROM {admin_table}
    ),
    -- Single spatial join against stg_waelder; shared by forest totals, dominant_forest, histogram
    {fi},
    forest_totals AS (
        SELECT area_id, SUM(inter_area_m2) AS forest_area_m2
        FROM forest_intersections
        GROUP BY area_id
    ),
    {hist}
    SELECT
        a.{id_col} AS area_id,
        ROUND(ST_Area(a.geometry) / 1e6, 4) AS berlin_area_km2,
        ROUND(COALESCE(ft.forest_area_m2, 0.0) / 1e6, 4) AS forest_area_km2,
        ROUND(GREATEST(ST_Area(a.geometry) - COALESCE(ft.forest_area_m2, 0.0), 0.0) / 1e6, 4)
            AS non_forest_area_km2,
        ROUND(
            CASE
                WHEN ST_Area(a.geometry) > 0
                    THEN LEAST(COALESCE(ft.forest_area_m2, 0.0) / ST_Area(a.geometry) * 100.0, 100.0)
                ELSE 0.0
            END,
            1
        ) AS forest_cover_pct,
        fh.forest_genus_1,
        fh.forest_genus_1_share,
        fh.forest_genus_2,
        fh.forest_genus_2_share,
        fh.forest_genus_3,
        fh.forest_genus_3_share,
        fh.forest_genus_4,
        fh.forest_genus_4_share,
        fh.forest_genus_5,
        fh.forest_genus_5_share,
        fh.forest_genus_other_share
    FROM {admin_table} a
    LEFT JOIN forest_totals ft    ON a.{id_col} = ft.area_id
    LEFT JOIN forest_histogram fh ON a.{id_col} = fh.area_id
    """


# ---------------------------------------------------------------------------
# Main aggregation
# ---------------------------------------------------------------------------


def aggregate_admin(
    con: duckdb.DuckDBPyConnection,
    admin_table: str,
    id_col: str,
    name_col: str,
    label: str,
) -> gpd.GeoDataFrame:
    log.info("%s — aggregating trees ...", label)
    trees_df = con.execute(_trees_agg_sql(admin_table, id_col, name_col)).df()
    log.info("  %d areas, %d trees total", len(trees_df), trees_df["tree_count"].sum())

    log.info("%s — computing Berlin/forest area metrics ...", label)
    area_df = con.execute(_area_metrics_sql(admin_table, id_col)).df()
    log.info("  %d areas with area metrics", len(area_df))

    df = trees_df.merge(area_df, on="area_id", how="left")
    for col in ["berlin_area_km2", "forest_area_km2", "non_forest_area_km2"]:
        df[col] = df[col].fillna(0.0)
    df["forest_cover_pct"] = df["forest_cover_pct"].fillna(0.0)
    df["tree_density_km2"] = (df["tree_count"] / df["non_forest_area_km2"]).round(1)
    df["tree_density_km2"] = df["tree_density_km2"].where(df["non_forest_area_km2"] > 0)

    # Reconstruct geometry from WKT (standard lng,lat = EPSG:4326)
    geoms = gpd.GeoSeries.from_wkt(df["wkt"])
    gdf = gpd.GeoDataFrame(
        df.drop(columns=["wkt"]),
        geometry=gpd.GeoSeries(geoms, crs="EPSG:4326"),
    )

    return gdf


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    log.info("Connecting to %s", DB_PATH)
    con = duckdb.connect(DB_PATH, read_only=True)
    con.execute("LOAD spatial; LOAD h3;")

    layers = [
        # (dbt_table,            id_col,          name_col,       label,       out_file)
        ("stg_bezirke", "bezirk_code", "bezirk_name", "Bezirke", "admin_bezirke.parquet"),
        (
            "stg_ortsteile_detail",
            "ortsteil_code",
            "ortsteil_name",
            "Ortsteile",
            "admin_ortsteile.parquet",
        ),
    ]

    for table, id_col, name_col, label, fname in layers:
        gdf = aggregate_admin(con, table, id_col, name_col, label)
        out_path = OUT_DIR / fname
        gdf.to_parquet(out_path, index=False)
        log.info(
            "%s — wrote %s  (%d areas, %.2f MB)",
            label,
            out_path,
            len(gdf),
            out_path.stat().st_size / 1024 / 1024,
        )

    log.info("Done.")


if __name__ == "__main__":
    main()
