"""从 DSH session 重建可读对话，并单独抽取用户消息（思考轨迹分析的原料）。"""
import json
import os

OUT_DIR = r"D:\工作区表\工作区4\协同进化\思考轨迹分析"
SRC = os.path.join(OUT_DIR, "session_原始.jsonl")

recs = []
with open(SRC, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            recs.append(json.loads(line))

print(f"records: {len(recs)}")


def text_of(message):
    """把 message.content 拼成文本；tool-call 简单标注。"""
    parts = []
    c = message.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                parts.append(b.get("text", ""))
            elif t == "tool-call":
                parts.append(f"[tool-call: {b.get('name')}]")
            elif t == "tool-result":
                parts.append("[tool-result]")
    return "\n".join(parts)


# ---------- 1. 抽取所有用户消息 ----------
user_msgs = []
for r in recs:
    if r.get("type") == "user/message":
        d = r["data"]
        if d.get("role") == "user" or (d.get("source") or {}).get("kind") == "user":
            user_msgs.append({
                "seq": r.get("seq"),
                "time": r.get("time"),
                "text": text_of(d),
            })
print(f"user messages: {len(user_msgs)}")

up = os.path.join(OUT_DIR, "用户消息全文.md")
with open(up, "w", encoding="utf-8") as f:
    f.write("# 用户消息全文（原始，未改写）\n\n")
    f.write(f"> 来源：DSH session `session-6156c405`，共 {len(user_msgs)} 条用户消息\n")
    f.write("> 这是思考轨迹分析的**原始原料**，逐字保留（含错别字与口误）。\n\n---\n\n")
    for i, m in enumerate(user_msgs, 1):
        f.write(f"## 用户消息 #{i}  (seq={m['seq']})\n\n")
        f.write("```text\n" + (m["text"] or "(空)") + "\n```\n\n---\n\n")
print(f"saved -> {up}")

# ---------- 2. 重建完整对话 ----------
assistant_msgs = [r for r in recs if r.get("type") == "assistant/message"]
print(f"assistant messages: {len(assistant_msgs)}")

timeline = []
for r in recs:
    t = r.get("type")
    if t == "user/message":
        timeline.append((r.get("seq", 0), "USER", text_of(r["data"])))
    elif t == "assistant/message":
        timeline.append((r.get("seq", 0), "ASSISTANT", text_of(r["data"]["message"])))
timeline.sort(key=lambda x: x[0])

conv = os.path.join(OUT_DIR, "对话完整重建.md")
with open(conv, "w", encoding="utf-8") as f:
    f.write("# 对话完整重建\n\n")
    f.write(f"> 来源：DSH session `session-6156c405`\n")
    f.write(f"> 用户消息 {len(user_msgs)} 条，助手消息 {len(assistant_msgs)} 条\n")
    f.write("> 助手消息为重建（由流式 chunk 拼装），工具调用以 `[tool-call: 名称]` 标注，工具返回省略。\n\n---\n\n")
    n = 0
    for seq, who, body in timeline:
        n += 1
        f.write(f"## [{n}] {who}  (seq={seq})\n\n")
        f.write(body.strip() if body.strip() else "(空)")
        f.write("\n\n---\n\n")
print(f"saved -> {conv}")

# ---------- 3. 推理轨迹（我自己的思考链，用于对比深度） ----------
reason = [r for r in recs if r.get("type") == "reasoning-chunks"]
print(f"reasoning chunk groups: {len(reason)}")
rtxt = []
for r in reason:
    d = r["data"]
    s = "".join(d.get("texts", []) or [])
    if s.strip():
        rtxt.append(s)
rp = os.path.join(OUT_DIR, "助手推理链.md")
with open(rp, "w", encoding="utf-8") as f:
    f.write("# 助手推理链（reasoning chunks 重建）\n\n")
    f.write(f"> 共 {len(rtxt)} 段。仅用于对照「论证深度」与「结论产出点」。\n\n---\n\n")
    for i, s in enumerate(rtxt, 1):
        f.write(f"### 推理段 {i}\n\n{s.strip()}\n\n")
print(f"saved -> {rp}")

print("\n=== 统计 ===")
for p in (up, conv, rp):
    sz = os.path.getsize(p)
    print(f"  {os.path.basename(p):26} {sz/1024:9.1f} KB")
