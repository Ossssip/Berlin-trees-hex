SELECT
    trim(id)                         AS tree_id,
    trim(baumart_bo)                 AS species_latin,
    trim(baumart_de)                 AS species_german,
    trim(gattung_bo)                 AS genus_latin,
    NULL                             AS genus_german,
    NULL                             AS tree_group,
    NULL::INTEGER                    AS planting_year,
    NULLIF(standalter, 0)            AS tree_age,
    geometry,
    'gruen_berlin'                   AS source
FROM {{ source('raw', 'raw_gruen_berlin') }}
WHERE geometry IS NOT NULL
