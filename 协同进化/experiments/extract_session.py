"""正确解压多帧 zstd 的 session.jsonl，并导出可读对话。"""
import json
import os
from collections import Counter

import zstandard as zstd

SRC = r"C:\Users\Administrator\.dsh\sessions\--D-~5DE5~4F5C~533A~8868-~5DE5~4F5C~533A4--\session-6156c405-1a13-4806-934f-dd3f7c6953ce\session.jsonl.zstd"
OUT_DIR = r"D:\工作区表\工作区4\协同进化\思考轨迹分析"
os.makedirs(OUT_DIR, exist_ok=True)

# 多帧：用 stream_reader 读全部
dctx = zstd.ZstdDecompressor()
with open(SRC, "rb") as f:
    data = dctx.stream_reader(f).read()
print(f"decompressed: {len(data):,} bytes")

text = data.decode("utf-8", errors="replace")
lines = [l for l in text.split("\n") if l.strip()]
print(f"lines: {len(lines):,}")

out_jsonl = os.path.join(OUT_DIR, "session_原始.jsonl")
with open(out_jsonl, "w", encoding="utf-8") as f:
    f.write(text)
print(f"saved -> {out_jsonl}\n")

types = Counter()
roles = Counter()
keys = Counter()
recs = []
bad = 0
for l in lines:
    try:
        o = json.loads(l)
    except Exception:
        bad += 1
        continue
    recs.append(o)
    for k in o.keys():
        keys[k] += 1
    types[str(o.get("type"))] += 1
    m = o.get("message")
    if isinstance(m, dict):
        roles[str(m.get("role"))] += 1

print(f"unparsable: {bad}")
print("=== types ===")
for k, c in types.most_common(30):
    print(f"  {k:34} {c}")
print("\n=== message.role ===")
for k, c in roles.most_common(12):
    print(f"  {k:34} {c}")
print("\n=== keys ===")
for k, c in keys.most_common(30):
    print(f"  {k:34} {c}")

print("\n=== 每种 type 一条样例（截断 400 字符） ===")
seen = set()
for o in recs:
    t = str(o.get("type"))
    if t in seen:
        continue
    seen.add(t)
    s = json.dumps(o, ensure_ascii=False)
    print(f"\n--- {t} ---")
    print("  " + s[:400])
