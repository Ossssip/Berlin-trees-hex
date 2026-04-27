{{ config(materialized='table') }}

SELECT ST_Union_Agg(geometry) AS geometry
FROM {{ source('raw', 'raw_alkis_ortsteile') }}
WHERE geometry IS NOT NULL
