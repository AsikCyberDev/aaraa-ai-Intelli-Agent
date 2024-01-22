import os 
import sys
import time
try:
    from websocket import create_connection
except ModuleNotFoundError:
    os.system(f'{sys.executable} -m pip install websocket-client')
    from websocket import create_connection
import json 

# find ws_url from api gateway
ws_url = "wss://omjou492fe.execute-api.us-west-2.amazonaws.com/prod/"
ws = create_connection(ws_url)

question_library = [
    "IoT Core是否支持Qos2？",
    "在API Gateway REST API中，能否将JSON数据作为GET方法的请求体发送？",
    "Lambda Authorizer 上下文响应是否有大小限制？如果存在，限制是多少？",
    "Lambda Docker镜像的最大支持多少？"
    # "IoT Core是否支持Qos2？",
    # "如何在Amazon Forecast上导出已经训练好的模型，以便在其他地方部署？",
    "如何将Kinesis Data Streams配置为AWS Lambda的事件源？"
]

body = {
    "action": "sendMessage",
    "model": "knowledge_qa",
    "messages": [{"role": "user","content": question_library[-1]}],
    "temperature": 0.7,
    "type" : "market_chain", 
    # "enable_q_q_match": True,
    # "enable_debug": False,
    "llm_model_id":'anthropic.claude-v2:1',
    "get_contexts":True,
    # "session_id":f"test_{int(time.time())}"
}


# body = {
#     "action": "sendMessage",
#     "model": "chat",
#     "messages": [{"role": "user","content": "今天天气怎么样"}],
#     "temperature": 0.7,
#     "type" : "market_chain", 
#     # "enable_q_q_match": True,
#     # "enable_debug": False,
#     "llm_model_id":'anthropic.claude-v2:1',
#     "get_contexts":True,
#     # "session_id":f"test_{int(time.time())}"
# }


ws.send(json.dumps(body))
start_time = time.time()
while True:
    ret = json.loads(ws.recv())
    try:
        message_type = ret['choices'][0]['message_type']
    except:
        print(ret)
        print(f'total time: {time.time()-start_time}' )
        raise
    if message_type == "START":
        continue 
    elif message_type == "CHUNK":
        print(ret['choices'][0]['message']['content'],end="",flush=True)
    elif message_type == "END":
        break
    elif message_type == "ERROR":
        print(ret['choices'][0]['message']['content'])
        break 
    elif message_type == "CONTEXT":
        print()
        print('contexts',ret)
        # print('sources: ',ret['choices'][0]['knowledge_sources'])

ws.close()  