{{ config(materialized='table') }}

WITH city AS (
    SELECT ST_Union_Agg(geometry) AS geometry
    FROM {{ ref('stg_bezirke') }}
),
clipped AS (
    SELECT
        f.id,
        f.lage,
        f.bezirk,
        f.betrkl,
        f.grpalter,
        f.gis_area,
        f.s1_1_ba,
        f.s1_1_deuts,
        f.s1_1_misch,
        f.s1_1_bhd,
        f.s1_1_hoehe,
        ST_CollectionExtract(ST_Intersection(f.geometry, c.geometry), 3) AS geom
    FROM {{ source('raw', 'raw_waelder') }} f
    CROSS JOIN city c
    WHERE f.geometry IS NOT NULL
      AND ST_Intersects(f.geometry, c.geometry)
)
SELECT
    id, lage, bezirk, betrkl, grpalter, gis_area,
    s1_1_ba, s1_1_deuts, s1_1_misch, s1_1_bhd, s1_1_hoehe,
    ST_FlipCoordinates(ST_Transform(geom, 'EPSG:25833', 'EPSG:4326')) AS geometry
FROM clipped
WHERE NOT ST_IsEmpty(geom)
