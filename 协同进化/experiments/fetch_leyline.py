"""下载 Leyline PDF 并抽取正文。不依赖 pypdf：优先用 pypdf，退化为 pdftotext，再退化为原始流解码。"""
import os
import re
import subprocess
import sys
import urllib.request

PDF_URL = "https://arxiv.org/pdf/2606.01065v1"
OUT = r"D:\工作区表\工作区4\协同进化\docs_收集"
os.makedirs(OUT, exist_ok=True)
pdf_path = os.path.join(OUT, "Leyline_2606.01065v1.pdf")
txt_path = os.path.join(OUT, "Leyline_全文.txt")

UA = {"User-Agent": "Mozilla/5.0 (compatible; research-reader/1.0)"}

print(f"downloading {PDF_URL} ...")
req = urllib.request.Request(PDF_URL, headers=UA)
with urllib.request.urlopen(req, timeout=120) as r:
    data = r.read()
print(f"  got {len(data):,} bytes")
with open(pdf_path, "wb") as f:
    f.write(data)
print(f"  saved -> {pdf_path}")
print(f"  magic: {data[:5]!r}  (expect b'%PDF-')")


def try_pypdf():
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return None
    reader = PdfReader(pdf_path)
    pages = []
    for i, p in enumerate(reader.pages):
        pages.append(f"\n\n===== PAGE {i+1} =====\n" + (p.extract_text() or ""))
    return "".join(pages)


def try_pdftotext():
    try:
        subprocess.run(["pdftotext", "-v"], capture_output=True, check=True)
    except Exception:
        return None
    subprocess.run(["pdftotext", "-layout", pdf_path, txt_path], check=True)
    return open(txt_path, encoding="utf-8", errors="replace").read()


text = None
for name, fn in (("pypdf", try_pypdf), ("pdftotext", try_pdftotext)):
    try:
        text = fn()
        if text and len(text) > 2000:
            print(f"  extracted via {name}: {len(text):,} chars")
            break
        text = None
    except Exception as e:
        print(f"  {name} failed: {type(e).__name__}: {e}")
        text = None

if not text:
    print("  !! no PDF text extractor available")
    print("  Falling back to raw stream scan (may be partial)")
    raw = data.decode("latin-1", errors="ignore")
    chunks = re.findall(r"\((?:[^()\\]|\\.)*\)", raw)
    text = " ".join(c[1:-1] for c in chunks)
    print(f"  raw scan: {len(text):,} chars")

with open(txt_path, "w", encoding="utf-8") as f:
    f.write(text)
print(f"  text -> {txt_path}")

# 打印结构：找章节标题
print("\n=== 章节结构 ===")
for line in text.split("\n"):
    s = line.strip()
    if re.match(r"^(\d+(\.\d+)*)\s+[A-Z\u4e00-\u9fff]", s) and len(s) < 90:
        print("  " + s)
