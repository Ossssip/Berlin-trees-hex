import os

import duckdb


def main():
    os.makedirs("data/processed", exist_ok=True)
    con = duckdb.connect("data/berlin_trees.duckdb")
    con.execute("INSTALL spatial; LOAD spatial;")
    print("Exporting int_trees_unified to data/processed/trees.parquet...")

    con.execute("""
        COPY (
            SELECT * REPLACE (
                -- DuckDB's EPSG:4326 uses (lat, lon) axis order; flip to (lon, lat)
                -- so the exported GeoParquet is standard WGS84 for GeoJSON consumers.
                ST_FlipCoordinates(
                    ST_Transform(geometry, 'EPSG:25833', 'EPSG:4326')
                ) AS geometry
            )
            FROM int_trees_unified
        )
        TO 'data/processed/trees.parquet'
        (FORMAT PARQUET)
    """)
    print("Export complete.")


if __name__ == "__main__":
    main()
