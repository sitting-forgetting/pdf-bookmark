"""命令行入口。

用法：
    python -m pdfbookmark inspect 书.pdf
    python -m pdfbookmark run     书.pdf [-o 输出目录]
    python -m pdfbookmark batch   文件夹 [-o 输出目录]
    python -m pdfbookmark apply   书.pdf 书.toc.json
    python -m pdfbookmark gui
"""
from __future__ import annotations

import os
import sys
import time

import fitz

from . import build
from .batch import run_batch, write_report
from .ocr import backend_name
from .pipeline import BookError, SkipBook, analyze, process_book, write_outputs
from .profile import profile


def _progress(tag: str):
    state = {"last": -1}

    def cb(done: int, total: int) -> None:
        pct = 100 * done / max(1, total)
        if done != total and pct - state["last"] < 2:
            return
        state["last"] = pct
        sys.stderr.write(f"\r  {tag} {done}/{total} ({pct:.0f}%)   ")
        sys.stderr.flush()
        if done == total:
            sys.stderr.write("\n")
    return cb


def cmd_inspect(path: str) -> int:
    p = profile(path)
    print(f"文件：{path}")
    print(f"  {p.summary()}")
    print(f"  OCR 后端：{backend_name()}")
    doc = fitz.open(path)
    try:
        if p.outline_len:
            print("  已有书签（前几条）：")
            for lvl, title, page in p.outline_sample:
                print(f"    {'  ' * (lvl - 1)}{title}  → p{page}")
    finally:
        doc.close()
    return 0


def cmd_run(path: str, out_dir: str | None = None, workers: int = 6,
            force: bool = False) -> int:
    t0 = time.time()
    p = profile(path)
    print(f"[1/4] 体检：{p.summary()}")
    out_dir = out_dir or os.path.join(os.path.dirname(os.path.abspath(path)), "bookmark_out")

    try:
        an = analyze(path, workers=workers, force=force, progress=_progress("分析"))
    except SkipBook as e:
        print(f"  {e}，默认不覆盖。加 --force 强制重建。")
        return 0
    except BookError as e:
        print(f"  ❌ {e}")
        return 2

    print(f"  目录页：第 {an.toc_pages[0]}–{an.toc_pages[-1]} 页，共 {len(an.toc_pages)} 页")
    print(f"  {an.pmap_desc}")
    print(f"  对齐后 {len(an.items)} 条书签")

    print("[2/4] 写入 PDF…")
    paths = write_outputs(an, out_dir)

    print("[3/4] 回读校验…")
    doc2 = fitz.open(paths["pdf"])
    try:
        got = len(doc2.get_toc(simple=True))
    finally:
        doc2.close()
    print(f"  书签回读：{got} 条")

    ok = sum(1 for c in an.checks if c["ok"])
    print(f"[4/4] 抽查 {len(an.checks)} 章，{ok} 章标题对齐正确")
    print(f"  成品：{paths['pdf']}")
    print(f"  目录：{paths['json']}")
    print(f"  报告：{paths['report']}")
    print(f"完成，用时 {time.time() - t0:.1f}s")
    return 0


def cmd_batch(folder: str, out_dir: str | None = None, workers: int = 6,
              force: bool = False, recursive: bool = True, limit: int | None = None) -> int:
    if not os.path.isdir(folder):
        print(f"❌ 不是文件夹：{folder}")
        return 1
    out_dir = out_dir or os.path.join(os.path.abspath(folder), "编目输出")
    print(f"批量处理：{folder}")
    print(f"输出到：{out_dir}\n")

    def on_start(k, n, rel):
        print(f"[{k}/{n}] {rel} …")

    def on_done(k, n, row):
        tag = {"ok": "✅", "skipped": "⏭", "failed": "❌"}.get(row.status, "?")
        extra = f"{row.items} 条 · 抽查 {row.check} · {row.seconds}s" if row.status == "ok" else row.error
        print(f"      {tag} {extra}")

    rows = run_batch(folder, out_dir, workers=workers, force=force,
                     recursive=recursive, limit=limit,
                     on_start=on_start, on_done=on_done)
    md, csv_path = write_report(rows, out_dir, folder)
    ok = sum(1 for r in rows if r.status == "ok")
    print(f"\n完成：{len(rows)} 本，成功 {ok} 本")
    print(f"  汇总：{md}")
    print(f"  表格：{csv_path}")
    return 0


def cmd_apply(pdf_path: str, json_path: str, out: str | None = None) -> int:
    items = build.load_items(json_path)
    toc = build.to_toc(items)
    out = out or os.path.join(os.path.dirname(os.path.abspath(pdf_path)),
                              os.path.splitext(os.path.basename(pdf_path))[0] + ".bookmarked.pdf")
    build.write_pdf(pdf_path, out, toc)
    print(f"写入 {len(toc)} 条书签到 {out}")
    return 0


def cmd_gui(port: int = 8765, open_browser: bool = True) -> int:
    from .webapp import serve
    return serve(port=port, open_browser=open_browser)


USAGE = """用法：
  python -m pdfbookmark inspect <书.pdf>
  python -m pdfbookmark run     <书.pdf> [-o 输出目录] [-j 并行数] [--force]
  python -m pdfbookmark batch   <文件夹>  [-o 输出目录] [-j 并行数] [--force] [--no-recursive]
  python -m pdfbookmark apply   <书.pdf> <目录.json> [-o 输出.pdf]
  python -m pdfbookmark gui     [--port 8765] [--no-open]
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    cmd = argv[0]
    args = argv[1:]
    out = None
    workers = 6
    force = False
    recursive = True
    limit = None
    port = 8765
    open_browser = True
    pos: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-o", "--out"):
            i += 1
            out = args[i]
        elif a in ("-j", "--workers"):
            i += 1
            workers = int(args[i])
        elif a == "--force":
            force = True
        elif a == "--no-recursive":
            recursive = False
        elif a == "--limit":
            i += 1
            limit = int(args[i])
        elif a == "--port":
            i += 1
            port = int(args[i])
        elif a == "--no-open":
            open_browser = False
        else:
            pos.append(a)
        i += 1

    if cmd == "inspect" and pos:
        return cmd_inspect(pos[0])
    if cmd == "run" and pos:
        return cmd_run(pos[0], out, workers, force)
    if cmd == "batch" and pos:
        return cmd_batch(pos[0], out, workers, force, recursive, limit)
    if cmd == "apply" and len(pos) >= 2:
        return cmd_apply(pos[0], pos[1], out)
    if cmd == "gui":
        return cmd_gui(port, open_browser)
    print(USAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
