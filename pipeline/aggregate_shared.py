"""
pipeline/aggregate_shared.py
----------------------------
SQL helpers shared by aggregate_h3.py and aggregate_admin.py.

Currently provides:
    load_ba_lookup(csv_path)                             → dict[str, str]
    forest_intersections_cte(geom_cte, id_col, geom_col) → SQL CTE string
    forest_histogram_ctes(id_col)                        → SQL CTE fragment string
    tree_genus_histogram_ctes(source_cte, id_col)        → SQL CTE fragment string

Typical usage (both scripts):

    from aggregate_shared import forest_intersections_cte, forest_histogram_ctes

    fi   = forest_intersections_cte("my_geom_cte", "my_id_col", "my_geom_col")
    hist = forest_histogram_ctes("my_id_col")

    sql = f\"\"\"
    WITH ...previous_ctes...,
    {fi},
    dominant_forest AS (
        SELECT my_id_col, arg_max(ba1, inter_area_m2) AS forest_dominant_species
        FROM forest_intersections WHERE ba1 IS NOT NULL
        GROUP BY my_id_col
    ),
    {hist}
    SELECT ... LEFT JOIN forest_histogram fh ON ... = fh.my_id_col
    \"\"\"

The spatial join (ST_Intersection) is computed once in forest_intersections and
shared by dominant_forest and all histogram CTEs — no duplicate passes over
stg_waelder.
"""

import csv
from pathlib import Path

_BA_CODES_PATH = Path(__file__).parent / "forest_ba_codes.csv"


def load_ba_lookup(csv_path: Path = _BA_CODES_PATH) -> dict[str, str]:
    """
    Load ba_code → genus_latin from the lookup CSV.

    Codes with empty genus_latin (Blöße = 'Blö', mixed-conifer catch-all = 'NHS')
    map to '' and are excluded downstream.
    """
    lookup: dict[str, str] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            lookup[row["ba_code"].strip()] = row["genus_latin"].strip()
    return lookup


def _ba_values_sql(lookup: dict[str, str]) -> str:
    """Convert lookup dict to a SQL VALUES list for an inline CTE."""
    rows = []
    for code, genus in sorted(lookup.items()):
        code_s = code.replace("'", "''")
        genus_s = genus.replace("'", "''")
        rows.append(f"        ('{code_s}', '{genus_s}')")
    return ",\n".join(rows)


def forest_intersections_cte(
    geom_cte: str,
    id_col: str,
    geom_col: str = "geometry",
) -> str:
    """
    Return a SQL CTE named ``forest_intersections`` that spatially joins a set of
    target geometries against stg_waelder.

    Parameters
    ----------
    geom_cte : str
        Name of a prior CTE exposing ``{id_col}`` and ``{geom_col}``
        in EPSG:25833.
    id_col : str
        Feature identifier column in ``geom_cte``.
    geom_col : str
        Geometry column in ``geom_cte`` (default: ``"geometry"``).

    Output columns:
        {id_col}, inter_area_m2,
        ba1..ba5  (TRIM of s1_1_ba .. s1_5_ba),
        m1..m5    (s1_1_misch .. s1_5_misch)

    This CTE is the single source for both dominant_forest and forest_histogram —
    the spatial intersection is computed once and shared.
    """
    return f"""    forest_intersections AS (
        SELECT
            {id_col},
            ST_Area(clipped_geom) AS inter_area_m2,
            ba1, m1, ba2, m2, ba3, m3, ba4, m4, ba5, m5
        FROM (
            SELECT
                feat.{id_col},
                ST_Intersection(feat.{geom_col}, w.geometry) AS clipped_geom,
                TRIM(w.s1_1_ba) AS ba1, w.s1_1_misch AS m1,
                TRIM(w.s1_2_ba) AS ba2, w.s1_2_misch AS m2,
                TRIM(w.s1_3_ba) AS ba3, w.s1_3_misch AS m3,
                TRIM(w.s1_4_ba) AS ba4, w.s1_4_misch AS m4,
                TRIM(w.s1_5_ba) AS ba5, w.s1_5_misch AS m5
            FROM {geom_cte} feat
            JOIN stg_waelder w ON ST_Intersects(feat.{geom_col}, w.geometry)
        )
        WHERE NOT ST_IsEmpty(clipped_geom)
    )"""


def forest_histogram_ctes(id_col: str) -> str:
    """
    Return a SQL CTE fragment (no leading WITH, no trailing comma) for
    area-weighted Hauptbestand genus composition.

    Expects a prior CTE named ``forest_intersections`` with columns:
        {id_col}, inter_area_m2,
        ba1..ba5, m1..m5
    as produced by :func:`forest_intersections_cte`.

    Appends CTEs (in order):
        ba_lookup, forest_slots, forest_weights,
        forest_genus_agg, forest_total, forest_ranked, forest_histogram

    Final CTE ``forest_histogram`` exposes:
        {id_col},
        forest_genus_1 .. forest_genus_5,
        forest_genus_1_share .. forest_genus_5_share  (0–100, 1 d.p.),
        forest_genus_other_share

    Only s1_* (Hauptbestand) slots are used; s2/s3/ue are excluded by design.
    Features with no intersecting forest polygon produce no row — callers should
    LEFT JOIN on {id_col}.
    """
    lookup = load_ba_lookup()
    ba_values = _ba_values_sql(lookup)

    return f"""    ba_lookup(ba_code, genus_latin) AS (
        VALUES
{ba_values}
    ),
    -- Unpivot slots 1–5 into long form; drop null ba_codes and zero-misch entries
    forest_slots AS (
        SELECT {id_col}, inter_area_m2, ba1 AS ba_code, m1 AS misch FROM forest_intersections WHERE ba1 IS NOT NULL AND m1 > 0
        UNION ALL
        SELECT {id_col}, inter_area_m2, ba2, m2 FROM forest_intersections WHERE ba2 IS NOT NULL AND m2 > 0
        UNION ALL
        SELECT {id_col}, inter_area_m2, ba3, m3 FROM forest_intersections WHERE ba3 IS NOT NULL AND m3 > 0
        UNION ALL
        SELECT {id_col}, inter_area_m2, ba4, m4 FROM forest_intersections WHERE ba4 IS NOT NULL AND m4 > 0
        UNION ALL
        SELECT {id_col}, inter_area_m2, ba5, m5 FROM forest_intersections WHERE ba5 IS NOT NULL AND m5 > 0
    ),
    -- Resolve ba_code → genus; exclude clearings (Blöße) and unresolvable catch-alls (NHS)
    -- LOWER() ensures genus matches PhyloPic image keys (stored/loaded as lowercase)
    forest_weights AS (
        SELECT
            fs.{id_col},
            LOWER(lk.genus_latin)                  AS genus,
            fs.inter_area_m2 * (fs.misch / 100.0)  AS genus_weight
        FROM forest_slots fs
        JOIN ba_lookup lk ON fs.ba_code = lk.ba_code
        WHERE lk.genus_latin != ''
    ),
    forest_genus_agg AS (
        SELECT {id_col}, genus, SUM(genus_weight) AS total_weight
        FROM forest_weights
        GROUP BY {id_col}, genus
    ),
    forest_total AS (
        SELECT {id_col}, SUM(total_weight) AS grand_total
        FROM forest_genus_agg
        GROUP BY {id_col}
    ),
    forest_ranked AS (
        SELECT
            g.{id_col},
            g.genus,
            g.total_weight / t.grand_total                                   AS genus_share,
            ROW_NUMBER() OVER (PARTITION BY g.{id_col} ORDER BY g.total_weight DESC) AS rn
        FROM forest_genus_agg g
        JOIN forest_total t ON g.{id_col} = t.{id_col}
    ),
    forest_histogram AS (
        SELECT
            {id_col},
            MAX(CASE WHEN rn = 1 THEN genus END)                             AS forest_genus_1,
            ROUND(MAX(CASE WHEN rn = 1 THEN genus_share END) * 100, 1)       AS forest_genus_1_share,
            MAX(CASE WHEN rn = 2 THEN genus END)                             AS forest_genus_2,
            ROUND(MAX(CASE WHEN rn = 2 THEN genus_share END) * 100, 1)       AS forest_genus_2_share,
            MAX(CASE WHEN rn = 3 THEN genus END)                             AS forest_genus_3,
            ROUND(MAX(CASE WHEN rn = 3 THEN genus_share END) * 100, 1)       AS forest_genus_3_share,
            MAX(CASE WHEN rn = 4 THEN genus END)                             AS forest_genus_4,
            ROUND(MAX(CASE WHEN rn = 4 THEN genus_share END) * 100, 1)       AS forest_genus_4_share,
            MAX(CASE WHEN rn = 5 THEN genus END)                             AS forest_genus_5,
            ROUND(MAX(CASE WHEN rn = 5 THEN genus_share END) * 100, 1)       AS forest_genus_5_share,
            ROUND(
                (1.0 - SUM(CASE WHEN rn <= 5 THEN genus_share ELSE 0.0 END)) * 100,
                1
            )                                                                 AS forest_genus_other_share
        FROM forest_ranked
        GROUP BY {id_col}
    )"""


def tree_genus_histogram_ctes(source_cte: str, id_col: str) -> str:
    """
    Return a SQL CTE fragment (no leading WITH, no trailing comma) for a top-10
    registered-tree genus histogram.

    Expects a prior CTE/table ``source_cte`` with columns:
        {id_col}, genus

    Final CTE ``tree_genus_histogram`` exposes:
        {id_col},
        tree_genus_1 .. tree_genus_10,
        tree_genus_1_count .. tree_genus_10_count,
        tree_genus_1_share .. tree_genus_10_share  (0–100, 1 d.p.),
        tree_genus_other_count,   ← genera ranked 11+
        tree_genus_other_share
    """
    slots = "\n".join(
        f"""            MAX(CASE WHEN rn = {i} THEN genus END)                            AS tree_genus_{i},
            MAX(CASE WHEN rn = {i} THEN genus_count END)                      AS tree_genus_{i}_count,
            ROUND(MAX(CASE WHEN rn = {i} THEN genus_share END) * 100, 1)      AS tree_genus_{i}_share,"""
        for i in range(1, 11)
    )
    return f"""    tree_genus_counts AS (
        SELECT {id_col}, genus, COUNT(*) AS genus_count
        FROM {source_cte}
        WHERE genus IS NOT NULL AND genus != ''
        GROUP BY {id_col}, genus
    ),
    tree_genus_totals AS (
        SELECT {id_col}, SUM(genus_count) AS total_count
        FROM tree_genus_counts
        GROUP BY {id_col}
    ),
    tree_genus_ranked AS (
        SELECT
            c.{id_col},
            c.genus,
            c.genus_count,
            c.genus_count * 1.0 / t.total_count AS genus_share,
            ROW_NUMBER() OVER (
                PARTITION BY c.{id_col}
                ORDER BY c.genus_count DESC, c.genus ASC
            ) AS rn
        FROM tree_genus_counts c
        JOIN tree_genus_totals t ON c.{id_col} = t.{id_col}
    ),
    tree_genus_histogram AS (
        SELECT
            {id_col},
{slots}
            COALESCE(SUM(CASE WHEN rn > 10 THEN genus_count ELSE 0 END), 0)  AS tree_genus_other_count,
            ROUND(
                COALESCE(SUM(CASE WHEN rn > 10 THEN genus_share ELSE 0.0 END), 0.0) * 100,
                1
            )                                                               AS tree_genus_other_share
        FROM tree_genus_ranked
        GROUP BY {id_col}
    )"""
