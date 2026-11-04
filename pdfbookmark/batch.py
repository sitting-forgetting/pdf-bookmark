"""批量处理：把一个文件夹里的 PDF 全部编目，出一张汇总表。"""
from __future__ import annotations

import csv
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .pipeline import Analysis, BookError, SkipBook, process_book


@dataclass
class Row:
    file: str
    status: str = ""          # ok / skipped / failed
    pages: int = 0
    kind: str = ""
    toc_pages: str = ""
    offset: int = 0
    items: int = 0
    check: str = ""
    seconds: float = 0.0
    error: str = ""
    out_pdf: str = ""


def find_pdfs(folder: str, recursive: bool = True) -> list[str]:
    root = Path(folder)
    pattern = "**/*.pdf" if recursive else "*.pdf"
    files = [str(p) for p in sorted(root.glob(pattern)) if p.is_file()]
    # 跳过已经生成过的输出
    return [f for f in files if not f.endswith(".bookmarked.pdf")]


def _unique_name(stem: str, used: set[str]) -> str:
    name = stem
    k = 2
    while name.lower() in used:
        name = f"{stem}_{k}"
        k += 1
    used.add(name.lower())
    return name


def run_batch(folder: str, out_dir: str, workers: int = 6, force: bool = False,
              recursive: bool = True, limit: int | None = None,
              on_start=None, on_done=None) -> list[Row]:
    files = find_pdfs(folder, recursive=recursive)
    if limit:
        files = files[:limit]
    rows: list[Row] = []
    used: set[str] = set()

    for k, path in enumerate(files, 1):
        rel = os.path.relpath(path, folder)
        if on_start:
            on_start(k, len(files), rel)
        row = Row(file=rel)
        t0 = time.time()
        try:
            an = process_book(path, out_dir, workers=workers, force=force)
            s = an.summary()
            row.status = "ok"
            row.pages = s["pages"]
            row.kind = an.kind_cn
            row.toc_pages = f"{an.toc_pages[0]}–{an.toc_pages[-1]}" if an.toc_pages else ""
            row.offset = s["offset"]
            row.items = s["items"]
            row.check = f"{s['check_ok']}/{s['check_total']}"
            row.out_pdf = os.path.join(out_dir, f"{an.stem}.bookmarked.pdf")
        except SkipBook as e:
            row.status = "skipped"
            row.error = str(e)
        except BookError as e:
            row.status = "failed"
            row.error = str(e)
        except Exception as e:                      # 不让一本书拖垮整批
            row.status = "failed"
            row.error = f"{type(e).__name__}: {e}"
        row.seconds = round(time.time() - t0, 1)
        rows.append(row)
        if on_done:
            on_done(k, len(files), row)
    return rows


def write_report(rows: list[Row], out_dir: str, folder: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    ok = [r for r in rows if r.status == "ok"]
    skipped = [r for r in rows if r.status == "skipped"]
    failed = [r for r in rows if r.status == "failed"]

    md = [
        "# 批量编目报告",
        "",
        f"- 文件夹：{folder}",
        f"- 共 {len(rows)} 个 PDF：成功 {len(ok)}，跳过 {len(skipped)}，失败 {len(failed)}",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "| # | 文件 | 状态 | 页数 | 类型 | 目录页 | 偏移 | 书签 | 抽查 | 用时 | 备注 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    icon = {"ok": "✅", "skipped": "⏭", "failed": "❌"}
    for i, r in enumerate(rows, 1):
        md.append(
            f"| {i} | {r.file} | {icon.get(r.status, '')} | {r.pages or '—'} | {r.kind or '—'} | "
            f"{r.toc_pages or '—'} | {r.offset or '—'} | {r.items or '—'} | {r.check or '—'} | "
            f"{r.seconds}s | {r.error or ''} |"
        )

    md_path = os.path.join(out_dir, "批量报告.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    csv_path = os.path.join(out_dir, "批量报告.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["文件", "状态", "页数", "类型", "目录页", "偏移", "书签数", "抽查",
                    "用时(秒)", "输出PDF", "备注"])
        for r in rows:
            w.writerow([r.file, r.status, r.pages, r.kind, r.toc_pages, r.offset,
                        r.items, r.check, r.seconds, r.out_pdf, r.error])
    return md_path, csv_path
