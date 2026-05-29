{{ config(materialized='table') }}

-- Morphological dissolve: buffer each stand out 50 m → union all → shrink back 50 m
-- Merges adjacent/near-adjacent forest patches into continuous blobs.
WITH city AS (
    SELECT ST_Union_Agg(geometry) AS geometry
    FROM {{ ref('stg_bezirke') }}
),
buffered AS (
    SELECT ST_Union_Agg(ST_Buffer(f.geometry, 50)) AS geometry
    FROM {{ source('raw', 'raw_waelder') }} f
    WHERE f.geometry IS NOT NULL
),
result AS (
    SELECT ST_Intersection(ST_Buffer(b.geometry, -50), c.geometry) AS geometry
    FROM buffered b
    CROSS JOIN city c
)
SELECT ST_FlipCoordinates(ST_Transform(geometry, 'EPSG:25833', 'EPSG:4326')) AS geometry
FROM result
WHERE geometry IS NOT NULL
  AND NOT ST_IsEmpty(geometry)
