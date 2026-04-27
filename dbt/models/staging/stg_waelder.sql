{{ config(materialized='table') }}

WITH city_limits AS (
    SELECT geometry FROM {{ ref('stg_ortsteile') }}
)
SELECT
    w.* EXCLUDE (geometry),
    ST_Intersection(w.geometry, c.geometry) AS geometry,
    ST_Area(ST_Intersection(w.geometry, c.geometry)) / 10000.0 AS area_ha
FROM {{ source('raw', 'raw_waelder') }} AS w
JOIN city_limits AS c
  ON ST_Intersects(w.geometry, c.geometry)
WHERE w.geometry IS NOT NULL
