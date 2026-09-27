"""验证：替换内容的 KV 该不该"带着旧内容算"。

用 HF DynamicCache 做一个最小实验：
  路径 P（你说的做法）: 带着旧 span 的 KV 生成新 span，新 span 的 KV 直接沿用
  路径 C（干净重算）  : 先删旧 span，再算新 span 的 KV

然后与"金标准"比较：把最终文本**完整重新 prefill 一遍**得到的 KV。
谁更接近金标准，谁的 KV 就是时间一致的。

Qwen3-0.6B 很小，CPU 上几秒即可跑完，不需要 GPU。
"""
import sys

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL = r"D:\工作区表\工作区4\协同进化\models\Qwen3-0.6B"


def build_kv(model, ids):
    """完整前向一次，返回 (logits_last, past_key_values)。"""
    with torch.no_grad():
        out = model(input_ids=ids, use_cache=True)
    return out.logits, out.past_key_values


def kv_tensors(past):
    """把 DynamicCache 里的 K/V 摊平成张量列表，便于比较。"""
    keys, vals = [], []
    for layer in past.layers:
        keys.append(layer.keys)
        vals.append(layer.values)
    return keys, vals


def max_abs_diff(a_list, b_list, n_common):
    """逐层比较前 n_common 个位置的最大绝对差。"""
    worst = 0.0
    for la, lb in zip(a_list, b_list):
        n = min(n_common, la.shape[-2], lb.shape[-2])
        if n <= 0:
            continue
        d = (la[..., :n, :] - lb[..., :n, :]).abs().max().item()
        worst = max(worst, d)
    return worst


def main():
    print("loading tokenizer/model ...")
    tok = AutoTokenizer.from_pretrained(MODEL)
    model = AutoModelForCausalLM.from_pretrained(MODEL, dtype=torch.float32)
    model.eval()
    print(f"model ok. layers={model.config.num_hidden_layers}")

    # ---- 构造一个"有错误"的对话，和一处修正 ----
    # 注意：这里必须先确认 token 长度相等 —— 文本等长不代表 token 等长（分词抖动）。
    prefix_text = "请在下面这行里把变量名写正确：\nresult = compute(a, b)\nprint(reslt)\n"

    # 候选配对：(错误片段, 修正片段)。选第一对 token 数相等的。
    candidates = [
        ("print(reslt)", "print(resul)"),
        ("reslt", "resul"),
        ("print(reslt)\n", "print(resul)\n"),
        ("print(reslt);", "print(resul);"),
        ("x = foo(aa)", "x = foo(bb)"),
        ("foo(aa)", "foo(bb)"),
        ("aa + bb", "bb + aa"),
        ("value = 100", "value = 200"),
    ]

    pair = None
    print("\n=== 挑选 token 等长的替换配对（文本等长 != token 等长）===")
    for w, r in candidates:
        wi = tok(w, add_special_tokens=False, return_tensors="pt").input_ids
        ri = tok(r, add_special_tokens=False, return_tensors="pt").input_ids
        mark = "  <== 采用" if wi.shape[1] == ri.shape[1] else ""
        print(f"  {w!r:22} {wi.shape[1]} tok | {r!r:22} {ri.shape[1]} tok{mark}")
        if wi.shape[1] == ri.shape[1] and pair is None:
            pair = (w, r, wi, ri)

    if pair is None:
        print("!! 找不到等长配对，请再补候选")
        return

    wrong_text, right_text, wrong_ids, right_ids = pair
    print(f"\n采用: {wrong_text!r} -> {right_text!r}  (各 {wrong_ids.shape[1]} token)")

    prefix_ids = tok(prefix_text, return_tensors="pt").input_ids
    n_pre = prefix_ids.shape[1]
    n_wrong = wrong_ids.shape[1]
    n_right = right_ids.shape[1]
    print(f"token 数: prefix={n_pre} wrong={n_wrong} right={n_right}  (等长 OK)")

    # ---- 金标准：最终文本（prefix + right）完整 prefill ----
    final_ids = torch.cat([prefix_ids, right_ids], dim=1)
    _, kv_gold = build_kv(model, final_ids)
    gold_keys, gold_vals = kv_tensors(kv_gold)

    # ---- 路径 P：带着旧 span 的 KV 生成新 span，KV 直接沿用 ----
    with_wrong_ids = torch.cat([prefix_ids, wrong_ids], dim=1)
    _, kv_with_wrong = build_kv(model, with_wrong_ids)
    # 在"能看到旧 span"的状态下继续算新 span
    with torch.no_grad():
        out_p = model(input_ids=right_ids, past_key_values=kv_with_wrong, use_cache=True)
    p_keys, p_vals = kv_tensors(out_p.past_key_values)

    # ---- 路径 C：先删旧 span 的 KV，再算新 span ----
    kv_clean = kv_with_wrong
    # 4.x 用 crop(n) 保留前 n 个；5.18+ 改用负数表示"从尾部删掉 n 个"
    try:
        kv_clean.crop(n_pre)
    except TypeError:
        kv_clean.crop(-n_wrong)
    with torch.no_grad():
        out_c = model(input_ids=right_ids, past_key_values=kv_clean, use_cache=True)
    c_keys, c_vals = kv_tensors(out_c.past_key_values)

    # ---- 比较（只看被修改的 span 那一段） ----
    print("\n=== 被修改 span 的 KV 与金标准的差异（最大绝对差）===")
    # 金标准里 span 从 n_pre 开始
    def seg(tensors):
        return [t[..., n_pre: n_pre + n_right, :] for t in tensors]

    d_p = max_abs_diff(seg(p_keys), seg(gold_keys), n_right)
    d_c = max_abs_diff(seg(c_keys), seg(gold_keys), n_right)
    print(f"  路径 P（带旧 KV 算，== 你的设想）: {d_p:.6f}")
    print(f"  路径 C（先删再算，== 本设计）    : {d_c:.6f}")

    print("\n=== 前缀部分是否两边都完好 ===")
    d_prefix = max_abs_diff(p_keys, gold_keys, n_pre)
    print(f"  前缀最大绝对差: {d_prefix:.6f}  (应接近 0，前缀未被触碰)")

    print("\n=== V 是否也受位置影响（理论：V 不含位置编码）===")
    d_v_p = max_abs_diff(seg(p_vals), seg(gold_vals), n_right)
    d_v_c = max_abs_diff(seg(c_vals), seg(gold_vals), n_right)
    print(f"  V 差异  路径P={d_v_p:.6f}   路径C={d_v_c:.6f}")
    print(f"  K 差异  路径P={d_p:.6f}   路径C={d_c:.6f}")
    print("  -> 若 P 的 K 差异明显大于 C，说明'带旧 KV 算'确实引入了时间不一致")

    print("\n=== 结论 ===")
    if d_c < d_p:
        print(f"  路径 C 更接近金标准（{d_c:.6f} < {d_p:.6f}）")
        print("  => 替换内容须在'删除旧 span 之后'的干净上下文里计算")
    else:
        print("  两条路径无显著差异 —— 需换更长的 span / 更敏感的例子重测")


if __name__ == "__main__":
    sys.exit(main())
