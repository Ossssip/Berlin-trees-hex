import os

import duckdb

DB_PATH = "data/berlin_trees.duckdb"


def main():
    print(f"Initializing DuckDB at {DB_PATH}...")

    os.makedirs("data", exist_ok=True)

    con = duckdb.connect(DB_PATH)

    print("Installing and loading extensions...")
    con.execute("INSTALL spatial;")
    con.execute("LOAD spatial;")
    con.execute("INSTALL h3 FROM community;")
    con.execute("LOAD h3;")

    print("Creating views for raw parquet files...")

    # ST_GeomFromWKB(ST_AsWKB(...)) strips the CRS identifier that GeoParquet embeds.
    # DuckDB spatial 1.5+ reads that CRS tag but can only persist it in storage v1.5.0+,
    # which isn't guaranteed. Round-tripping through WKB drops the tag cleanly.
    commands = [
        "CREATE OR REPLACE VIEW raw_strassenbaeume AS SELECT * REPLACE(ST_GeomFromWKB(ST_AsWKB(geometry)) AS geometry) FROM read_parquet('data/raw/baumbestand_strassen.parquet');",
        "CREATE OR REPLACE VIEW raw_anlagenbaeume AS SELECT * REPLACE(ST_GeomFromWKB(ST_AsWKB(geometry)) AS geometry) FROM read_parquet('data/raw/baumbestand_anlagen.parquet');",
        "CREATE OR REPLACE VIEW raw_waelder AS SELECT * REPLACE(ST_GeomFromWKB(ST_AsWKB(geometry)) AS geometry) FROM read_parquet('data/raw/forstbetriebskarte.parquet');",
        "CREATE OR REPLACE VIEW raw_gruen_berlin AS SELECT * REPLACE(ST_GeomFromWKB(ST_AsWKB(geometry)) AS geometry) FROM read_parquet('data/raw/gruen_berlin.parquet');",
        "CREATE OR REPLACE VIEW raw_alkis_ortsteile AS SELECT * REPLACE(ST_GeomFromWKB(ST_AsWKB(geometry)) AS geometry) FROM read_parquet('data/raw/alkis_ortsteile.parquet');",
    ]

    for cmd in commands:
        con.execute(cmd)

    print("DuckDB initialization complete.")


if __name__ == "__main__":
    main()
