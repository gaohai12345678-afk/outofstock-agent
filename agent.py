from langchain.tools import tool
import os
import time
import json
from dotenv import load_dotenv
from langchain_deepseek import ChatDeepSeek
from langchain_core.messages import AnyMessage
from typing_extensions import TypedDict ,Annotated
from langchain_core.messages import SystemMessage
from langchain_core.messages import ToolMessage
from typing import Literal
from langgraph.graph import StateGraph, START, END
import operator

load_dotenv(override=True)
model = ChatDeepSeek(model="deepseek-chat", temperature=0, timeout=30, max_retries=2)


@tool
def query_order(order_id: str) -> str:
    """查询订单详情，返回订单里的商品和缺货情况"""
    return "订单A1000: 薯片(原味)*1 已缺货；可乐*1 正常"

@tool
def search_alternatives(item_name: str) -> str:
   """查询某个缺货商品的可替换商品清单和价格"""
   return "可替换商品: 薯片(麻辣味)7元、薯片(大包装) 9元、可乐 3元、酸奶 6元（同价）、雪碧6元(同价)"

#把工具挂到模型上
tools = [query_order, search_alternatives]
tools_by_name = {tool.name: tool for tool in tools}
model_with_tools = model.bind_tools(tools)

#第二步：定义state，记录消息和LLM调用次数
class MessageState(TypedDict):
    messages: Annotated[list[AnyMessage], operator.add]
    llm_calls: int

#第三步：定义模型节点
def llm_call(state:MessageState):
    """LLM决定是否调用工具。失败重试3次，仍失败则生成转人工兜底消息"""
    for attempt in range(1, 4):
        try:
            response = model_with_tools.invoke(
                [
                    SystemMessage(
                        content="你是闪购平台的缺货协商助手。商家打包时发现用户订单中的商品缺货，会触发一个缺货事件交给你处理。\n"
                                "工作流程（按顺序处理）：\n"
                                "1. 查询订单详情：用户不记得订单号，默认使用 A1000 订单\n"
                                "2. 查询缺货商品的可替换清单\n"
                                "2.1如果存在同价替代品，只推荐同价替代品，不要推荐需要补差价的商品。"
                                "3. 基于查询结果，生成一段【电话话术】\n"
                                "电话话术必须包含三部分：\n"
                                "- 说明缺货情况，向用户致歉\n"
                                "- 列出替代方案：价格以工具查询结果为准，不得编造；优先推荐同价或更优的选择\n"
                                "- 询问用户想换哪种，或选择退款\n"
                                "注意：你只负责生成话术和方案，用户最终选哪种由用户决定，你不执行任何修改订单的动作。 直接输出最终话术本身，不要输出推理过程"
                    )
                ]
                + state["messages"]
            )
            return {
                "messages": [response],
                "llm_calls": state.get("llm_calls", 0) + 1
            }
        except Exception as e:
            print(f"LLM 调用失败（第{attempt}次）：{type(e).__name__}，重试中…")
            time.sleep(1)
    # 3 次全失败，输出转人工兜底消息
    return {
        "messages": [SystemMessage(
            content="系统提示：智能服务暂时不可用，本次缺货事件请转人工处理。"
        )],
        "llm_calls": state.get("llm_calls", 0) + 1
    }

#第四步：定义工具节点
def tool_node(state:dict):
    """执行工具调用"""
    result = []
    for tool_call in state["messages"][-1].tool_calls:
        tool = tools_by_name.get(tool_call["name"])
        if tool is None:
            result.append(ToolMessage(
                content=f"错误：工具 {tool_call['name']} 不存在，请使用可用工具重新选择",
                tool_call_id=tool_call["id"]
            ))
            continue
        observation = tool.invoke(tool_call["args"])
        result.append(ToolMessage(content=observation, tool_call_id=tool_call["id"]))
    return {"messages": result}

#第五步：定义结束逻辑，整个agent的主循环由这里控制
def should_continue(state:MessageState):
    """判断是否继续调用工具"""
    messages = state["messages"]
    last_message = messages[-1]
    if getattr(last_message, "tool_calls", None):
        return "tool_node"
    return END


INTENT_PROMPT = """你是电商客服系统的意图识别器。根据用户回复判断意图，只输出JSON。

候选商品（item 必须使用这里的规范名：酸奶、雪碧、麻辣、大包装；用户要的商品不在列表里时，用用户的原话）：
酸奶6元、雪碧6元、麻辣7元、大包装9元

意图枚举：
- refund：用户想退款/不要了。注意否定语义："别退款""不用退"不是refund
- pick_specific：用户指定了要换的具体商品（包括不在候选列表里的，如"方便面"）
- accept：同意换，但没指定商品
- unsure：让系统自己决定（随便/看着办）
- unknown：发泄情绪、答非所问、无法判断

输出格式（严格JSON，不要输出其他任何内容）：
{"intent": "意图", "item": "商品名或null"}

示例：
"可以，换吧" → {"intent": "accept", "item": null}
"别退款，换一个" → {"intent": "accept", "item": null}
"帮我换成雪碧" → {"intent": "pick_specific", "item": "雪碧"}
"退了吧" → {"intent": "refund", "item": null}
"""

def classify_intent(reply: str) -> tuple[str, str | None]:
    """识别用户意图，返回 (意图, 指定的商品名)。LLM语义分类，失败兜底转人工"""
    try:
        response = model.invoke(
            [SystemMessage(content=INTENT_PROMPT), HumanMessage(content=reply)]
        )
        raw = response.content.strip()
        # 模型可能用```json ```包裹，直接截取第一个{到最后一个}
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        intent = data.get("intent")
        item = data.get("item")
        if intent not in ("refund", "pick_specific", "accept", "unsure", "unknown"):
            intent = "unknown"
        if not item:  # None、空串、"null" 都归一化
            item = None
        return (intent, item)
    except Exception as e:
        print(f"意图识别失败：{type(e).__name__}，降级转人工")
        return ("unknown", None)


ALTERNATIVES = {
    "酸奶": 6,
    "麻辣": 7,
    "大包装": 9,
    "雪碧": 6,
}
ORIGINAL_PRICE = 6
CONFIRM_PRICE_THRESHOLD = 10  # 换货金额达到该值时需要二次确认（错误成本超过打扰成本）
# 推荐商品：候选中第一个同价商品，动态计算。映射/话术/执行共用这一真相源（P3 修复）
RECOMMENDED_ITEM = next(
    (name for name, price in ALTERNATIVES.items() if price == ORIGINAL_PRICE), None
)

def map_to_action(intent: str, item: str | None) -> tuple[str, bool]:
    """意图映射为 (动作, 是否需要二次确认)"""
    if intent == "refund":
        return ("退款薯片(原味) 6元", True)
    if intent == "pick_specific":
        if item not in ALTERNATIVES:
            return (f"告知用户：{item}不在可换商品范围内", False)
        price = ALTERNATIVES[item]
        if price == ORIGINAL_PRICE:
            return (f"换{item}（同价）", price >= CONFIRM_PRICE_THRESHOLD)
        return (f"换{item}（需补差价，暂不支持）", False)
    if intent in ("accept", "unsure"):
        # 推荐商品动态取自 RECOMMENDED_ITEM，确认门槛看它的价格
        return (f"按建议换{RECOMMENDED_ITEM}（同价）", ALTERNATIVES[RECOMMENDED_ITEM] >= CONFIRM_PRICE_THRESHOLD)
    return ("转人工客服", False)

def modify_order(order_id: str, new_item: str, price: int) -> str:
    """修改订单，把缺货商品替换为同价替代品"""
    return f"订单{order_id} 已替换为 {new_item}（{price}元），订单完成"

def refund_order(order_id: str, item_name: str, price: int) -> str:
    """退款订单中的缺货商品"""
    return f"订单{order_id} 的 {item_name}（{price}元）已退款，订单完成"

def escalate(reason: str) -> str:
    """转人工客服"""
    return f"已转人工客服，原因：{reason}"



#第六步：构造并编译agent

#构建工作流
agent_builder = StateGraph(MessageState)

#添加节点
agent_builder.add_node("llm_call", llm_call)
agent_builder.add_node("tool_node", tool_node)

#添加边连接节点
agent_builder.add_edge(START, "llm_call")
agent_builder.add_conditional_edges(
    "llm_call",
    should_continue,
    ["tool_node", END]
)
agent_builder.add_edge("tool_node", "llm_call")

agent = agent_builder.compile()

#运行
from langchain_core.messages import HumanMessage
item = input("商家：请输入缺货商品名称：")
messages = [HumanMessage(
    content=f"缺货事件：订单 A1000 中商品【{item}】缺货，请处理"
)]
message = agent.invoke({"messages": messages},config={"recursion_limit": 15})
final_message = message["messages"][-1]
print("\n【电话接通】")
print(final_message.content)
print()

# 兜底防御：智能服务不可用已转人工，自动协商流程终止
if isinstance(final_message, SystemMessage):
    print("本次缺货事件已转人工处理，自动流程终止。")
    exit()

user_reply = input("用户：")

max_rounds = 3
rounds = 0
while rounds < max_rounds:
    rounds += 1
    intent, item = classify_intent(user_reply)
    print(f"识别意图：{intent}，指定商品：{item}")
    action, need_confirm = map_to_action(intent, item)
    print(f"内部映射：{action}，需二次确认：{need_confirm}")

    # 二次确认：涉及钱的操作、或大额换货必须先问
    if need_confirm:
        confirm = input(f"确认：{action}，是否执行？（是/否）：")
        if confirm not in ("是", "好", "确认", "嗯", "ok", "OK"):
            # 被拒话术要通用：可能是拒退款，也可能是拒大额换货；推荐商品用变量不写死
            print(f"好的，本次不为您办理：{action}。请问您是愿意换成同价的{RECOMMENDED_ITEM}，还是有其他想法呢？")
            user_reply = input("用户：")
            continue


    # 执行动作
    if intent == "refund":
        print(">>> 走了分支：refund → 退款")
        result = refund_order("A1000", "薯片(原味)", 6)
    elif intent == "pick_specific":
        # 单一真相源：同价与否只看 ALTERNATIVES，不在此处硬编码商品清单
        if item in ALTERNATIVES and ALTERNATIVES[item] == ORIGINAL_PRICE:
            print(">>> 走了分支：pick_specific(同价) → 改单")
            result = modify_order("A1000", item, ALTERNATIVES[item])
        elif item in ALTERNATIVES:
            print(">>> 走了分支：pick_specific(差价) → 转人工")
            result = escalate(f"用户选择的{item}需补差价，暂不支持")
        else:
            print(">>> 走了分支：pick_specific(不在候选) → 转人工")
            result = escalate(f"用户指定的{item}不在可换商品范围内")
    elif intent in ("accept", "unsure"):
        print(">>> 走了分支：accept/unsure → 按建议改单")
        result = modify_order("A1000", RECOMMENDED_ITEM, ALTERNATIVES[RECOMMENDED_ITEM])
    else:
        print(">>> 走了分支：unknown → 转人工")
        result = escalate("无法识别用户意图")
    print(f"执行结果：{result}")
    break
else:
    print("对话多次未达成一致，转人工处理")
    print(escalate("用户多次确认未达成一致"))



