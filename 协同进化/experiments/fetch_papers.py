"""抓取 Leyline / EVOKE 两篇的元数据与摘要（只用标准库 + urllib）。"""
import json
import re
import urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "Mozilla/5.0 (research; contact: local)"}


def get(url, timeout=45):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def arxiv_meta(arxiv_id):
    url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"
    raw = get(url)
    root = ET.fromstring(raw)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    e = root.find("a:entry", ns)
    if e is None:
        return None
    def txt(tag):
        n = e.find(f"a:{tag}", ns)
        return " ".join(n.text.split()) if n is not None and n.text else ""
    return {
        "id": arxiv_id,
        "title": txt("title"),
        "published": txt("published"),
        "updated": txt("updated"),
        "authors": [a.find("a:name", ns).text for a in e.findall("a:author", ns)],
        "summary": txt("summary"),
        "cats": [c.get("term") for c in e.findall("a:category", ns)],
    }


def zenodo_meta(rec_id):
    url = f"https://zenodo.org/api/records/{rec_id}"
    raw = get(url)
    d = json.loads(raw)
    md = d.get("metadata", {})
    desc = md.get("description", "") or ""
    desc = re.sub(r"<[^>]+>", " ", desc)
    desc = " ".join(desc.split())
    return {
        "id": rec_id,
        "title": md.get("title"),
        "date": md.get("publication_date"),
        "type": (md.get("resource_type") or {}).get("title"),
        "creators": [c.get("name") for c in md.get("creators", [])],
        "description": desc,
        "files": [f.get("key") for f in d.get("files", [])],
        "doi": d.get("doi"),
        "license": (md.get("license") or {}).get("id") if isinstance(md.get("license"), dict) else md.get("license"),
    }


def show(label, obj):
    print("=" * 70)
    print(label)
    print("=" * 70)
    if not obj:
        print("  (no data)")
        print()
        return
    for k in ("title", "published", "updated", "date", "type", "doi", "license", "cats"):
        if obj.get(k):
            print(f"  {k:10}: {obj[k]}")
    if obj.get("authors"):
        print(f"  authors   : {', '.join(obj['authors'])}")
    if obj.get("creators"):
        print(f"  creators  : {', '.join(obj['creators'])}")
    if obj.get("files"):
        print(f"  files     : {', '.join(obj['files'])}")
    body = obj.get("summary") or obj.get("description") or ""
    if body:
        print("\n  --- 摘要/描述 ---")
        for i in range(0, len(body), 100):
            print("  " + body[i:i + 100])
    print()


if __name__ == "__main__":
    try:
        show("Leyline: KV Cache Directives for Agentic Inference (arXiv 2606.01065)",
             arxiv_meta("2606.01065"))
    except Exception as e:
        print(f"Leyline FAILED: {type(e).__name__}: {e}\n")

    for rid in (20467232, 20623044):
        try:
            show(f"EVOKE (Zenodo {rid})", zenodo_meta(rid))
        except Exception as e:
            print(f"Zenodo {rid} FAILED: {type(e).__name__}: {e}\n")
