SELECT 1
FROM {{ ref('int_trees_unified') }}
HAVING count(*) <= 900000
