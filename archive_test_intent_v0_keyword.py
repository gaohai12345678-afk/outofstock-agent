# test_intent.py
def classify_intent(reply: str) -> tuple[str, str | None]:
    """识别用户意图，返回 (意图, 指定的商品名)"""
    if "退" in reply or "不要了" in reply or "算了" in reply:
        return ("refund", None)
    for item in ["酸奶", "麻辣", "大包装"]:
        if item in reply:
            return ("pick_specific", item)
    if "换" in reply or "行" in reply or "可以" in reply or "好" in reply:
        return ("accept", None)
    if "随便" in reply or "看着办" in reply or "你定" in reply:
        return ("unsure", None)
    return ("unknown", None)

ALTERNATIVES = {
    "酸奶": 6,
    "麻辣": 7,
    "大包装": 9,
}
ORIGINAL_PRICE = 6

def map_to_action(intent: str, item: str | None) -> tuple[str, bool]:
    """意图映射为 (动作, 是否需要二次确认)"""
    if intent == "refund":
        return ("退款薯片(原味) 6元", True)
    if intent == "pick_specific":
        price = ALTERNATIVES.get(item, 0)
        if price == ORIGINAL_PRICE:
            return (f"换{item}（同价）", False)
        return (f"换{item}（差价 {price - ORIGINAL_PRICE:+d} 元）", True)
    if intent in ("accept", "unsure"):
        return ("按建议换酸奶（同价）", False)
    return ("转人工客服", False)


# 测试意图识别
for test in ["换酸奶吧", "退款吧", "算了不要了", "帮我换大包装", "嗯好的", "随便吧", "你们服务真差"]:
    print(f"{test} -> {classify_intent(test)}")

# 测试内部映射
print(map_to_action("pick_specific", "酸奶"))
print(map_to_action("pick_specific", "麻辣"))
print(map_to_action("refund", None))
print(map_to_action("unknown", None))

