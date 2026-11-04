"""组装书签、写入 PDF、校验。"""
from __future__ import annotations

import difflib
import json
import re

import fitz

from .ocr import ocr_page
from .toc import group_rows
from .pagemap import PageMap
from .toc import Entry


def _norm(s: str) -> str:
    return re.sub(r"[\s\u3000·•∙⋯…‥.．、，,。：:；;—\-—_（）()「」『』《》“”\"']", "", s)


def assemble(entries: list[Entry], pmap: PageMap, n_pages: int) -> list[dict]:
    """把解析出的目录条目对齐到实际 PDF 页码；个别页码缺失的用邻项补齐。"""
    rows: list[dict] = []
    for e in entries:
        if e.note == "前置页":
            continue      # 前言/摘要/目录用的是罗马页码，无法用正文偏移换算
        phys = pmap.to_phys(e.printed) if e.printed is not None else None
        note = ""
        if e.printed is None:
            note = "页码缺失"
        elif phys is None:
            note = "页码越界"
        elif not (1 <= phys <= n_pages):
            note = "页码越界"
            phys = None
        rows.append({
            "level": e.level, "title": e.title, "printed": e.printed,
            "page": phys, "conf": round(e.conf, 2), "note": note,
        })

    # 缺页码的，用后面的条目补（章与节通常同页起），没有后面的就用前面的
    for i, r in enumerate(rows):
        if r["page"] is not None:
            continue
        for j in range(i + 1, len(rows)):
            if rows[j]["page"] is not None:
                r["page"] = rows[j]["page"]
                break
        else:
            for j in range(i - 1, -1, -1):
                if rows[j]["page"] is not None:
                    r["page"] = rows[j]["page"]
                    break

    items = [r for r in rows if r["page"] is not None]
    last = 0
    for r in items:
        r["page"] = max(int(r["page"]), last)
        last = r["page"]
    return items


def to_toc(items: list[dict]) -> list[list]:
    toc = []
    prev = 0
    for it in items:
        lvl = max(1, min(int(it["level"]), prev + 1))
        prev = lvl
        toc.append([lvl, it["title"], int(it["page"])])
    return toc


def write_pdf(src: str, dst: str, toc: list[list]) -> None:
    doc = fitz.open(src)
    try:
        doc.set_toc(toc)
        doc.save(dst, garbage=1, deflate=True)
    finally:
        doc.close()


def load_items(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["items"] if isinstance(data, dict) else data


def refine_text_pages(doc: "fitz.Document", items: list[dict],
                      front_end: int | None = None) -> list[dict]:
    """文字版补救：章级条目在目标页找不到标题时，去"目录之前的前置页"里找。

    典型场景是「前言」「序」这类前置页——它们用另一套页码，只看正文偏移会指错。
    只在目录页之前找，避免被正文里偶然提到的标题带偏。
    """
    if not front_end:
        return items
    for it in items:
        if it["level"] != 1:
            continue
        idx = int(it["page"]) - 1
        if not (0 <= idx < doc.page_count):
            continue
        want = _norm(it["title"])
        if len(want) < 2:
            continue
        try:
            if want in _norm(doc[idx].get_text("text")):
                continue
        except Exception:
            continue
        for p in range(0, min(front_end, doc.page_count)):
            try:
                if want in _norm(doc[p].get_text("text")):
                    it["page"] = p + 1
                    it["note"] = "前置页"
                    break
            except Exception:
                continue
    last = 0
    for it in items:
        it["page"] = max(int(it["page"]), last)
        last = it["page"]
    return items


def spot_check(doc: fitz.Document, items: list[dict], use_text: bool,
               limit: int = 8) -> list[dict]:
    """抽查：把每章起始页的页眉 OCR 出来，和目录标题比对。"""
    chapters = [it for it in items if it["level"] == 1]
    if len(chapters) > limit:
        step = max(1, len(chapters) // limit)
        chapters = chapters[::step][:limit]
    out = []
    for it in chapters:
        p = int(it["page"]) - 1
        if not (0 <= p < doc.page_count):
            continue
        lines = ocr_page(doc, p, dpi=200, clip_norm=(0, 0, 1, 0.35))
        rows = group_rows(lines)          # 同一行的碎片按 x 排好再拼，避免"第二章/秦汉造船"倒序
        got = _norm("".join("".join(l.text for l in r) for r in rows))
        want = _norm(it["title"])
        ratio = difflib.SequenceMatcher(None, want, got).find_longest_match(
            0, len(want), 0, len(got)).size if want else 0
        coverage = ratio / max(1, len(want))
        out.append({
            "title": it["title"],
            "page": it["page"],
            "coverage": round(coverage, 2),
            "ok": coverage >= (0.5 if len(want) <= 6 else 0.6),
            "page_head": "".join(l.text for l in lines)[:60],
        })
    return out


def report(items: list[dict], checks: list[dict], pmap_desc: str,
           toc_pages: list[int], profile_line: str) -> str:
    n_levels = max((it["level"] for it in items), default=0)
    ok = sum(1 for c in checks if c["ok"])
    lines = [
        "# 编目报告",
        "",
        f"- 书籍：{profile_line}",
        f"- 目录页：第 {', '.join(str(p + 1) for p in toc_pages) if toc_pages else '未定位'}"
        f"（共 {len(toc_pages)} 页）",
        f"- {pmap_desc}",
        f"- 书签：{len(items)} 条，{n_levels} 级",
        "",
        "## 抽查（章首页标题 vs 目录标题）",
        "",
        "| 章 | 页码 | 匹配度 | 结果 |",
        "| --- | --- | --- | --- |",
    ]
    for c in checks:
        lines.append(f"| {c['title'][:28]} | {c['page']} | {c['coverage']:.0%} | "
                     f"{'✅' if c['ok'] else '⚠️'} |")
    if checks:
        lines.append("")
        lines.append(f"抽查 {len(checks)} 章，{ok} 章对齐正确。")
    return "\n".join(lines)
