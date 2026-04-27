SELECT
    trim(id)                        AS tree_id,
    trim(art_bot)                   AS species_latin,
    trim(art_dtsch)                 AS species_german,
    trim(gattung)                   AS genus_latin,
    trim(gattung_deutsch)           AS genus_german,
    trim(art_gruppe)                AS tree_group,
    TRY_CAST(pflanzjahr AS INTEGER) AS planting_year,
    standalter                      AS tree_age,
    geometry,
    'strassenbaeume'                AS source
FROM {{ source('raw', 'raw_strassenbaeume') }}
WHERE geometry IS NOT NULL
