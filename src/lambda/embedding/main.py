import os
import sys
import time
import json
import logging
import numpy as np
import boto3, json

from langchain.document_loaders import S3DirectoryLoader
from langchain.vectorstores import OpenSearchVectorSearch
from sm_utils import create_sagemaker_embeddings_from_js_model
from langchain.text_splitter import RecursiveCharacterTextSplitter
from requests_aws4auth import AWS4Auth
from aos_utils import OpenSearchClient

# global constants
MAX_FILE_SIZE = 1024*1024*100 # 100MB
MAX_OS_DOCS_PER_PUT = 500
CHUNK_SIZE_FOR_DOC_SPLIT = 600
CHUNK_OVERLAP_FOR_DOC_SPLIT = 20

logger = logging.getLogger()
logging.basicConfig(format='%(asctime)s,%(module)s,%(processName)s,%(levelname)s,%(message)s', level=logging.INFO, stream=sys.stderr)

# fetch all the environment variables
_document_bucket = os.environ.get('document_bucket')
_embeddings_model_endpoint_name = os.environ.get('embeddings_model_endpoint_name')
_opensearch_cluster_domain = os.environ.get('opensearch_cluster_domain')

s3 = boto3.resource('s3')
aws_region = boto3.Session().region_name
document_bucket = s3.Bucket(_document_bucket)
credentials = boto3.Session().get_credentials()
awsauth = AWS4Auth(credentials.access_key, credentials.secret_key, aws_region, 'es', session_token=credentials.token)

def process_shard(shard, embeddings_model_endpoint_name, aws_region, os_index_name, os_domain_ep, os_http_auth) -> int: 
    logger.info(f'Starting process_shard of {len(shard)} chunks.')
    st = time.time()
    embeddings = create_sagemaker_embeddings_from_js_model(embeddings_model_endpoint_name, aws_region)
    docsearch = OpenSearchVectorSearch(index_name=os_index_name,
                                       embedding_function=embeddings,
                                       opensearch_url=os_domain_ep,
                                       http_auth=os_http_auth)    
    docsearch.add_documents(documents=shard)
    et = time.time() - st
    logger.info(f'Shard completed in {et} seconds.')
    return 0

def lambda_handler(event, context):
    request_timestamp = time.time()
    logger.info(f'request_timestamp :{request_timestamp}')
    logger.info(f"event:{event}")
    logger.info(f"context:{context}")
    # parse aos endpoint from event
    index_name = event['aos_index']
    aos_client = OpenSearchClient(_opensearch_cluster_domain)

    # iterate all files within specific s3 prefix in bucket llm-bot-documents and print out file number and total size
    prefix = event['document_prefix']
    total_size = 0
    total_files = 0
    for obj in document_bucket.objects.filter(Prefix=prefix):
        total_files += 1
        total_size += obj.size
    logger.info(f'total_files:{total_files}, total_size:{total_size}')

    # raise error and return if the total size is larger than 100MB
    if total_size > MAX_FILE_SIZE:
        raise Exception(f'total_size:{total_size} is larger than {MAX_FILE_SIZE}')

    loader = S3DirectoryLoader(_document_bucket, prefix=prefix)
    text_splitter = RecursiveCharacterTextSplitter(
        # Set a really small chunk size, just to show.
        chunk_size = CHUNK_SIZE_FOR_DOC_SPLIT,
        chunk_overlap = CHUNK_OVERLAP_FOR_DOC_SPLIT,
        length_function = len,
    )

    # split all docs into chunks
    st = time.time()
    logger.info('Loading documents ...')
    docs = loader.load()

    # add a custom metadata field, timestamp and embeddings_model
    for doc in docs:
        doc.metadata['timestamp'] = time.time()
        doc.metadata['embeddings_model'] = _embeddings_model_endpoint_name
    chunks = text_splitter.create_documents([doc.page_content for doc in docs], metadatas=[doc.metadata for doc in docs])
    et = time.time() - st
    logger.info(f'Time taken: {et} seconds. {len(chunks)} chunks generated') 

    st = time.time()
    db_shards = (len(chunks) // MAX_OS_DOCS_PER_PUT) + 1
    logger.info(f'Loading chunks into vector store ... using {db_shards} shards') 
    shards = np.array_split(chunks, db_shards)

    exists = aos_client.indices.exists(index_name)
    logger.info(f"index_name={index_name}, exists={exists}")
    
    embeddings = create_sagemaker_embeddings_from_js_model(_embeddings_model_endpoint_name, aws_region)

    docsearch = OpenSearchVectorSearch.from_documents(index_name = index_name,
                                                        documents = shards[0],
                                                        embedding = embeddings,
                                                        opensearch_url = _opensearch_cluster_domain,
                                                        http_auth = awsauth)
    shard_start_index = 1  
    process_shard(shards[shard_start_index:], _embeddings_model_endpoint_name, aws_region, index_name, _opensearch_cluster_domain, awsauth)

    et = time.time() - st
    logger.info(f'Time taken: {et} seconds. all shards processed')

    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps({
            "created": request_timestamp,
            "model": _embeddings_model_endpoint_name,            
        })
    }
