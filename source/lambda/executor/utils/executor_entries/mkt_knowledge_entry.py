import logging 
import json 
import os
import boto3
from functools import partial 
from textwrap import dedent
from langchain.schema.runnable import (
    RunnableBranch,
    RunnableLambda,
    RunnableParallel,
    RunnablePassthrough,
)
from langchain.retrievers import ContextualCompressionRetriever
from langchain.retrievers.merger_retriever import MergerRetriever
from ..intent_utils import IntentRecognitionAOSIndex
from ..llm_utils import LLMChain
from ..serialization_utils import JSONEncoder
from ..langchain_utils import chain_logger,RunnableDictAssign,RunnableParallelAssign
from ..constant import IntentType, CONVERSATION_SUMMARY_TYPE
import asyncio

from ..retriever import (
    QueryDocumentRetriever,
    QueryQuestionRetriever,
)
from .. import parse_config
from ..reranker import BGEReranker, MergeReranker
from ..context_utils import contexts_trunc,retriever_results_format,retriever_results_filter
from ..langchain_utils import RunnableDictAssign
from ..preprocess_utils import is_api_query, language_check,query_translate,get_service_name
from ..workspace_utils import WorkspaceManager

logger = logging.getLogger('mkt_knowledge_entry')
logger.setLevel(logging.INFO)

zh_embedding_endpoint = os.environ.get("zh_embedding_endpoint", "")
en_embedding_endpoint = os.environ.get("en_embedding_endpoint", "")
workspace_table = os.environ.get("workspace_table", "")

dynamodb = boto3.resource("dynamodb")
workspace_table = dynamodb.Table(workspace_table)
workspace_manager = WorkspaceManager(workspace_table)

def mkt_fast_reply(
        answer="很抱歉，我只能回答与亚马逊云科技产品和服务相关的咨询。",
        fast_info=""
    ):
    output = {
            "answer": answer,
            "sources": [],
            "contexts": [],
            "context_docs": [],
            "context_sources": []
    }
    logger.info(f'mkt_fast_reply: {fast_info}')
    return output
    
def market_chain_knowledge_entry(
    query_input: str,
    stream=False,
    manual_input_intent=None,
    event_body=None,
    rag_config=None,
    message_id=None
):
    """
    Entry point for the Lambda function.

    :param query_input: The query input.
    :param aos_index: The index of the AOS engine.
    :param stream(Bool): Whether to use llm stream decoding output.
    return: answer(str)
    """
    if rag_config is None:
        rag_config = parse_config.parse_mkt_entry_knowledge_config(event_body)

    assert rag_config is not None

    logger.info(f'market rag knowledge configs:\n {json.dumps(rag_config,indent=2,ensure_ascii=False,cls=JSONEncoder)}')

    workspace_ids = rag_config["retriever_config"]["workspace_ids"]
    qq_workspace_list = []
    qd_workspace_list = []
    for workspace_id in workspace_ids:
        workspace = workspace_manager.get_workspace(workspace_id)
        if not workspace or "index_type" not in workspace:
            logger.warning(f"workspace {workspace_id} not found")
            continue
        if workspace["index_type"] == "qq":
            qq_workspace_list.append(workspace)
        else:
            qd_workspace_list.append(workspace)

    debug_info = {}
    contexts = []
    sources = []
    answer = ""
    intent_info = {
        "manual_input_intent": manual_input_intent,
        "strict_qq_intent_result": {},
    }


    ################################################################################
    # step 1 conversation summary chain, rewrite query involve history conversation#
    ################################################################################
    
    conversation_query_rewrite_config = rag_config['query_process_config']['conversation_query_rewrite_config']
    cqr_llm_chain = LLMChain.get_chain(
        intent_type=CONVERSATION_SUMMARY_TYPE,
        **conversation_query_rewrite_config
    )
    cqr_llm_chain = RunnableBranch(
        # single turn
        (lambda x: not x['chat_history'],RunnableLambda(lambda x:x['query'])),
        cqr_llm_chain
    )

    conversation_summary_chain = chain_logger(
        RunnablePassthrough.assign(
            query=cqr_llm_chain
        ),
        "conversation_summary_chain",
        log_output_template='conversation_summary_chain result: {query}.',
        message_id=message_id
    )

    #######################
    # step 2 query preprocess#
    #######################
    translate_config = rag_config['query_process_config']['translate_config']
    translate_chain = RunnableLambda(
        lambda x: query_translate(
                  x['query'],
                  lang=x['query_lang'],
                  translate_config=translate_config
                  )
        )
    lang_check_and_translate_chain = RunnablePassthrough.assign(
        query_lang = RunnableLambda(lambda x:language_check(x['query']))
    )  | RunnablePassthrough.assign(translated_text=translate_chain)
    
    is_api_query_chain = RunnableLambda(lambda x:is_api_query(x['query']))
    service_names_recognition_chain = RunnableLambda(lambda x:get_service_name(x['query']))
    
    preprocess_chain = lang_check_and_translate_chain | RunnableParallelAssign(
        is_api_query=is_api_query_chain,
        service_names=service_names_recognition_chain
    )

    log_output_template=dedent("""
                               preprocess result:
                               query_lang: {query_lang}
                               translated_text: {translated_text}
                               is_api_query: {is_api_query} 
                               service_names: {service_names}
                            """)
    preprocess_chain = chain_logger(
        preprocess_chain,
        'preprocess query chain',
        message_id=message_id,
        log_output_template=log_output_template
    )

    #####################################
    # step 3.1 intent recognition chain #
    #####################################
    intent_recognition_index = IntentRecognitionAOSIndex(embedding_endpoint_name=zh_embedding_endpoint)
    intent_index_ingestion_chain = chain_logger(
        intent_recognition_index.as_ingestion_chain(),
        "intent_index_ingestion_chain",
        message_id=message_id
    )
    intent_index_check_exist_chain = RunnablePassthrough.assign(
        is_intent_index_exist = intent_recognition_index.as_check_index_exist_chain()
    )
    intent_index_search_chain = chain_logger(
        intent_recognition_index.as_search_chain(top_k=5),
        "intent_index_search_chain",
        message_id=message_id
    )
    inten_postprocess_chain = intent_recognition_index.as_intent_postprocess_chain(method='top_1')
    
    intent_search_and_postprocess_chain = intent_index_search_chain | inten_postprocess_chain
    intent_branch = RunnableBranch(
        (lambda x: not x['is_intent_index_exist'], intent_index_ingestion_chain | intent_search_and_postprocess_chain),
        intent_search_and_postprocess_chain
    )
    intent_recognition_chain = intent_index_check_exist_chain | intent_branch
    
    ####################
    # step 3.2 qq match#
    ####################
    qq_match_threshold = rag_config['retriever_config']['qq_config']['qq_match_threshold']
    retriever_list = [
        QueryQuestionRetriever(
            workspace,
            size=5
        )
        for workspace in qq_workspace_list
    ]
    qq_chain =  MergerRetriever(retrievers=retriever_list) | \
                RunnableLambda(retriever_results_format) |\
                RunnableLambda(partial(
                    retriever_results_filter,
                    threshold=qq_match_threshold
                ))

    ############################
    # step 4. qd retriever chain#
    ############################
    qd_config = rag_config['retriever_config']['qd_config']                     
    using_whole_doc = qd_config['using_whole_doc']
    context_num = qd_config['context_num']
    retriever_top_k = qd_config['retriever_top_k']
    reranker_top_k = qd_config['reranker_top_k']
    enable_reranker = qd_config['enable_reranker']

    retriever_list = [
        QueryDocumentRetriever(
            workspace=workspace,
            using_whole_doc=using_whole_doc,
            context_num=context_num,
            top_k=retriever_top_k,
            #   "zh", zh_embedding_endpoint
        )
        for workspace in qd_workspace_list
    ]

    lotr = MergerRetriever(retrievers=retriever_list)
    if enable_reranker:
        compressor = BGEReranker(top_n=reranker_top_k)
    else:
        compressor = MergeReranker(top_n=reranker_top_k)
    compression_retriever = ContextualCompressionRetriever(
        base_compressor=compressor, base_retriever=lotr
    )
    qd_chain = chain_logger(
        RunnablePassthrough.assign(
        docs=compression_retriever | RunnableLambda(retriever_results_format)
        ),
        "retrieve module",
        message_id=message_id
    )
    
    #####################
    # step 5. llm chain #
    #####################
    generator_llm_config = rag_config['generator_llm_config']
    context_num = generator_llm_config['context_num']
    llm_chain = RunnableDictAssign(lambda x: contexts_trunc(x['docs'],context_num=context_num)) |\
          RunnablePassthrough.assign(
               answer=LLMChain.get_chain(
                    intent_type=IntentType.KNOWLEDGE_QA.value,
                    stream=stream,
                    **generator_llm_config
                    ),
                chat_history=lambda x:rag_config['chat_history']
          )

    # llm_chain = chain_logger(llm_chain,'llm_chain', message_id=message_id)

    ###########################
    # step 6 synthesize chain #
    ###########################
     
    ######################
    # step 6.1 rag chain #
    ######################
    qd_match_threshold = rag_config['retriever_config']['qd_config']['qd_match_threshold']
    qd_fast_reply_branch = RunnablePassthrough.assign(
        filtered_docs = RunnableLambda(lambda x: retriever_results_filter(x['docs'],threshold=qd_match_threshold))
    ) | RunnableBranch(
        (
            lambda x: not x['filtered_docs'],
            RunnableLambda(lambda x: mkt_fast_reply(fast_info=x['filtered_docs']))
        ),
        llm_chain
    )

    rag_chain = qd_chain | qd_fast_reply_branch

    ######################################
    # step 6.2 fast reply based on intent#
    ######################################
    log_output_template=dedent("""
        qq_result num: {qq_result_num}
        intent recognition type: {intent_type}
    """)
    qq_and_intention_type_recognition_chain = chain_logger(
        RunnableParallelAssign(
            qq_result=qq_chain,
            intent_type=intent_recognition_chain,
        ) | RunnablePassthrough.assign(qq_result_num=lambda x:len(x['qq_result'])),
        "intention module",
        log_output_template=log_output_template,
        message_id=message_id
    )
    
    allow_intents = [
        IntentType.KNOWLEDGE_QA.value,
        IntentType.MARKET_EVENT.value
        ]
    qq_and_intent_fast_reply_branch = RunnableBranch(
        (lambda x: len(x['qq_result']) > 0, 
         RunnableLambda(
            lambda x: mkt_fast_reply(
                sorted(x['qq_result'],key=lambda x:x['score'],reverse=True)[0]['answer']
                ))
        ),
        (lambda x: x['intent_type'] not in allow_intents, RunnableLambda(lambda x: mkt_fast_reply())),
        rag_chain
    )

    #######################
    # step 6.3 full chain #
    #######################

    process_query_chain = conversation_summary_chain | preprocess_chain

    process_query_chain = chain_logger(
        process_query_chain,
        "query process module",
        message_id=message_id
    )

    qq_and_intent_fast_reply_branch = chain_logger(
        qq_and_intent_fast_reply_branch,
        "llm module",
        message_id=message_id
    )

    full_chain = process_query_chain | qq_and_intention_type_recognition_chain | qq_and_intent_fast_reply_branch

    response = asyncio.run(full_chain.ainvoke(
        {
            "query": query_input,
            "debug_info": debug_info,
            # "intent_type": intent_type,
            "intent_info": intent_info,
            "chat_history": rag_config['chat_history'],
            # "query_lang": "zh"
        }
    ))

    answer = response["answer"]
    sources = response["context_sources"]
    contexts = response["context_docs"]

    return answer, sources, contexts, debug_info