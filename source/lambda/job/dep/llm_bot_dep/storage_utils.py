"""
Helper functions for storage intermediate content or log
"""

import datetime
import json
import logging
import sys
import time
from typing import List
from urllib.parse import urlparse
from botocore.exceptions import ClientError

from langchain.docstore.document import Document

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def convert_to_logger(document: Document) -> str:
    # TODO: Convert the document to a logger file format, customize if possible
    logger_content = "Page Content: " + document.page_content + "\n"
    logger_content += "Metadata: " + json.dumps(document.metadata, ensure_ascii=False)

    return logger_content


def upload_chunk_to_s3(
    s3, logger_content: str, bucket: str, prefix: str, splitting_type: str
):
    """Upload the logger file to S3 with hierarchy below:
    filename A
        ├── semantic-splitting (split by headers)
        │   ├── timestamp 1
        │   │   ├── logger file 1
        │   ├── timestamp 2
        │   │   ├── logger file 2
        ├── chunk-size-splitting (split by chunk size)
        │   ├── timestamp 3
        │   │   ├── logger file 3
        │   ├── timestamp 4
        │   │   ├── logger file 4
        ├── before-splitting (whole markdown content before splitting)
        │   ├── timestamp 5
        │   │   ├── logger file 5
        │   ├── timestamp 6
        │   │   ├── logger file 6
        ├── qa-enhancement (QA enhanced content generated by LLM)
        │   ├── timestamp 7
        │   │   ├── logger file 7
        │   ├── timestamp 8
        │   │   ├── logger file 8
        ...
    """
    # round the timestamp to hours to avoid too many folders
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d-%H")
    # make the logger file name unique
    object_key = f"{prefix}/{splitting_type}/{timestamp}/{datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S-%f')}.log"
    try:
        res = s3.put_object(Bucket=bucket, Key=object_key, Body=logger_content)
        logger.debug(f"Upload logger file to S3: {res}")
    except Exception as e:
        logger.error(f"Error uploading logger file to S3: {e}")


def save_content_to_s3(s3, document: Document, res_bucket: str, splitting_type: str):
    """Save content to S3 bucket

    Args:
        document (Document): The page document to be saved
        res_bucket (str): Target S3 bucket
        s3 (_type_): S3 client
    """
    logger_file = convert_to_logger(document)
    # Extract the filename from the file_path in the metadata
    file_path = document.metadata.get("file_path", "")
    # filename = file_path.split('/')[-1].split('.')[0]
    filename = file_path.replace("s3://", "").replace("/", "-").replace(".", "-")
    # RecursiveCharacterTextSplitter have been rewrite to split based on chunk size & overlap, use separate folder to store the logger file
    upload_chunk_to_s3(s3, logger_file, res_bucket, filename, splitting_type)


def _s3_uri_exist(s3_client, s3_uri: str) -> bool:
    """Checks if an object exists at a given S3 URI. 
    eg. s3://bucket/folder/file.csv

    Args:
        s3_uri: s3 URI to the s3 object

    Returns:
        bool: whether the object exists or not
    """
    parsed = urlparse(s3_uri)

    try:
        s3_client.head_object(Bucket=parsed.netloc, Key=parsed.path.lstrip('/'))
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        else:
            raise Exception("Failed to get S3 object during ETL inference")
