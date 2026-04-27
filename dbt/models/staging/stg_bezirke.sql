{{ config(materialized='table') }}

-- Bezirke (districts) dissolved from individual Ortsteile.
-- The 12 Berlin districts are derived by grouping Ortsteile on their
-- 2-digit Bezirk code (characters 7-8 of the ALKIS Schlüssel).
WITH ortsteile AS (
    SELECT
        SUBSTR(sch, 7, 2) AS bezirk_code,
        geometry
    FROM {{ source('raw', 'raw_alkis_ortsteile') }}
    WHERE geometry IS NOT NULL
)
SELECT
    bezirk_code,
    CASE bezirk_code
        WHEN '01' THEN 'Mitte'
        WHEN '02' THEN 'Friedrichshain-Kreuzberg'
        WHEN '03' THEN 'Pankow'
        WHEN '04' THEN 'Charlottenburg-Wilmersdorf'
        WHEN '05' THEN 'Spandau'
        WHEN '06' THEN 'Steglitz-Zehlendorf'
        WHEN '07' THEN 'Tempelhof-Schöneberg'
        WHEN '08' THEN 'Neukölln'
        WHEN '09' THEN 'Treptow-Köpenick'
        WHEN '10' THEN 'Marzahn-Hellersdorf'
        WHEN '11' THEN 'Lichtenberg'
        WHEN '12' THEN 'Reinickendorf'
        ELSE 'Unknown'
    END AS bezirk_name,
    ST_Union_Agg(geometry) AS geometry
FROM ortsteile
GROUP BY bezirk_code
