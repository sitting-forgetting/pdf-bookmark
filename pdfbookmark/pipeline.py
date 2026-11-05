"""把「分析目录 + 写书签」整理成可复用的流程，命令行、批量、网页三处共用。"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from . import build
from .pagemap import build_pagemap, scan_numbers
from .profile import profile
from .toc import find_toc, parse_toc

KIND_CN = {"scan": "扫描件（无文字层）", "text": "文字版", "mixed": "混合"}


class SkipBook(Exception):
    """已有书签，按规则跳过。"""

    def __init__(self, outline_len: int):
        super().__init__(f"已有 {outline_len} 条书签")
        self.outline_len = outline_len


class BookError(Exception):
    """无法处理，需要告诉用户原因。"""


@dataclass
class Analysis:
    path: str
    pages: int
    kind: str
    kind_cn: str
    outline_len: int
    toc_pages: list[int]
    offset: int
    pmap_desc: str
    items: list[dict]
    checks: list[dict]
    elapsed: float = 0.0
    toc_seconds: float = 0.0
    scan_seconds: float = 0.0

    @property
    def stem(self) -> str:
        return Path(self.path).stem

    def summary(self) -> dict:
        judged = [c for c in self.checks if not c.get("skipped")]
        ok = sum(1 for c in judged if c["ok"])
        return {
            "pages": self.pages,
            "kind": self.kind,
            "kind_cn": self.kind_cn,
            "toc_pages": self.toc_pages,
            "offset": self.offset,
            "items": len(self.items),
            "levels": max((it["level"] for it in self.items), default=0),
            "check_ok": ok,
            "check_total": len(judged),
            "elapsed": round(self.elapsed, 1),
        }


def analyze(path: str, workers: int = 6, force: bool = False,
            progress=None) -> Analysis:
    """分析一本书：定位目录 → 解析条目 → 算页码偏移 → 对齐。不写输出文件。"""
    t0 = time.time()
    p = profile(path)
    if p.encrypted:
        raise BookError("PDF 已加密，无法处理")
    if p.outline_len >= 3 and not force:
        raise SkipBook(p.outline_len)
    if p.pages == 0:
        raise BookError("PDF 没有可用页面")

    doc = fitz.open(path)
    try:
        use_text = p.kind == "text"

        t1 = time.time()
        region = find_toc(doc, use_text, progress=progress)
        if not region.pages:
            raise BookError("没有定位到目录页（前 100 多页里找不到「目录/目次」，"
                            "可能这本书没有目录页，或目录是图片且识别失败）")
        entries = parse_toc(doc, region, use_text, progress=progress)
        if not entries:
            raise BookError(f"目录页找到了（第 {region.pages[0] + 1} 页起），但没解析出条目")
        toc_seconds = time.time() - t1

        t2 = time.time()
        nums = scan_numbers(path, doc, use_text, workers=workers, progress=progress)
        body_start = region.pages[-1] + 1
        pmap = build_pagemap(nums, body_start, region.pages)
        if not pmap.ok:
            raise BookError("页码锚点太少，无法确定正文页码偏移"
                            "（可能是每页都没有印刷页码，或页脚识别失败）")
        items = build.assemble(entries, pmap, doc.page_count)
        if not items:
            raise BookError("目录条目没能对上任何 PDF 页码")
        if use_text:
            items = build.refine_text_pages(doc, items, front_end=region.pages[0])
        scan_seconds = time.time() - t2

        checks = build.spot_check(doc, items, use_text)
        return Analysis(
            path=path,
            pages=doc.page_count,
            kind=p.kind,
            kind_cn=KIND_CN[p.kind],
            outline_len=p.outline_len,
            toc_pages=[i + 1 for i in region.pages],
            offset=pmap.offset,
            pmap_desc=pmap.describe(),
            items=items,
            checks=checks,
            elapsed=time.time() - t0,
            toc_seconds=toc_seconds,
            scan_seconds=scan_seconds,
        )
    finally:
        doc.close()


def write_outputs(an: Analysis, out_dir: str, write_report: bool = True) -> dict:
    """写出带书签的 PDF、目录 JSON、报告。返回路径。"""
    os.makedirs(out_dir, exist_ok=True)
    stem = an.stem
    out_pdf = os.path.join(out_dir, f"{stem}.bookmarked.pdf")
    build.write_pdf(an.path, out_pdf, build.to_toc(an.items))

    data = {
        "source": os.path.abspath(an.path),
        "pages": an.pages,
        "toc_pages": an.toc_pages,
        "offset": an.offset,
        "items": an.items,
    }
    json_path = os.path.join(out_dir, f"{stem}.toc.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    report_path = ""
    if write_report:
        rep = build.report(an.items, an.checks, an.pmap_desc, [p - 1 for p in an.toc_pages],
                           f"{an.pages} 页 · {an.kind_cn} · 已有书签 {an.outline_len} 条")
        report_path = os.path.join(out_dir, f"{stem}.report.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(rep + "\n")

    return {"pdf": out_pdf, "json": json_path, "report": report_path}


def process_book(path: str, out_dir: str, workers: int = 6, force: bool = False,
                 progress=None) -> Analysis:
    """完整跑一本：分析 + 写出。"""
    an = analyze(path, workers=workers, force=force, progress=progress)
    write_outputs(an, out_dir)
    return an
