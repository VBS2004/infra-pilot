"""Metadata pipeline: TerraDS sqlite -> filtered repos + repo/module/resource join (parquet).

A script version of the exploration in pyspark_test.ipynb:
    license filter -> drop archived -> dedupe FullName -> size filter
    -> join repos -> modules -> resources -> parquet

    python -m data_pipeline.build_metadata --sqlite data/TerraDS.sqlite --out data
"""
import argparse
import os
import sqlite3

DEFAULT_LICENSES = ("MIT", "Apache-2.0", "BSD-3-Clause")


def build(sqlite_path, out_dir, *, licenses=DEFAULT_LICENSES, min_size_kb=5, spark=None):
    """Returns {"raw", "licensed", "active", "deduped", "sized", "modules", "resources"}."""
    import pandas as pd
    from pyspark.sql import functions as F
    from data_pipeline.spark_env import get_spark

    own = spark is None
    spark = spark or get_spark("terrads-metadata")
    try:
        conn = sqlite3.connect(sqlite_path)
        try:
            repos = spark.createDataFrame(pd.read_sql("SELECT * FROM Repositories", conn))
            modules = spark.createDataFrame(pd.read_sql("SELECT * FROM Modules", conn))
            resources = spark.createDataFrame(pd.read_sql("SELECT * FROM Resources", conn))
        finally:
            conn.close()

        counts = {"raw": repos.count()}
        licensed = repos.filter(F.col("License").isin(list(licenses)))
        counts["licensed"] = licensed.count()
        active = licensed.filter(F.col("Archived") == 0)
        counts["active"] = active.count()
        # Deterministic dedupe: keep the most-starred (then lowest Id) repo per FullName.
        w = __import__("pyspark.sql.window", fromlist=["Window"]).Window \
            .partitionBy("FullName").orderBy(F.desc("StarCount"), F.asc("Id"))
        deduped = active.withColumn("_rn", F.row_number().over(w)).filter("_rn = 1").drop("_rn")
        counts["deduped"] = deduped.count()
        sized = deduped.filter(F.col("SizeInKb") > min_size_kb)
        counts["sized"] = sized.count()

        mods = modules.withColumnRenamed("Id", "ModuleId").join(
            sized.select(F.col("Id").alias("RepoId"), "FullName", "License", "StarCount"),
            F.col("RepositoryId") == F.col("RepoId"), "inner")
        counts["modules"] = mods.count()
        res = resources.withColumnRenamed("Id", "ResourceId")
        joined = mods.join(res, mods["ModuleId"] == res["ModuleId"], "inner").drop(res["ModuleId"])
        counts["resources"] = joined.count()

        sized.write.mode("overwrite").parquet(os.path.join(out_dir, "terrads_filtered.parquet"))
        joined.write.mode("overwrite").parquet(os.path.join(out_dir, "terrads_joined.parquet"))
        return counts
    finally:
        if own:
            spark.stop()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sqlite", default="data/TerraDS.sqlite")
    ap.add_argument("--out", default="data")
    ap.add_argument("--licenses", nargs="+", default=list(DEFAULT_LICENSES))
    ap.add_argument("--min-size-kb", type=int, default=5)
    a = ap.parse_args(argv)
    for k, v in build(a.sqlite, a.out, licenses=tuple(a.licenses), min_size_kb=a.min_size_kb).items():
        print(f"{k:>10}: {v:,}")


if __name__ == "__main__":
    main()
