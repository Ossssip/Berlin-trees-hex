{{ config(materialized='table') }}

-- Assign each tree an H3 cell at res 12 (edge ~9.4 m).
-- Expanding to the k=1 disk (7 cells) guarantees full coverage of any 2 m radius,
-- with no boundary misses. The join on integer cell keys is a hash join;
-- ST_Distance only runs on the small candidate set that shares a cell.
WITH union_all AS (
    SELECT
        *,
        md5(source || '_' || tree_id) AS tree_uuid
    FROM (
        SELECT * FROM {{ ref('stg_strassenbaeume') }}
        UNION ALL
        SELECT * FROM {{ ref('stg_anlagenbaeume') }}
        UNION ALL
        SELECT * FROM {{ ref('stg_gruen_berlin') }}
    )
),
h3_gridded AS (
    SELECT *,
        h3_latlng_to_cell(
            ST_Y(ST_Transform(geometry, 'EPSG:25833', 'EPSG:4326')),
            ST_X(ST_Transform(geometry, 'EPSG:25833', 'EPSG:4326')),
            12
        ) AS h3_cell
    FROM union_all
),
expanded AS (
    SELECT *, UNNEST(h3_grid_disk(h3_cell, 1)) AS neighbor_cell
    FROM h3_gridded
),
duplicates AS (
    SELECT DISTINCT e.tree_uuid
    FROM expanded e
    JOIN h3_gridded g2
      ON g2.h3_cell = e.neighbor_cell
     AND e.source != g2.source
     AND ST_Distance(e.geometry, g2.geometry) <= 2
     -- Require genus agreement (case-insensitive) to exclude cross-genus proximity,
     -- which is dense planting rather than a duplicate. NULL genus on either side is
     -- kept flagged as uncertain rather than silently dropped.
     AND (
         LOWER(TRIM(e.genus_latin)) = LOWER(TRIM(g2.genus_latin))
         OR e.genus_latin IS NULL
         OR g2.genus_latin IS NULL
     )
)

SELECT
    u.*,
    COALESCE(d.tree_uuid IS NOT NULL, FALSE) AS is_potential_duplicate
FROM union_all u
LEFT JOIN duplicates d ON u.tree_uuid = d.tree_uuid
