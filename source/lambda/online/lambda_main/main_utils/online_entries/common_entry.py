import json
from typing import Annotated, Any, TypedDict

from common_logic.common_utils.constant import (
    LLMTaskType,
    ChatbotMode,
    ToolRuningMode
)
from common_logic.common_utils.exceptions import (
    ToolNotExistError, 
    ToolParameterNotExistError,
    MultipleToolNameError,
    ToolNotFound
)

from common_logic.common_utils.lambda_invoke_utils import (
    invoke_lambda,
    is_running_local,
    node_monitor_wrapper,
    send_trace,
)
from common_logic.common_utils.python_utils import add_messages, update_nest_dict
from common_logic.common_utils.logger_utils import get_logger
from common_logic.common_utils.prompt_utils import get_prompt_templates_from_ddb
from common_logic.common_utils.serialization_utils import JSONEncoder
from functions.tool_calling_parse import parse_tool_calling as _parse_tool_calling
from functions.tool_execute_result_format import format_tool_call_results
from functions import get_tool_by_name
from lambda_main.main_utils.parse_config import parse_common_entry_config
from langgraph.graph import END, StateGraph
from functions.lambda_common_tools.knowledge_base_retrieve import knowledge_base_retrieve

logger = get_logger('common_entry')
class ChatbotState(TypedDict):
    chatbot_config: dict  # chatbot config
    query: str
    ws_connection_id: str
    stream: bool
    query_rewrite: str = None  # query rewrite ret
    intent_type: str = None  # intent
    intention_fewshot_examples: list
    trace_infos: Annotated[list[str], add_messages]
    message_id: str = None
    chat_history: Annotated[list[dict], add_messages]
    agent_chat_history: Annotated[list[dict], add_messages]
    debug_infos: Annotated[dict, update_nest_dict]
    answer: Any  # final answer
    current_monitor_infos: str
    extra_response: Annotated[dict, update_nest_dict]
    contexts: str = None
    all_index_retriever_contexts: list
    current_agent_output: dict
    parse_tool_calling_ok: bool
    enable_trace: bool
    format_intention: str
    ########### function calling parameters ###########
    # 
    current_function_calls: list[str]
    # current_tool_execute_res: dict
    current_intent_tools: list
    current_tool_calls: list
    current_tool_name: str
    is_current_tool_calling_once: bool
    
    # valid_tool_calling_names: list[str]
    # parameters to monitor the running of agent
    agent_recursion_limit: int # the maximum number that tool_plan_and_results_generation node can be called
    agent_recursion_validation: bool
    current_agent_recursion_num: int #


####################
# nodes in lambdas #
####################

@node_monitor_wrapper
def query_preprocess(state: ChatbotState):
    output: str = invoke_lambda(
        event_body=state,
        lambda_name="Online_Query_Preprocess",
        lambda_module_path="lambda_query_preprocess.query_preprocess",
        handler_name="lambda_handler",
    )

    send_trace(f"\n\n**query_rewrite:** \n{output}", state["stream"], state["ws_connection_id"], state["enable_trace"])
    return {"query_rewrite": output}

@node_monitor_wrapper
def intention_detection(state: ChatbotState):
    intention_fewshot_examples = invoke_lambda(
        lambda_module_path="lambda_intention_detection.intention",
        lambda_name="Online_Intention_Detection",
        handler_name="lambda_handler",
        event_body=state,
    )

    current_intent_tools: list[str] = list(
        set([e["intent"] for e in intention_fewshot_examples])
    )

    send_trace(
        f"**intention retrieved:**\n{json.dumps(intention_fewshot_examples,ensure_ascii=False,indent=2)}", state["stream"], state["ws_connection_id"], state["enable_trace"])
    return {
        "intention_fewshot_examples": intention_fewshot_examples,
        "current_intent_tools": current_intent_tools,
        "intent_type": "intention detected",
    }

@node_monitor_wrapper
def llm_rag_results_generation(state: ChatbotState):
    group_name = state['chatbot_config']['group_name']
    llm_config = state["chatbot_config"]["rag_config"]["llm_config"]
    task_type = LLMTaskType.RAG
    prompt_templates_from_ddb = get_prompt_templates_from_ddb(
        group_name,
        model_id = llm_config['model_id'],
    ).get(task_type,{})

    output: str = invoke_lambda(
        lambda_name="Online_LLM_Generate",
        lambda_module_path="lambda_llm_generate.llm_generate",
        handler_name="lambda_handler",
        event_body={
            "llm_config": {
                **prompt_templates_from_ddb,
                **llm_config,
                "stream": state["stream"],
                "intent_type": task_type,
            },
            "llm_input": {
                "contexts": [state["contexts"]],
                "query": state["query"],
                "chat_history": state["chat_history"],
            },
        },
    )
    return {"answer": output}


@node_monitor_wrapper
def tools_choose_and_results_generation(state: ChatbotState):
    # check once tool calling
    current_agent_output:dict = invoke_lambda(
        event_body={
            **state,
            # "other_chain_kwargs": {"system_prompt": get_common_system_prompt()}
            },
        lambda_name="Online_Agent",
        lambda_module_path="lambda_agent.agent",
        handler_name="lambda_handler",
   
    )
    current_agent_recursion_num = state['current_agent_recursion_num'] + 1
    agent_recursion_validation = state['current_agent_recursion_num'] < state['agent_recursion_limit']

    send_trace(f"\n\n**current_agent_output:** \n{json.dumps(current_agent_output['agent_output'],ensure_ascii=False,indent=2)}\n\n **current_agent_recursion_num:** {current_agent_recursion_num}", state["stream"], state["ws_connection_id"])
    return {
        "current_agent_output": current_agent_output,
        "current_agent_recursion_num": current_agent_recursion_num,
        "agent_recursion_validation": agent_recursion_validation
    }


@node_monitor_wrapper
def agent(state: ChatbotState):
    # two cases to invoke rag function
    # 1. when valid intention fewshot found
    # 2. for the first time, agent decides to give final results
    no_intention_condition = not state['intention_fewshot_examples']
    first_tool_final_response = False
    if (state['current_agent_recursion_num'] == 1) and state['parse_tool_calling_ok'] and state['agent_chat_history']:
        tool_execute_res = state['agent_chat_history'][-1]['additional_kwargs']['raw_tool_call_results'][0]
        tool_name = tool_execute_res['name']
        if tool_name == "give_final_response":
            first_tool_final_response = True

    if no_intention_condition or first_tool_final_response:
        send_trace("no clear intention, switch to rag")
        contexts = knowledge_retrieve(state)['contexts']
        state['contexts'] = contexts
        answer:str = llm_rag_results_generation(state)['answer']
        return {
            "answer": answer,
            "is_current_tool_calling_once": True
        }

    # deal with once tool calling
    if state['agent_recursion_validation'] and state['parse_tool_calling_ok'] and state['agent_chat_history']:
        tool_execute_res = state['agent_chat_history'][-1]['additional_kwargs']['raw_tool_call_results'][0]
        tool_name = tool_execute_res['name']
        output = tool_execute_res['output']
        tool = get_tool_by_name(tool_name)
        if tool.running_mode == ToolRuningMode.ONCE:
            send_trace("once tool")
            return {
                "answer": str(output['result']),
                "is_current_tool_calling_once": True
            }

    response = app_agent.invoke(state)

    return response


@node_monitor_wrapper
def results_evaluation(state: ChatbotState):
    # parse tool_calls:
    try:
        output = _parse_tool_calling(
            agent_output=state['current_agent_output']
        )
        tool_calls = output['tool_calls']
        send_trace(f"\n\n**tool_calls parsed:** \n{tool_calls}", state["stream"], state["ws_connection_id"], state["enable_trace"])
        if not state["extra_response"].get("current_agent_intent_type", None):
            state["extra_response"]["current_agent_intent_type"] = output['tool_calls'][0]["name"]
       
        return {
            "parse_tool_calling_ok": True,
            "current_tool_calls": tool_calls,
            "agent_chat_history": [output['agent_message']]
        }
    
    except (ToolNotExistError,
             ToolParameterNotExistError,
             MultipleToolNameError,
             ToolNotFound
             ) as e:
        send_trace(f"\n\n**tool_calls parse failed:** \n{str(e)}", state["stream"], state["ws_connection_id"], state["enable_trace"])
        return {
            "parse_tool_calling_ok": False,
            "agent_chat_history":[
                e.agent_message,
                e.error_message
            ]
        }


@node_monitor_wrapper
def tool_execution(state: ChatbotState):
    tool_calls = state['current_tool_calls']
    assert len(tool_calls) == 1, tool_calls
    tool_call_results = []
    for tool_call in tool_calls:
        tool_name = tool_call["name"]
        tool_kwargs = tool_call['kwargs']
        # call tool
        output = invoke_lambda(
            event_body = {
                "tool_name":tool_name,
                "state":state,
                "kwargs":tool_kwargs
                },
            lambda_name="Online_Tool_Execute",
            lambda_module_path="functions.lambda_tool",
            handler_name="lambda_handler"   
        )
        tool_call_results.append({
            "name": tool_name,
            "output": output,
            "kwargs": tool_call['kwargs'],
            "model_id": tool_call['model_id']
        })
    
    output = format_tool_call_results(tool_calls[0]['model_id'],tool_call_results)
    send_trace(f'**tool_execute_res:** \n{output["tool_message"]["content"]}')
    return {"agent_chat_history": [output['tool_message']]}


@node_monitor_wrapper
def rag_all_index_lambda(state: ChatbotState):
    # call retrivever
    retriever_params = state["chatbot_config"]["rag_config"]["retriever_config"]
    retriever_params["query"] = state["query"]
    output: str = invoke_lambda(
        event_body=retriever_params,
        lambda_name="Online_Function_Retriever",
        lambda_module_path="functions.lambda_retriever.retriever",
        handler_name="lambda_handler",
    )
    contexts = [doc["page_content"] for doc in output["result"]["docs"]]
    return {"contexts": contexts}


knowledge_retrieve = rag_all_index_lambda

@node_monitor_wrapper
def llm_direct_results_generation(state: ChatbotState):
    group_name = state['chatbot_config']['group_name']
    llm_config = state["chatbot_config"]["chat_config"]
    task_type = LLMTaskType.CHAT

    prompt_templates_from_ddb = get_prompt_templates_from_ddb(
        group_name,
        model_id = llm_config['model_id'],
    ).get(task_type,{})
    logger.info(prompt_templates_from_ddb)

    answer: dict = invoke_lambda(
        event_body={
            "llm_config": {
                **llm_config,
                "stream": state["stream"],
                "intent_type": task_type,
                **prompt_templates_from_ddb
            },
            "llm_input": {
                "query": state["query"],
                "chat_history": state["chat_history"],
               
            },
        },
        lambda_name="Online_LLM_Generate",
        lambda_module_path="lambda_llm_generate.llm_generate",
        handler_name="lambda_handler",
    )
    return {"answer": answer}

def final_results_preparation(state: ChatbotState):
    return {"answer": state['answer']}


def matched_query_return(state: ChatbotState):
    return {"answer": state["answer"]}

################
# define edges #
################

def query_route(state: dict):
    return f"{state['chatbot_config']['chatbot_mode']} mode"


def intent_route(state: dict):
    # if not state['intention_fewshot_examples']:
    #     state['extra_response']['current_agent_intent_type'] = 'final_rag'
    #     return 'no clear intention'
    return state["intent_type"]

def agent_route(state: dict):
    if state.get("is_current_tool_calling_once",False):
        return "no need tool calling"

    state["agent_recursion_validation"] = state['current_agent_recursion_num'] < state['agent_recursion_limit']
    # if state["parse_tool_calling_ok"]:
    #     state["current_tool_name"] = state["current_tool_calls"][0]["name"]
    # else:
    #     state["current_tool_name"] = ""

    # if state["agent_recursion_validation"] and not state["parse_tool_calling_ok"]:
    #     return "invalid tool calling"

    if state["agent_recursion_validation"]:
        return "valid tool calling"
        # if state["current_tool_name"] in ["QA", "service_availability", "explain_abbr"]:
        #     return "force to retrieve all knowledge"
        # elif state["current_tool_name"] in state["valid_tool_calling_names"]:
        #     return "valid tool calling"
        # else:
        #     return "no need tool calling"
    else:
        # TODO give final strategy
        raise RuntimeError

#############################
# define online top-level graph for app #
#############################
app = None

def build_graph():
    workflow = StateGraph(ChatbotState)
    # add node for all chat/rag/agent mode
    workflow.add_node("query_preprocess", query_preprocess)
    # chat mode
    workflow.add_node("llm_direct_results_generation", llm_direct_results_generation)
    # rag mode
    workflow.add_node("knowledge_retrieve", knowledge_retrieve)
    workflow.add_node("llm_rag_results_generation", llm_rag_results_generation)
    # agent mode
    workflow.add_node("intention_detection", intention_detection)
    workflow.add_node("matched_query_return", matched_query_return)
    # agent sub graph
    workflow.add_node("agent", agent)
    workflow.add_node("tools_execution", tool_execution)
    workflow.add_node("final_results_preparation", final_results_preparation)

    # add all edges
    workflow.set_entry_point("query_preprocess")
    # chat mode
    workflow.add_edge("llm_direct_results_generation", END)
    # rag mode
    workflow.add_edge("knowledge_retrieve", "llm_rag_results_generation")
    workflow.add_edge("llm_rag_results_generation", END)
    # agent mode
    workflow.add_edge("tools_execution", "agent")
    workflow.add_edge("matched_query_return", "final_results_preparation")
    workflow.add_edge("final_results_preparation", END)

    # add conditional edges
    # choose running mode based on user selection:
    # 1. chat mode: let llm generate results directly
    # 2. rag mode: retrive all knowledge and let llm generate results
    # 3. agent mode: let llm generate results based on intention detection, tool calling and retrieved knowledge
    workflow.add_conditional_edges(
        "query_preprocess",
        query_route,
        {
            "chat mode": "llm_direct_results_generation",
            "rag mode": "knowledge_retrieve",
            "agent mode": "intention_detection",
        },
    )

    # three running branch will be chosen based on intention detection results:
    # 1. similar query found: if very similar queries were found in knowledge base, these queries will be given as results
    # 2. intention detected: if intention detected, the agent logic will be invoked
    workflow.add_conditional_edges(
        "intention_detection",
        intent_route,
        {
            "similar query found": "matched_query_return",
            "intention detected": "agent",
        },
    )

    # the results of agent planning will be evaluated and decide next step:
    # 1. valid tool calling: the agent chooses the valid tools, and the tools will be executed
    # 2. no need tool calling: the agent thinks no tool needs to be called, the final results can be generated
    workflow.add_conditional_edges(
        "agent",
        agent_route,
        {
            "valid tool calling": "tools_execution",
            "no need tool calling": "final_results_preparation",
        },
    )

    app = workflow.compile()
    return app

#############################
# define online sub-graph for agent #
#############################
app_agent = None

def build_agent_graph():
    def _results_evaluation_route(state: dict):
        #TODO: pass no need tool calling or valid tool calling?
        if state["agent_recursion_validation"] and not state["parse_tool_calling_ok"]:
            return "invalid tool calling"
        return "continue"

    workflow = StateGraph(ChatbotState)
    workflow.add_node("tools_choose_and_results_generation", tools_choose_and_results_generation)
    workflow.add_node("results_evaluation", results_evaluation)

    # add all edges
    workflow.set_entry_point("tools_choose_and_results_generation")
    workflow.add_edge("tools_choose_and_results_generation","results_evaluation")

    # add conditional edges
    # the results of agent planning will be evaluated and decide next step:
    # 1. invalid tool calling: if agent makes clear mistakes, like wrong tool names or format, it will be forced to plan again
    # 2. valid tool calling: the agent chooses the valid tools
    workflow.add_conditional_edges(
        "results_evaluation",
        _results_evaluation_route,
        {
            "invalid tool calling": "tools_choose_and_results_generation",
            "continue": END,
        }
    )
    app = workflow.compile()
    return app

def common_entry(event_body):
    """
    Entry point for the Lambda function.
    :param event_body: The event body for lambda function.
    return: answer(str)
    """
    global app,app_agent
    if app is None:
        app = build_graph()
    
    if app_agent is None:
        app_agent = build_agent_graph()

    # debuging
    if is_running_local():
        with open("common_entry_workflow.png", "wb") as f:
            f.write(app.get_graph().draw_mermaid_png())
        
        with open("common_entry_agent_workflow.png", "wb") as f:
            f.write(app_agent.get_graph().draw_mermaid_png())
            
    ################################################################################
    # prepare inputs and invoke graph
    event_body["chatbot_config"] = parse_common_entry_config(
        event_body["chatbot_config"]
    )
    logger.info(f'event_body:\n{json.dumps(event_body,ensure_ascii=False,indent=2,cls=JSONEncoder)}')
    chatbot_config = event_body["chatbot_config"]
    query = event_body["query"]
    use_history = chatbot_config["use_history"]
    chat_history = event_body["chat_history"] if use_history else []
    stream = event_body["stream"]
    message_id = event_body["custom_message_id"]
    ws_connection_id = event_body["ws_connection_id"]
    enable_trace = chatbot_config["enable_trace"]

    # invoke graph and get results
    response = app.invoke(
        {
            "stream": stream,
            "chatbot_config": chatbot_config,
            "query": query,
            "enable_trace": enable_trace,
            "trace_infos": [],
            "message_id": message_id,
            "chat_history": chat_history,
            "agent_chat_history": [],
            "ws_connection_id": ws_connection_id,
            "debug_infos": {},
            "extra_response": {},
            "agent_recursion_limit": chatbot_config['agent_recursion_limit'],
            "current_agent_recursion_num": 0,
        }
    )

    return {"answer": response["answer"], **response["extra_response"]}


main_chain_entry = common_entry
