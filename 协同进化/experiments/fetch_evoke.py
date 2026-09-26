"""下载 EVOKE 论文 PDF 并抽取正文；同时探测其代码仓库。"""
import json
import os
import urllib.request

OUT = r"D:\工作区表\工作区4\协同进化\docs_收集"
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (compatible; research-reader/1.0)"}


def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# --- EVOKE PDF ---
url = "https://zenodo.org/api/records/20467232/files/paper-v2.pdf/content"
print(f"downloading EVOKE: {url}")
data = fetch(url)
pdf = os.path.join(OUT, "EVOKE_zenodo20467232_paper-v2.pdf")
with open(pdf, "wb") as f:
    f.write(data)
print(f"  got {len(data):,} bytes, magic={data[:5]!r}")
print(f"  saved -> {pdf}")

from pypdf import PdfReader
reader = PdfReader(pdf)
pages = []
for i, p in enumerate(reader.pages):
    pages.append(f"\n\n===== PAGE {i+1} =====\n" + (p.extract_text() or ""))
text = "".join(pages)
txt = os.path.join(OUT, "EVOKE_全文.txt")
with open(txt, "w", encoding="utf-8") as f:
    f.write(text)
print(f"  extracted {len(text):,} chars -> {txt}")

# --- 版本 2（另一条 record）元数据 ---
for rid in (20623044,):
    try:
        d = json.loads(fetch(f"https://zenodo.org/api/records/{rid}"))
        print(f"\n=== related record {rid} ===")
        print("  title:", d["metadata"]["title"])
        print("  date :", d["metadata"]["publication_date"])
        print("  files:", [f["key"] for f in d.get("files", [])])
    except Exception as e:
        print(f"\n  record {rid} failed: {type(e).__name__}: {e}")

# --- 代码仓库可达性 ---
print("\n=== EVOKE code repo ===")
for u in ("https://github.com/Anyesh/EVOKE", "https://api.github.com/repos/Anyesh/EVOKE"):
    try:
        req = urllib.request.Request(u, headers=UA)
        with urllib.request.urlopen(req, timeout=40) as r:
            print(f"  OK {r.status} {u}")
            if "api.github" in u:
                j = json.loads(r.read())
                print(f"     desc: {j.get('description')}")
                print(f"     lang: {j.get('language')}  stars: {j.get('stargazers_count')}  pushed: {j.get('pushed_at')}")
                print(f"     license: {(j.get('license') or {}).get('spdx_id')}")
                print(f"     clone: {j.get('clone_url')}")
    except Exception as e:
        print(f"  FAIL {u} -> {type(e).__name__}")
