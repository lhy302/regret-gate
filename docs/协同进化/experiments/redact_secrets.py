"""找出文档包里所有泄露的令牌出现位置，并就地脱敏。"""
import os
import re

ROOT = r"D:\工作区表\工作区4\协同进化"
TOKEN = "[REDACTED-TOKEN]"
# 通用模式：任何 ghp_/github_pat_ 令牌都视为敏感
PATTERNS = [TOKEN, r"ghp_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}"]

hits = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in ("models", "sources", "finetune", ".venv-finetune", "__pycache__")]
    for fn in filenames:
        p = os.path.join(dirpath, fn)
        if os.path.getsize(p) > 30 * 1024 * 1024:
            continue
        try:
            t = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for pat in PATTERNS:
            for m in re.finditer(pat, t):
                line = t[:m.start()].count("\n") + 1
                hits.append((os.path.relpath(p, ROOT), line, m.group()[:12] + "...", pat != TOKEN))

print("=== 命中位置 ===")
for rel, line, preview, generic in hits:
    print(f"  {rel}:{line}  {preview}  {'(通用模式)' if generic else '(就是你给的那个)'}")
print(f"总计 {len(hits)} 处\n")

# 就地脱敏
changed = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in ("models", "sources", "finetune", ".venv-finetune", "__pycache__")]
    for fn in filenames:
        p = os.path.join(dirpath, fn)
        if os.path.getsize(p) > 30 * 1024 * 1024:
            continue
        try:
            t = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        orig = t
        t = re.sub(TOKEN, "[REDACTED-TOKEN]", t)
        t = re.sub(r"ghp_[A-Za-z0-9]{20,}", "[REDACTED-TOKEN]", t)
        t = re.sub(r"github_pat_[A-Za-z0-9_]{20,}", "[REDACTED-TOKEN]", t)
        if t != orig:
            with open(p, "w", encoding="utf-8") as f:
                f.write(t)
            changed.append(os.path.relpath(p, ROOT))

print("=== 已脱敏文件 ===")
for c in changed:
    print(f"  {c}")
print(f"共 {len(changed)} 个文件\n")

# 复查
print("=== 复查（应无命中）===")
left = 0
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in ("models", "sources", "finetune", ".venv-finetune", "__pycache__")]
    for fn in filenames:
        p = os.path.join(dirpath, fn)
        if os.path.getsize(p) > 30 * 1024 * 1024:
            continue
        try:
            t = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for pat in PATTERNS:
            for m in re.finditer(pat, t):
                left += 1
                print(f"  残留: {os.path.relpath(p, ROOT)}:{t[:m.start()].count(chr(10))+1}")
print(f"残留 {left} 处" + ("  [OK 干净]" if left == 0 else "  [!! 仍有残留]"))
