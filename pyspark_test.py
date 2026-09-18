import os
import sqlite3
import pandas as pd
import traceback
from pyspark.sql import SparkSession, functions as F

if "SPARK_HOME" in os.environ:
    del os.environ["SPARK_HOME"]

os.environ["JAVA_HOME"] = "/usr/lib/jvm/java-17-openjdk"
os.environ["SPARK_SUBMIT_OPTS"] = "--add-exports=java.base/sun.nio.ch=ALL-UNNAMED"

try:
    # Initialize a single SparkSession
    spark = (
        SparkSession.builder
        .master("local[*]")
        .appName("terraformer-v2")
        .getOrCreate()
    )
    print("Spark initialized successfully. Version:", spark.version)

    # Read data from SQLite using pandas
    print("Loading data from SQLite to Pandas...")
    conn = sqlite3.connect("data/TerraDS.sqlite")
    repos_df = pd.read_sql("SELECT * FROM Repositories", conn)
    modules_df = pd.read_sql("SELECT * FROM Modules", conn)
    resources_df = pd.read_sql("SELECT * FROM Resources", conn)
    conn.close()

    # Convert Pandas DataFrames to Spark DataFrames 
    # (PyArrow will significantly speed this up)
    print("Converting Pandas DataFrames to Spark DataFrames...")
    repos_spark = spark.createDataFrame(repos_df)
    modules_spark = spark.createDataFrame(modules_df)
    resources_spark = spark.createDataFrame(resources_df)

    print("Raw repos:", repos_spark.count())

except Exception as e:
    traceback.print_exc()
finally:
    # Ensure the Spark session is stopped cleanly
    if 'spark' in locals():
        spark.stop()
        print("Spark session stopped.")