"""SparkSession construction that works from a plain `python` invocation.

The venv's pip `pyspark` bundles its own Spark, so a system SPARK_HOME (e.g.
/opt/spark, a different version) must not be picked up. Java 17 is required;
JAVA_HOME is only auto-detected when the caller has not set it.
"""
import glob
import os
import sys


def _find_java17():
    for pattern in ("/usr/lib/jvm/java-17*", "/usr/lib/jvm/*17*", "/usr/lib/jvm/default"):
        for cand in sorted(glob.glob(pattern)):
            if os.path.exists(os.path.join(cand, "bin", "java")):
                return cand
    return None


def get_spark(app_name="terra-pilot-data", master="local[*]", shuffle_partitions=None):
    os.environ.pop("SPARK_HOME", None)
    # Python workers are separate processes and do not see this process's sys.path:
    # export the checkout's src/ + root so `terra_pilot` imports without pip install.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    extra = os.pathsep.join([os.path.join(root, "src"), root])
    os.environ["PYTHONPATH"] = extra + (os.pathsep + os.environ["PYTHONPATH"]
                                        if os.environ.get("PYTHONPATH") else "")
    if not os.environ.get("JAVA_HOME"):
        java = _find_java17()
        if java:
            os.environ["JAVA_HOME"] = java
    os.environ.setdefault("SPARK_SUBMIT_OPTS",
                          "--add-exports=java.base/sun.nio.ch=ALL-UNNAMED")
    # Executors are Python workers: make them use this interpreter.
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    from pyspark.sql import SparkSession
    b = (SparkSession.builder.master(master).appName(app_name)
         # 1 GB (Spark's default) OOMs the parquet writer on the full Resources table.
         .config("spark.driver.memory", os.environ.get("SPARK_DRIVER_MEMORY", "4g"))
         .config("spark.sql.execution.arrow.pyspark.enabled", "true"))
    if shuffle_partitions:
        b = b.config("spark.sql.shuffle.partitions", str(shuffle_partitions))
    return b.getOrCreate()
