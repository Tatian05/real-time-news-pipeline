import os
import psycopg2

from dotenv import load_dotenv
from pyspark.sql import SparkSession
from pyspark.sql.functions import *
from pyspark.sql.types import StructType, StructField, StringType, ArrayType, TimestampType


#SPARK SESSION
spark = SparkSession \
        .builder \
        .config("spark.jars.packages", "org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.0") \
        .appName("Latest-news-streaming") \
        .getOrCreate()

#KAFKA BROKER
bootstrap_server = "kafka:9092"

#RAW KAFKA TOPIC
topic = "raw-latest-news"

#TRANSFORM DATA
df_news = spark \
        .readStream \
        .format("kafka") \
        .option("kafka.bootstrap.servers", bootstrap_server) \
        .option("subscribe", topic) \
        .option("startingOffsets", "earliest") \
        .load()

schema = StructType([
    StructField("status", StringType(), True),
    StructField("news", ArrayType(StructType([
        StructField("id", StringType(), True),
        StructField("title", StringType(), True),
        StructField("description", StringType(), True),
        StructField("url", StringType(), True),
        StructField("author", StringType(), True),
        StructField("image", StringType(), True),
        StructField("language", StringType(), True),
        StructField("category", ArrayType(StringType()), True),
        StructField("published", TimestampType(), True)
        ])), True),
    StructField("page", StringType(), True)
])

df_news = df_news.selectExpr("CAST(value AS STRING)").select("value")
df_news = df_news.withColumn("data", from_json(col("value"), schema)).select("data.*")

df_news = df_news.select("news") \
        .withColumn("news", explode("news")) \
        .select("*", 
            col("news.id").alias("id"),
            col("news.title").alias("title"),
            col("news.description").alias("description"),
            col("news.url").alias("url"),
            col("news.author").alias("author"),
            col("news.image").alias("image"),
            col("news.language").alias("language"),
            col("news.category").alias("category"),
            col("news.published").alias("published")) \
        .withColumn("category", explode("category")) \
        .fillna({'image': "None"}) \
        .dropDuplicates(['id', 'category']) \
        .drop("news")

#CLEANED DATA KAFKA TOPIC
df_kafka = df_news.selectExpr("CAST (id AS STRING) AS key", "to_json(struct(*)) AS value")
cleaned_topic = "cleaned-latest-news"

#SEND TO KAFKA
kafka_query = df_kafka \
    .writeStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", bootstrap_server) \
    .option("topic", cleaned_topic) \
    .option("checkpointLocation", "/opt/spark-checkpoints") \
    .option("failOnDataLoss", "false")  \
    .start()

#POSTGRESQL CONNECTION
load_dotenv()

db_name = os.environ.get("POSTGRES_DB")
db_host = os.environ.get("POSTGRES_HOST")
db_port = os.environ.get("POSTGRES_PORT")
db_user = os.environ.get("POSTGRES_USER")
db_password = os.environ.get("POSTGRES_PASSWORD")

postgres_url = f"jdbc:postgresql://{db_host}:{db_port}/{db_name}"
db_properties = {
    "user": db_user,
    "password": db_password,
    "driver":"org.postgresql.Driver"
}

#SQL TABLE
schema_query = "CREATE SCHEMA IF NOT EXISTS news"

temp_table_query = """
    CREATE TABLE IF NOT EXISTS news.temp (
        id VARCHAR(255),
        title VARCHAR(255),
        description VARCHAR(1000),
        url VARCHAR(255),
        author VARCHAR(100),
        image VARCHAR(500),
        language VARCHAR(50),
        category VARCHAR(50),
        published TIMESTAMP
    );
"""

latest_table_query = """
    CREATE TABLE IF NOT EXISTS news.latest (
        id VARCHAR(255),
        title VARCHAR(255),
        description VARCHAR(1000),
        url VARCHAR(255),
        author VARCHAR(100),
        image VARCHAR(500),
        language VARCHAR(50),
        category VARCHAR(50),
        published TIMESTAMP
    );
"""

merge_query = """
    MERGE INTO news.latest AS target
    USING news.temp AS source
    ON target.id = source.id AND target.category = source.category
    WHEN MATCHED THEN 
        UPDATE SET
            title = source.title,
            description = source.description,
            url = source.url,
            author = source.author,
            image = source.image,
            language = source.language,
            category = source.category,
            published = source.published
    WHEN NOT MATCHED THEN
    INSERT (id, title, description, url, author, image, language, category, published)
    VALUES (source.id, source.title, source.description, source.url, source.author, source.image, source.language, source.category, source.published)
"""

#INSERT DATA TO POSTGRESQL TEMP TABLE
def insert_to_postgres(df, batch_id):
    try:
        with psycopg2.connect(
            dbname = db_name,
            host = db_host,
            port = db_port,
            user = db_user,
            password = db_password
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute(schema_query)
                cursor.execute(temp_table_query)
                cursor.execute(latest_table_query)

                conn.commit()
        
        df.write.jdbc(url=postgres_url, table="news.temp", mode="append", properties=db_properties)

        with conn.cursor() as cursor:
            cursor.execute(merge_query)
            cursor.execute("TRUNCATE TABLE news.temp")
            conn.commit()

        print("Merge completed successfully")
    except Exception as e:
        print(f"Error during PostgreSQL operation: {e}")


sql_query = df_news.writeStream \
        .foreachBatch(insert_to_postgres) \
        .option("checkpointLocation", "/opt/spark-checkpoints/postgres") \
        .start()

kafka_query.awaitTermination()
sql_query.awaitTermination()