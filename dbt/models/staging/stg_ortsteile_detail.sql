{{ config(materialized='table') }}

-- Individual Ortsteil (sub-district) polygons with names and bezirk membership.
-- The ALKIS Schlüssel format: 110000 BB OOOO
--   BB   = 2-digit Bezirk number (chars 7-8, 1-indexed)
--   OOOO = Ortsteil suffix
SELECT
    sch                          AS ortsteil_code,
    nam                          AS ortsteil_name,
    SUBSTR(sch, 7, 2)            AS bezirk_code,
    geometry
FROM {{ source('raw', 'raw_alkis_ortsteile') }}
WHERE geometry IS NOT NULL
