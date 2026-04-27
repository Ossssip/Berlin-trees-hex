"""
pipeline/aggregate_h3.py
------------------------
Aggregate individual trees into H3 hexagonal cells at resolutions 6–9.

For each resolution, outputs one GeoParquet file:
    data/processed/h3_res{N}.parquet

Per-hex statistics:
    tree_count, tree_density_km2,
    berlin_area_km2, forest_area_km2, non_forest_area_km2,
    dominant_genus, genus_count,
    dominant_species (cultivar-stripped), species_count, dominant_species_pct,
    source_strassenbaeume, source_anlagenbaeume, source_gruen_berlin,
    forest_cover_pct,
    forest_genus_1..5, forest_genus_1..5_share, forest_genus_other_share,
    h3_index (string), geometry (WGS84 polygon)

Pipeline per resolution (temp tables, in dependency order):
    tmp_berlin_boundary   — precomputed once (invariant across resolutions)
    tmp_trees_h3          — int_trees_unified scanned once; H3 assignment + genus/species
    tmp_hex_geoms         — boundary + reproject per distinct cell (once)
    tmp_hex_berlin        — Berlin-clip per hex (once); intersection computed once
    tmp_forest_intersections — stg_waelder spatial join (once); ST_Intersection computed once

Run:
    conda run -n berlin_trees python pipeline/aggregate_h3.py
"""

import logging
import os
import sys
from pathlib import Path

import duckdb
import geopandas as gpd

sys.path.insert(0, str(Path(__file__).parent))
from aggregate_shared import forest_histogram_ctes, tree_genus_histogram_ctes

RESOLUTIONS = [6, 7, 8, 9]
DB_PATH = "data/berlin_trees.duckdb"
OUT_DIR = Path("data/processed")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Invariants — computed once before the resolution loop
# ---------------------------------------------------------------------------


def _precompute_invariants(con: duckdb.DuckDBPyConnection) -> None:
    """
    Materialise geometry objects that do not depend on resolution.
    The Berlin boundary is a single polygon used for hex clipping in every
    resolution; building it once avoids repeated ST_Union_Agg over stg_bezirke.
    """
    log.info("Precomputing invariants (Berlin boundary) ...")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE tmp_berlin_boundary AS
        SELECT ST_Union_Agg(geometry) AS geom FROM stg_bezirke
    """)


# ---------------------------------------------------------------------------
# Per-resolution temp tables
# ---------------------------------------------------------------------------


def _build_resolution_temps(con: duckdb.DuckDBPyConnection, resolution: int) -> None:
    """
    Materialise all per-resolution working tables in dependency order.

    All expensive spatial work lives here; the final query is purely tabular.

    Dependency chain:
        int_trees_unified
            → tmp_trees_h3          (H3 assignment, genus/species — one scan)
            → tmp_hex_geoms         (boundary + reproject per cell — built once)
            → tmp_hex_berlin        (Berlin-clip — ST_Intersection computed once)
            → tmp_forest_intersections (stg_waelder join — ST_Intersection computed once)
    """
    log.info("  [1/4] tmp_trees_h3  (scan int_trees_unified once) ...")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE tmp_trees_h3 AS
        SELECT
            h3_latlng_to_cell(
                -- DuckDB EPSG:4326 is authority axis order: ST_X = lat, ST_Y = lng
                ST_X(ST_Transform(geometry, 'EPSG:25833', 'EPSG:4326')),
                ST_Y(ST_Transform(geometry, 'EPSG:25833', 'EPSG:4326')),
                {resolution}
            ) AS h3_index,
            COALESCE(
                NULLIF(NULLIF(LOWER(TRIM(genus_latin)), ''), 'unbekannt'),
                NULLIF(LOWER(SPLIT_PART(TRIM(species_latin), ' ', 1)), '')
            ) AS genus,
            LOWER(TRIM(SPLIT_PART(COALESCE(TRIM(species_latin), ''), '''', 1))) AS species,
            source
        FROM int_trees_unified
    """)

    log.info("  [2/5] tmp_forest_h3 (fill forest polygons with H3 cells) ...")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE tmp_forest_h3 AS
        -- Expand by 1-ring to capture boundary cells whose center falls just outside the polygon.
        -- Phantom cells (no trees, no actual forest intersection) are filtered in the final query.
        SELECT DISTINCT UNNEST(h3_grid_disk(h3_index, 1)) AS h3_index
        FROM (
            SELECT DISTINCT
                UNNEST(h3_polygon_wkt_to_cells(
                    -- Transform 25833→4326 gives authority axis (lat, lon); flip to get lon/lat WKT
                    ST_AsText(ST_FlipCoordinates(ST_Transform(part.geom, 'EPSG:25833', 'EPSG:4326'))),
                    {resolution}
                )) AS h3_index
            FROM (
                -- h3_polygon_wkt_to_cells only accepts simple POLYGON WKT; explode multi-geoms first
                SELECT UNNEST(ST_Dump(geometry)) AS part
                FROM stg_waelder
                WHERE ST_GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON', 'GEOMETRYCOLLECTION')
            )
            WHERE ST_GeometryType(part.geom) = 'POLYGON'
        )
    """)

    log.info("  [3/5] tmp_hex_geoms (boundary + reproject per distinct cell) ...")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE tmp_hex_geoms AS
        SELECT
            h3_index,
            -- h3_cell_to_boundary_wkt returns (lng, lat) WKT; flip for EPSG:4326 lat-first
            ST_Transform(
                ST_FlipCoordinates(ST_GeomFromText(h3_cell_to_boundary_wkt(h3_index))),
                'EPSG:4326', 'EPSG:25833'
            ) AS geom_25833,
            -- Keep standard (lng, lat) WKT for final GeoDataFrame export — no swap needed
            h3_cell_to_boundary_wkt(h3_index) AS wkt_4326
        FROM (
            SELECT h3_index FROM tmp_trees_h3
            UNION
            SELECT h3_index FROM tmp_forest_h3
        )
    """)

    log.info("  [4/5] tmp_hex_berlin (Berlin-clip; intersection computed once) ...")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE tmp_hex_berlin AS
        SELECT
            h3_index,
            clipped_geom                 AS berlin_geom,
            ST_Area(clipped_geom)        AS berlin_area_m2
        FROM (
            SELECT
                h.h3_index,
                ST_Intersection(h.geom_25833, b.geom) AS clipped_geom
            FROM tmp_hex_geoms h
            CROSS JOIN tmp_berlin_boundary b
            WHERE ST_Intersects(h.geom_25833, b.geom)
        )
    """)

    log.info("  [5/5] tmp_forest_intersections (stg_waelder join; intersection computed once) ...")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE tmp_forest_intersections AS
        SELECT
            h3_index,
            ST_Area(clipped_geom) AS inter_area_m2,
            ba1, m1, ba2, m2, ba3, m3, ba4, m4, ba5, m5
        FROM (
            SELECT
                h.h3_index,
                ST_Intersection(h.berlin_geom, w.geometry) AS clipped_geom,
                TRIM(w.s1_1_ba) AS ba1, w.s1_1_misch AS m1,
                TRIM(w.s1_2_ba) AS ba2, w.s1_2_misch AS m2,
                TRIM(w.s1_3_ba) AS ba3, w.s1_3_misch AS m3,
                TRIM(w.s1_4_ba) AS ba4, w.s1_4_misch AS m4,
                TRIM(w.s1_5_ba) AS ba5, w.s1_5_misch AS m5
            FROM tmp_hex_berlin h
            JOIN stg_waelder w ON ST_Intersects(h.berlin_geom, w.geometry)
        )
        WHERE NOT ST_IsEmpty(clipped_geom)
    """)


# ---------------------------------------------------------------------------
# Final aggregation (pure tabular — all spatial work already materialised)
# ---------------------------------------------------------------------------


def _final_agg_sql() -> str:
    """
    Single query that assembles the final per-hex result from the materialised
    temp tables.  No spatial operations here — only GROUP BY, joins, arithmetic.

    forest_area_m2 = SUM(inter_area_m2) from individual polygon intersections.
    Forest stands in the Forstbetriebskarte are non-overlapping, so this equals
    the union-intersection area without the cost of ST_Union_Agg.
    """
    tree_hist = tree_genus_histogram_ctes("tmp_trees_h3", "h3_index")
    hist = forest_histogram_ctes("h3_index")
    return f"""
    WITH
    -- Expose the materialised temp table under the name the shared CTEs expect
    forest_intersections AS (SELECT * FROM tmp_forest_intersections),
    tree_agg AS (
        SELECT
            h3_index,
            COUNT(*)                                                      AS tree_count,
            mode(genus)                                                   AS dominant_genus,
            COUNT(DISTINCT genus)                                         AS genus_count,
            mode(NULLIF(species, ''))                                     AS dominant_species,
            COUNT(DISTINCT NULLIF(species, ''))                           AS species_count,
            SUM(CASE WHEN source = 'strassenbaeume' THEN 1 ELSE 0 END)   AS source_strassenbaeume,
            SUM(CASE WHEN source = 'anlagenbaeume'  THEN 1 ELSE 0 END)   AS source_anlagenbaeume,
            SUM(CASE WHEN source = 'gruen_berlin'   THEN 1 ELSE 0 END)   AS source_gruen_berlin
        FROM tmp_trees_h3
        GROUP BY h3_index
    ),
    species_counts AS (
        SELECT h3_index, NULLIF(species, '') AS species, COUNT(*) AS n
        FROM tmp_trees_h3
        WHERE species IS NOT NULL AND species != ''
        GROUP BY h3_index, species
    ),
    dominant_n AS (
        SELECT h3_index, MAX(n) AS max_n
        FROM species_counts
        GROUP BY h3_index
    ),
    forest_totals AS (
        SELECT h3_index, SUM(inter_area_m2) AS forest_area_m2
        FROM forest_intersections
        GROUP BY h3_index
    ),
    {tree_hist},
    {hist}
    SELECT
        h3_h3_to_string(hb.h3_index)                                          AS h3_index_str,
        COALESCE(t.tree_count, 0)                                              AS tree_count,
        t.dominant_genus,
        COALESCE(t.genus_count, 0)                                             AS genus_count,
        t.dominant_species,
        COALESCE(t.species_count, 0)                                           AS species_count,
        CASE WHEN COALESCE(t.tree_count, 0) > 0
            THEN ROUND(100.0 * d.max_n / t.tree_count, 1) END                 AS dominant_species_pct,
        COALESCE(t.source_strassenbaeume, 0)                                   AS source_strassenbaeume,
        COALESCE(t.source_anlagenbaeume, 0)                                    AS source_anlagenbaeume,
        COALESCE(t.source_gruen_berlin, 0)                                     AS source_gruen_berlin,
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
        ROUND(hb.berlin_area_m2 / 1e6, 4)                                     AS berlin_area_km2,
        ROUND(COALESCE(ft.forest_area_m2, 0.0) / 1e6, 4)                      AS forest_area_km2,
        ROUND(
            GREATEST(hb.berlin_area_m2 - COALESCE(ft.forest_area_m2, 0.0), 0.0) / 1e6,
            4
        )                                                                      AS non_forest_area_km2,
        ROUND(
            CASE
                WHEN hb.berlin_area_m2 > 0
                    THEN LEAST(COALESCE(ft.forest_area_m2, 0.0) / hb.berlin_area_m2 * 100.0, 100.0)
                ELSE 0.0
            END,
            1
        )                                                                      AS forest_cover_pct,
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
        fh.forest_genus_other_share,
        g.wkt_4326                                                             AS wkt
    FROM tmp_hex_berlin hb
    LEFT JOIN tmp_hex_geoms g         ON hb.h3_index = g.h3_index
    LEFT JOIN tree_agg t              ON hb.h3_index = t.h3_index
    LEFT JOIN dominant_n d            ON hb.h3_index = d.h3_index
    LEFT JOIN forest_totals ft        ON hb.h3_index = ft.h3_index
    LEFT JOIN tree_genus_histogram tg ON hb.h3_index = tg.h3_index
    LEFT JOIN forest_histogram fh     ON hb.h3_index = fh.h3_index
    WHERE COALESCE(t.tree_count, 0) > 0 OR COALESCE(ft.forest_area_m2, 0) > 0
    """


# ---------------------------------------------------------------------------
# Main aggregation
# ---------------------------------------------------------------------------


def aggregate_resolution(con: duckdb.DuckDBPyConnection, resolution: int) -> gpd.GeoDataFrame:
    log.info("Resolution %d — building temp tables ...", resolution)
    _build_resolution_temps(con, resolution)

    log.info("Resolution %d — final tabular aggregation ...", resolution)
    df = con.execute(_final_agg_sql()).df()
    log.info("  %d hexes, %d trees total", len(df), df["tree_count"].sum())

    df["tree_density_km2"] = (df["tree_count"] / df["non_forest_area_km2"]).round(1)
    df["tree_density_km2"] = df["tree_density_km2"].where(df["non_forest_area_km2"] > 0)

    # h3_cell_to_boundary_wkt returns standard (lng, lat) WKT — no swap needed
    geoms = gpd.GeoSeries.from_wkt(df["wkt"])
    gdf = gpd.GeoDataFrame(
        df.drop(columns=["wkt"]),
        geometry=gpd.GeoSeries(geoms, crs="EPSG:4326"),
    )

    cols = ["h3_index_str"] + [c for c in gdf.columns if c != "h3_index_str"]
    return gdf[cols]


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    log.info("Connecting to %s", DB_PATH)
    con = duckdb.connect(DB_PATH, read_only=True)
    con.execute("LOAD spatial; LOAD h3;")

    _precompute_invariants(con)

    for res in RESOLUTIONS:
        gdf = aggregate_resolution(con, res)
        out_path = OUT_DIR / f"h3_res{res}.parquet"
        gdf.to_parquet(out_path, index=False)
        log.info(
            "Resolution %d — wrote %s  (%d hexes, %.1f MB)",
            res,
            out_path,
            len(gdf),
            out_path.stat().st_size / 1024 / 1024,
        )

    log.info("Done.")


if __name__ == "__main__":
    main()
