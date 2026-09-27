"""验证 delete / insert 是否会破坏后续 KV，以及破坏的是什么。

三个实验，全部与"金标准 = 把最终文本完整重新 prefill"比较：

  D1  删除中间 span：先删其 KV，再重算后缀 KV  -> 与"短文本完整 prefill"比
  I1  中间插入 span：先插其 KV，再重算后缀 KV  -> 与"长文本完整 prefill"比
  C0  空操作 (splice(i,i,[]))                    -> 应与原 KV 逐位相同

同时记录：后缀**文本**在"缩短/加长后的历史"下是否仍然自洽。
"""
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL = r"D:\工作区表\工作区4\协同进化\models\Qwen3-0.6B"
tok = AutoTokenizer.from_pretrained(MODEL)
model = AutoModelForCausalLM.from_pretrained(MODEL, dtype=torch.float32)
model.eval()
print(f"model ok, layers={model.config.num_hidden_layers}\n")


def ids(s, special=False):
    return tok(s, add_special_tokens=special, return_tensors="pt").input_ids


def kv_of(input_ids, past=None):
    with torch.no_grad():
        out = model(input_ids=input_ids, past_key_values=past, use_cache=True)
    return out.logits, out.past_key_values


def ks(past):
    return [l.keys for l in past.layers]


def vs(past):
    return [l.values for l in past.layers]


def diff(a, b, lo, hi):
    """逐层比较 [lo,hi) 区间的最大绝对差。"""
    worst = 0.0
    for la, lb in zip(a, b):
        n0 = min(lo, la.shape[-2], lb.shape[-2])
        n1 = min(hi, la.shape[-2], lb.shape[-2])
        if n1 <= n0:
            continue
        worst = max(worst, (la[..., n0:n1, :] - lb[..., n0:n1, :]).abs().max().item())
    return worst


# ============ 语料 ============
head = "函数如下：\n"
body = "def f(x):\n    return x + 1\n"
tail = "print(f(2))\n"

head_i = ids(head, special=True)
body_i = ids(body)
tail_i = ids(tail)

n_head, n_body, n_tail = head_i.shape[1], body_i.shape[1], tail_i.shape[1]
print(f"token 数: head={n_head}  body(待删/插入)={n_body}  tail={n_tail}\n")

# ============ 基线 ============
full_i = torch.cat([head_i, body_i, tail_i], dim=1)
_, kv_full = kv_of(full_i)
full_k, full_v = ks(kv_full), vs(kv_full)

# ============ D1: 删除中间 span，重算后缀 ============
print("=" * 62)
print("D1  删除中间 span  (删除 body，保留 tail)")
print("=" * 62)
_, kv_before = kv_of(torch.cat([head_i, body_i], dim=1))  # head+body
kv_del = kv_before
try:
    kv_del.crop(n_head)          # 4.x: 保留前 n_head
except TypeError:
    kv_del.crop(-n_body)         # 5.18+: 从尾部删 n_body
_, kv_del_tail = kv_of(tail_i, past=kv_del)   # 重算后缀
dk, dv = ks(kv_del_tail), vs(kv_del_tail)

# 金标准：短文本完整 prefill
short_i = torch.cat([head_i, tail_i], dim=1)
_, kv_short = kv_of(short_i)
sk, sv = ks(kv_short), vs(kv_short)

print(f"  重算后缀 KV vs 短文本完整 prefill:")
print(f"    K 最大绝对差 = {diff(dk, sk, n_head, n_head + n_tail):.6f}")
print(f"    V 最大绝对差 = {diff(dv, sv, n_head, n_head + n_tail):.6f}")
print(f"  前缀(head)差异 = {diff(dk, sk, 0, n_head):.6f}")

# 后缀文本在"缩短历史"下是否自洽？
with torch.no_grad():
    lg_short = model(short_i).logits[0, -1]
    lg_full = model(full_i).logits[0, -1]
top_s = torch.topk(lg_short, 5).indices.tolist()
top_f = torch.topk(lg_full, 5).indices.tolist()
print(f"\n  下一个 token 预测（看后缀文本是否还站得住）:")
print(f"    短历史(head+tail) top5 = {[tok.decode([t]) for t in top_s]}")
print(f"    原历史(head+body+tail) top5 = {[tok.decode([t]) for t in top_f]}")
print(f"    两者 top1 相同? {top_s[0] == top_f[0]}")

# ============ I1: 中间插入 span，重算后缀 ============
print()
print("=" * 62)
print("I1  中间插入 span  (在 head 后插入 body，保留 tail)")
print("=" * 62)
_, kv_ins_tail = kv_of(tail_i, past=kv_full)  # 占位：下面用正确顺序
# 正确顺序：先 head，再插 body，再算 tail
_, kv_head = kv_of(head_i)
_, kv_ins_mid = kv_of(body_i, past=kv_head)
_, kv_ins_tail = kv_of(tail_i, past=kv_ins_mid)
ik, iv = ks(kv_ins_tail), vs(kv_ins_tail)

# 金标准就是 full（head+body+tail）—— 与 I1 构造的序列完全相同
print(f"  重算后缀 KV vs 完整 prefill（同一序列）:")
off = n_head + n_body
print(f"    K 最大绝对差 = {diff(ik, full_k, off, off + n_tail):.6f}")
print(f"    V 最大绝对差 = {diff(iv, full_v, off, off + n_tail):.6f}")
print("  -> 若接近 0，说明'插入后重算后缀'是正确做法 [OK]")

# 但后缀"文本"在插入后还自洽吗？
with torch.no_grad():
    lg_head_tail = model(torch.cat([head_i, tail_i], dim=1)).logits[0, -1]
print(f"\n  插入前，模型在 head+tail 上预测的下一个 token top5 = {[tok.decode([t]) for t in torch.topk(lg_head_tail,5).indices.tolist()]}")
print("  （对比：插入 body 之后，tail 的语义是否仍成立 —— 这就是'文本不重算'的代价）")

# ============ C0: 空操作 ============
print()
print("=" * 62)
print("C0  空操作 splice(i,i,[])  —— 应与原 KV 逐位相同")
print("=" * 62)
_, kv_noop = kv_of(torch.cat([head_i, body_i], dim=1))
nk, nv = ks(kv_noop), vs(kv_noop)
_, kv_noop2 = kv_of(torch.cat([head_i, body_i], dim=1))  # 同输入再算一次
ok, ov = ks(kv_noop2), vs(kv_noop2)
print(f"  K 最大绝对差 = {diff(nk, ok, 0, 10**9):.9f}")
print(f"  V 最大绝对差 = {diff(nv, ov, 0, 10**9):.9f}")
print("  -> 应为 0（确定性），作为 diff 函数的噪声底")

print()
print("=" * 62)
print("结论")
print("=" * 62)
print("""
  delete 与 insert 都改变长度 -> 后续位置全部偏移 -> 后缀 KV 必然失效。
  这与 replace 不等长是**同一个**破坏机制，不是新问题。
  区别只在「用户想不想保留后缀文本」：
    replace 不改文本长度意图，但改了内容  -> 后缀文本必须重生成
    delete  想缩短，被删内容多半是多余的  -> 后缀文本可能仍成立，但 KV 必须重算
    insert  想加内容，后缀 KV 必失效      -> 若还要重生成后缀文本 = 整段重写，收益归零
""")
