"""File-corpus pipeline: TerraDS_CodeRepos/*.tar.gz -> deduped, parse-checked HCL files (parquet).

The Spark-shaped job from DECISIONS.md section 4: thousands of archives are spread
across partitions, each task streams tarballs and emits one row per .tf/.hcl/.tfvars
file; then a shuffle removes exact duplicate contents (forks are common), files with
structural HCL errors and files that look like they contain secrets are dropped, and the
result is written partitioned by extension.

    python -m data_pipeline.extract_files --archives data/TerraDS_CodeRepos \\
        --out data/terrads_files.parquet --limit 500

Not done yet: near-duplicate (MinHash/LSH) dedup.
"""
import argparse
import glob
import hashlib
import io
import os
import re
import tarfile

EXTS = (".tf", ".hcl", ".tfvars")
SKIP_DIR_PARTS = {".terraform", ".git", ".terragrunt-cache", "node_modules"}
MAX_BYTES = 1_000_000

_SECRET_RES = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                                   # AWS access key id
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*[\"']?[A-Za-z0-9/+=]{40}"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),                            # API-style tokens
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),                           # GitHub PAT
]


def has_secret(text):
    return any(r.search(text) for r in _SECRET_RES)


def _repo_id(path):
    m = re.match(r"(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else -1


def extract_archive(path):
    """Yield row tuples for every candidate file in one archive. Never raises on a
    bad archive (returns what it could read): one corrupt tarball must not kill a job."""
    from terra_pilot.utils import validate
    rows = []
    rid = _repo_id(path)
    try:
        with tarfile.open(path, "r:*") as tar:
            for m in tar:
                if not m.isfile() or m.size > MAX_BYTES:
                    continue
                name = m.name
                ext = os.path.splitext(name)[1].lower()
                if ext not in EXTS or SKIP_DIR_PARTS & set(name.split("/")):
                    continue
                raw = tar.extractfile(m).read()
                if b"\x00" in raw:
                    continue
                text = raw.decode("utf-8", errors="replace")
                rows.append((
                    rid, name, ext, len(raw),
                    hashlib.sha1(raw).hexdigest(), text,
                    not validate.hcl_text_problems(text),
                    has_secret(text),
                ))
    except (tarfile.TarError, EOFError, OSError):
        pass
    return rows


def run(archives, out_path, *, limit=None, partitions=None, spark=None):
    """archives: a directory of *.tar.gz or an explicit list of paths.
    Returns counts per stage."""
    from pyspark.sql import functions as F, types as T
    from pyspark.sql.window import Window
    from data_pipeline.spark_env import get_spark

    paths = sorted(glob.glob(os.path.join(archives, "*.tar.gz"))) \
        if isinstance(archives, str) else list(archives)
    if limit:
        paths = paths[:limit]
    own = spark is None
    spark = spark or get_spark("terrads-files")
    try:
        schema = T.StructType([
            T.StructField("repo_id", T.LongType()), T.StructField("path", T.StringType()),
            T.StructField("ext", T.StringType()), T.StructField("size", T.LongType()),
            T.StructField("sha1", T.StringType()), T.StructField("text", T.StringType()),
            T.StructField("parse_ok", T.BooleanType()), T.StructField("has_secret", T.BooleanType()),
        ])
        n = partitions or max(1, min(len(paths), (os.cpu_count() or 2) * 4))
        rdd = spark.sparkContext.parallelize(paths, n).flatMap(extract_archive)
        df = spark.createDataFrame(rdd, schema).cache()
        counts = {"archives": len(paths), "files": df.count()}

        ok = df.filter(F.col("parse_ok"))
        counts["parse_ok"] = ok.count()
        clean = ok.filter(~F.col("has_secret"))
        counts["no_secret"] = clean.count()
        # exact-duplicate removal; keep the lowest repo_id / path for determinism
        w = Window.partitionBy("sha1").orderBy("repo_id", "path")
        unique = clean.withColumn("_rn", F.row_number().over(w)).filter("_rn = 1").drop("_rn")
        counts["unique"] = unique.count()
        unique.write.mode("overwrite").partitionBy("ext").parquet(out_path)
        df.unpersist()
        return counts
    finally:
        if own:
            spark.stop()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--archives", default="data/TerraDS_CodeRepos")
    ap.add_argument("--out", default="data/terrads_files.parquet")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--partitions", type=int)
    a = ap.parse_args(argv)
    for k, v in run(a.archives, a.out, limit=a.limit, partitions=a.partitions).items():
        print(f"{k:>10}: {v:,}")


if __name__ == "__main__":
    main()
