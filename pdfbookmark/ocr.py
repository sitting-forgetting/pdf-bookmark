"""OCR 后端封装。

优先使用 tools/visionocr.swift 编译出的原生 macOS Vision 工具（中英文识别质量最好、
无需联网、无需下载模型）；若不存在则退回 tesseract。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import fitz

ROOT = Path(__file__).resolve().parent.parent
VISION_BIN = ROOT / "bin" / "visionocr"
DEFAULT_LANG = "zh-Hans,zh-Hant,en-US"


@dataclass
class Line:
    """一行 OCR 结果。坐标均为相对页面的归一化值（0~1），原点在左上角。"""

    text: str
    x: float
    y: float
    w: float
    h: float
    conf: float
    size: float = 0.0

    @property
    def x1(self) -> float:
        return self.x + self.w

    @property
    def y1(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


def have_vision() -> bool:
    return VISION_BIN.exists() and os.access(VISION_BIN, os.X_OK)


def backend_name() -> str:
    if have_vision():
        return "vision"
    if shutil.which("tesseract"):
        return "tesseract"
    return "none"


def ocr_image(path: str, level: str = "accurate", lang: str = DEFAULT_LANG) -> list[Line]:
    """OCR 一张图片，返回按阅读顺序排列的行。"""
    if have_vision():
        try:
            proc = subprocess.run(
                [str(VISION_BIN), path, "--level", level, "--lang", lang],
                capture_output=True, text=True, timeout=120,
            )
        except (subprocess.TimeoutExpired, OSError):
            return []
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []
        return [
            Line(d["text"], d["x"], d["y"], d["w"], d["h"], d["conf"])
            for d in data
            if d.get("text", "").strip()
        ]
    return _tesseract(path, lang, level)


def _tesseract(path: str, lang: str, level: str) -> list[Line]:
    """退化方案：tesseract，用 TSV 输出拿到行框。"""
    if not shutil.which("tesseract"):
        return []
    langs = "chi_sim+chi_tra+eng"
    try:
        proc = subprocess.run(
            ["tesseract", path, "stdout", "-l", langs, "--psm", "3", "tsv"],
            capture_output=True, text=True, timeout=180,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    import csv
    import io

    # 以图片像素尺寸归一化
    from PIL import Image

    try:
        with Image.open(path) as im:
            iw, ih = im.size
    except Exception:
        return []

    groups: dict[tuple[int, int, int, int], list[dict]] = {}
    reader = csv.DictReader(io.StringIO(proc.stdout), delimiter="\t")
    for row in reader:
        if row.get("level") != "5" or not row.get("text", "").strip():
            continue
        key = (int(row["page_num"]), int(row["block_num"]), int(row["par_num"]), int(row["line_num"]))
        groups.setdefault(key, []).append(row)

    out: list[Line] = []
    for _, rows in groups.items():
        rows.sort(key=lambda r: int(r["left"]))
        text = "".join(r["text"] for r in rows)
        left = min(int(r["left"]) for r in rows)
        top = min(int(r["top"]) for r in rows)
        right = max(int(r["left"]) + int(r["width"]) for r in rows)
        bottom = max(int(r["top"]) + int(r["height"]) for r in rows)
        conf = sum(float(r["conf"]) for r in rows) / (100.0 * len(rows))
        out.append(Line(
            text=text,
            x=left / iw, y=top / ih,
            w=(right - left) / iw, h=(bottom - top) / ih,
            conf=conf,
        ))
    out.sort(key=lambda l: (round(l.y, 3), l.x))
    return out


def render_png(doc: fitz.Document, index: int, dpi: float = 300,
               clip_norm: tuple[float, float, float, float] | None = None,
               out_dir: str | None = None) -> str:
    """把某页（或页面的一个归一化区域）渲染成 PNG，返回路径。"""
    page = doc[index]
    w, h = page.rect.width, page.rect.height
    clip = None
    if clip_norm is not None:
        x0, y0, x1, y1 = clip_norm
        clip = fitz.Rect(w * x0, h * y0, w * x1, h * y1)
    pix = page.get_pixmap(dpi=int(dpi), clip=clip)
    out_dir = out_dir or tempfile.mkdtemp(prefix="pdfbm_")
    path = os.path.join(out_dir, f"p{index:05d}.png")
    pix.save(path)
    return path


def ocr_page(doc: fitz.Document, index: int, dpi: float = 300,
             clip_norm: tuple[float, float, float, float] | None = None,
             level: str = "accurate", keep_dir: str | None = None) -> list[Line]:
    path = render_png(doc, index, dpi=dpi, clip_norm=clip_norm, out_dir=keep_dir)
    try:
        return ocr_image(path, level=level)
    finally:
        if keep_dir is None:
            try:
                os.remove(path)
                os.rmdir(os.path.dirname(path))
            except OSError:
                pass


# ---------------------------------------------------------------- 并行扫描

def _scan_chunk(job: tuple[str, list[int], float, float, float, str]) -> list[tuple[int, list[tuple[str, float, float]]]]:
    pdf, indices, y0, y1, dpi, level = job
    doc = fitz.open(pdf)
    out = []
    tmpdir = tempfile.mkdtemp(prefix="pdfbm_")
    try:
        for i in indices:
            page = doc[i]
            w, h = page.rect.width, page.rect.height
            pix = page.get_pixmap(dpi=int(dpi), clip=fitz.Rect(0, h * y0, w, h * y1))
            path = os.path.join(tmpdir, f"{i}.png")
            pix.save(path)
            lines = ocr_image(path, level=level)
            out.append((i, [(l.text, l.x, l.conf) for l in lines]))
            os.remove(path)
    finally:
        doc.close()
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass
    return out


def scan_region(
    pdf_path: str,
    indices: Sequence[int],
    y0: float,
    y1: float,
    dpi: float = 300,
    level: str = "accurate",
    workers: int = 6,
    progress=None,
) -> dict[int, list[tuple[str, float, float]]]:
    """并行 OCR 每页的某个横向条带（页眉/页脚），返回 {页序号(0-based): [(文本, x, 置信度)]}。"""
    indices = list(indices)
    if not indices:
        return {}
    workers = max(1, min(workers, len(indices)))
    chunk = max(1, len(indices) // workers)
    jobs = []
    for k in range(0, len(indices), chunk):
        jobs.append((pdf_path, indices[k:k + chunk], y0, y1, dpi, level))

    result: dict[int, list[tuple[str, float, float]]] = {}
    done = 0
    if workers == 1:
        for job in jobs:
            for i, lines in _scan_chunk(job):
                result[i] = lines
                done += 1
                if progress:
                    progress(done, len(indices))
        return result

    ctx = multiprocessing.get_context("fork")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        for part in ex.map(_scan_chunk, jobs):
            for i, lines in part:
                result[i] = lines
                done += 1
                if progress:
                    progress(done, len(indices))
    return result
