"""目录页定位与目录条目解析。

关键经验：Vision/OCR 在整页识别时容易漏掉目录页右栏的页码（引导点连成一长串时尤
其明显），但对右栏窄条单独识别时几乎全中。因此扫描件走两遍：
  1) 整页识别 → 标题（保证长标题不被截断）
  2) 右栏窄条识别 → 页码
再按纵坐标把两者配对。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import fitz

from concurrent.futures import ThreadPoolExecutor

from .ocr import Line, ocr_image, ocr_page, render_png

_LEADERS = "·•∙・‧⋯…‥.．-—_~‑–"
_LEADER_CLS = re.escape(_LEADERS)
# 书名末尾用斜杠接页码也很常见：「前言／１」「一、何谓比较文学？／１」
_SEPARATORS = "／/｜|：:"
_SEP_CLS = re.escape(_SEPARATORS)
_TAIL_SEP = "[" + _LEADER_CLS + _SEP_CLS + r"\s]"
_NUM_ONLY = re.compile(r"^[\s" + _LEADER_CLS + r"]*(\d{1,4})[\s" + _LEADER_CLS + r"]*$")
_ROMAN_ONLY = re.compile(r"^[\s" + _LEADER_CLS + r"]*([ivxlcdm]{1,7})[\s" + _LEADER_CLS + r"]*$", re.I)
_HEAD_RE = re.compile(r"^(目\s*[次录]|contents|tableofcontents)$", re.I)

_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"^第[〇零一二三四五六七八九十百千万两0-9]{1,6}[章编卷篇部辑]"), 1),
    (re.compile(r"^第[〇零一二三四五六七八九十百千万两0-9]{1,6}节"), 2),
    (re.compile(r"^[〇零一二三四五六七八九十百]{1,4}、"), 3),
    (re.compile(r"^[（(][〇零一二三四五六七八九十百]{1,4}[）)]"), 4),
    (re.compile(r"^[（(]\d{1,3}[）)]"), 4),
]
# OCR 认错字时的宽松规则
_LOOSE_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"^第.{1,4}[章编卷篇部辑]\s"), 1),
    (re.compile(r"^第.{1,4}节\s"), 2),
]
_MAJOR_WORDS = re.compile(
    r"^(绪论|緒論|导论|導論|引言|前言|序言|序|结语|結語|结论|結論|余论|餘論|"
    r"附录|附錄|参考文献|參考文獻|后记|後記|跋|致谢|致謝|索引|凡例|出版说明|出版說明|"
    r"导言|導言|引言|编后记|編後記)"
)

_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def roman_to_int(s: str) -> int:
    total, prev = 0, 0
    for ch in reversed(s.lower()):
        val = _ROMAN[ch]
        if val < prev:
            total -= val
        else:
            total += val
            prev = val
    return total


def clean_title(t: str) -> str:
    t = t.replace("\u3000", " ")
    t = re.sub(r"[ \t]+", " ", t).strip()
    t = t.strip(_LEADERS + " ")
    t = re.sub(r"[" + _LEADER_CLS + _SEP_CLS + r"。，、；：］】」』〔【]+\s*$", "", t).strip()
    t = re.sub(r"\s+", " ", t)
    return t


def pattern_level(title: str) -> int | None:
    for pat, lvl in _PATTERNS:
        if pat.match(title):
            return lvl
    for pat, lvl in _LOOSE_PATTERNS:
        if pat.match(title):
            return lvl
    if _MAJOR_WORDS.match(title):
        return 1
    return None


def _median(vals):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[len(s) // 2]


# ---------------------------------------------------------------- 取行

def text_lines(doc: fitz.Document, index: int) -> list[Line]:
    page = doc[index]
    w, h = page.rect.width, page.rect.height
    out: list[Line] = []
    try:
        data = page.get_text("dict")
    except Exception:
        return out
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = sorted(line.get("spans", []), key=lambda s: s.get("bbox", (0,))[0])
            text = "".join(s.get("text", "") for s in spans)
            if not text.strip():
                continue
            x0, y0, x1, y1 = line["bbox"]
            size = max((s.get("size", 0.0) for s in spans), default=0.0)
            out.append(Line(text, x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h, 1.0, size / h))
    out.sort(key=lambda l: (round(l.y, 3), l.x))
    return out


def page_lines(doc: fitz.Document, index: int, use_text: bool, dpi: float = 200,
               clip: tuple[float, float, float, float] | None = None) -> list[Line]:
    if use_text:
        lines = text_lines(doc, index)
        if lines:
            if clip is None:
                return lines
            _, y0, _, y1 = clip
            return [l for l in lines if y0 <= l.cy <= y1]
    return ocr_page(doc, index, dpi=dpi, clip_norm=clip)


def ocr_page_abs(doc: fitz.Document, index: int, dpi: float = 300,
                 clip_norm: tuple[float, float, float, float] | None = None) -> list[Line]:
    """OCR 页面局部，并把坐标换算回整页归一化坐标。"""
    if clip_norm is None:
        return ocr_page(doc, index, dpi=dpi)
    x0, y0, x1, y1 = clip_norm
    dx, dy = x1 - x0, y1 - y0
    out = []
    for l in ocr_page(doc, index, dpi=dpi, clip_norm=clip_norm):
        out.append(Line(l.text, x0 + l.x * dx, y0 + l.y * dy,
                        l.w * dx, l.h * dy, l.conf, l.size * dy))
    return out


def _ocr_crops_parallel(doc: fitz.Document, index: int,
                        jobs: list[tuple[tuple[float, float, float, float], float]]
                        ) -> list[list[Line]]:
    """先把每个裁切区域渲染成图片（PyMuPDF 不能多线程共用同一文档），再并行 OCR。"""
    paths: list[str] = []
    try:
        for clip, dpi in jobs:
            paths.append(render_png(doc, index, dpi=dpi, clip_norm=clip))
        with ThreadPoolExecutor(max_workers=len(paths)) as ex:
            raw = list(ex.map(ocr_image, paths))
    finally:
        for pth in paths:
            try:
                os.remove(pth)
                os.rmdir(os.path.dirname(pth))
            except OSError:
                pass
    out: list[list[Line]] = []
    for lines, (clip, _dpi) in zip(raw, jobs):
        x0, y0, x1, y1 = clip
        dx, dy = x1 - x0, y1 - y0
        out.append([
            Line(l.text, x0 + l.x * dx, y0 + l.y * dy, l.w * dx, l.h * dy, l.conf, l.size * dy)
            for l in lines
        ])
    return out


def toc_page_lines(doc: fitz.Document, index: int, use_text: bool,
                   dpi: float = 300) -> tuple[list[Line], list[Line] | None]:
    """返回 (标题行, 页码行)。文字版一次提取即可；扫描件走"整页 + 右栏多尺度"。

    两个坑：
    1. 右栏整条竖切时 Vision 几乎识别不出东西（长宽比过窄），必须切成上下横带；
    2. 不同分辨率捞到的数字不一样（200dpi 能认出 300dpi 漏掉的），所以要跨尺度合并。
    """
    if use_text:
        lines = text_lines(doc, index)
        if lines:
            inline = [
                l for l in lines
                if _NUM_ONLY.match(l.text.strip())
                or re.search(_TAIL_SEP + r"{1,}\d{1,4}[\s" + _LEADER_CLS + r"]*$", l.text)
            ]
            if len(inline) >= max(3, len(lines) // 3):
                return lines, None
            # 有些 PDF 是"扫描后补过文字层"：标题在文字层里，页码还是图，
            # 这时右栏要补一遍 OCR
            nums: list[Line] = []
            for b0, b1 in ((0.0, 0.56), (0.44, 1.0)):
                nums.extend(ocr_page_abs(doc, index, dpi=dpi, clip_norm=(0.55, b0, 1.0, b1)))
            if number_candidates(nums):
                return lines, nums
            return lines, None

    jobs: list[tuple[tuple[float, float, float, float], float]] = [((0.0, 0.0, 1.0, 1.0), dpi)]
    step = 0.25
    for k in range(4):
        b0 = max(0.0, k * step - 0.04)
        b1 = min(1.0, (k + 1) * step + 0.04)
        jobs.append(((0.0, b0, 1.0, b1), 200.0))

    results = _ocr_crops_parallel(doc, index, jobs)
    full = results[0]
    num_lines: list[Line] = []
    for part in results[1:]:
        num_lines.extend(part)
    return full, num_lines


# ---------------------------------------------------------------- 行分组

def group_rows(lines: list[Line], tol_factor: float = 0.65) -> list[list[Line]]:
    lines = sorted(lines, key=lambda l: (l.y, l.x))
    if not lines:
        return []
    med_h = _median([l.h for l in lines]) or 0.01
    rows: list[list[Line]] = []
    cur: list[Line] = [lines[0]]
    for l in lines[1:]:
        if abs(l.cy - _median([c.cy for c in cur])) <= med_h * tol_factor:
            cur.append(l)
        else:
            rows.append(cur)
            cur = [l]
    rows.append(cur)
    for r in rows:
        r.sort(key=lambda l: l.x)
    return rows


def row_number(row: list[Line]) -> tuple[int | None, Line | None]:
    for l in reversed(row):
        m = _NUM_ONLY.match(l.text)
        if m and l.x > 0.40:
            return int(m.group(1)), l
        m = _ROMAN_ONLY.match(l.text)
        if m and l.x > 0.40:
            return roman_to_int(m.group(1)), l
    if row:
        joined = " ".join(l.text for l in row)
        m = re.search(_TAIL_SEP + r"{1,}(\d{1,4})[\s" + _LEADER_CLS + r"]*$", joined)
        if m:
            return int(m.group(1)), None
        m = re.search(_TAIL_SEP + r"{1,}([ivxlcdm]{1,7})\s*$", joined, re.I)
        if m:
            return -roman_to_int(m.group(1)), None   # 负数标记：前置页罗马数字
    return None, None


def number_candidates(num_lines: list[Line] | None) -> list[tuple[float, int, float]]:
    """右栏识别结果 → [(cy, 页码, 置信度)]，不同尺度的重复项按位置去重。"""
    if not num_lines:
        return []
    raw = []
    for l in num_lines:
        m = _NUM_ONLY.match(l.text)
        if not m:
            continue
        n = int(m.group(1))
        if 0 < n <= 4000:
            raw.append((l.cy, n, l.conf))
    raw.sort()
    out: list[tuple[float, int, float]] = []
    for cy, n, c in raw:
        if out and abs(cy - out[-1][0]) < 0.006:
            if c > out[-1][2]:
                out[-1] = (cy, n, c)
            continue
        out.append((cy, n, c))
    return out


def toc_score(lines: list[Line], nums: list[tuple[float, int, float]] | None = None) -> float:
    body = [l for l in lines if 0.02 < l.cy < 0.94]
    rows = group_rows(body)
    rows = [r for r in rows if 0.02 < _median([l.cy for l in r]) < 0.94]
    if len(rows) < 3:
        return 0.0
    if nums is None:
        hits = sum(1 for r in rows if row_number(r)[0] is not None)
        return hits / len(rows)
    hits = 0
    for r in rows:
        cy = _median([l.cy for l in r])
        if any(abs(cy - ncy) < 0.012 for ncy, _, _ in nums):
            hits += 1
    return hits / len(rows)


def _heading_line(lines: list[Line]) -> Line | None:
    if not lines:
        return None
    med_h = _median([l.h for l in lines]) or 0.01
    for l in lines[:6]:
        t = re.sub(r"\s+", "", l.text).lower()
        if _HEAD_RE.match(t):
            return l
        if l.h > med_h * 1.7 and len(t) <= 8 and "目" in t:
            return l
    # 文字层的 PDF 里"目录"常被拆成几个独立文本行，拼起来判断
    joined = re.sub(r"\s+", "", "".join(l.text for l in lines[:4]))
    if re.match(r"^(目录|目次|contents)", joined, re.I) and len(joined) <= 12:
        return lines[0]
    return None


# ---------------------------------------------------------------- 找目录页

@dataclass
class TocRegion:
    pages: list[int]
    heading_page: int | None = None
    scores: dict[int, float] = field(default_factory=dict)
    cache: dict[int, tuple[list[Line], list[Line] | None]] = field(default_factory=dict)


def find_toc(doc: fitz.Document, use_text: bool, scan_limit: int = 120,
             progress=None) -> TocRegion:
    n = doc.page_count
    limit = min(n, max(scan_limit, int(n * 0.12)))
    scores: dict[int, float] = {}
    cache: dict[int, tuple[list[Line], list[Line] | None]] = {}
    heading_page = None

    # 第一遍：只看页面顶部，找"目录/目次"标题
    for i in range(limit):
        top = page_lines(doc, i, use_text, dpi=150, clip=(0, 0, 1, 0.45))
        if _heading_line(top) is not None:
            full, nums = toc_page_lines(doc, i, use_text)
            cache[i] = (full, nums)
            sc = toc_score(full, number_candidates(nums) or None)
            scores[i] = sc
            if sc >= 0.3:
                heading_page = i
                break
        if progress and i % 10 == 0:
            progress(i, limit)

    if heading_page is None:
        for i in range(min(limit, 45)):
            if i in cache:
                continue
            if use_text and not text_lines(doc, i):
                continue
            full, nums = toc_page_lines(doc, i, use_text)
            cache[i] = (full, nums)
            sc = toc_score(full, number_candidates(nums) or None)
            scores[i] = sc
            if sc >= 0.55:
                heading_page = i
                break
        if heading_page is None:
            return TocRegion(pages=[], scores=scores, cache=cache)

    start = heading_page
    pages = [start]
    i = start + 1
    while i < n and i < start + 45:
        full, nums = toc_page_lines(doc, i, use_text)
        cache[i] = (full, nums)
        sc = toc_score(full, number_candidates(nums) or None)
        scores[i] = sc
        if sc >= 0.45:
            pages.append(i)
            i += 1
        else:
            break
    return TocRegion(pages=pages, heading_page=start, scores=scores, cache=cache)


# ---------------------------------------------------------------- 解析条目

@dataclass
class Entry:
    title: str
    printed: int | None
    x0: float
    y: float
    page_in_toc: int
    level: int = 0
    pattern: int | None = None
    conf: float = 1.0
    page: int | None = None
    note: str = ""
    indent_level: int = 1

    def as_dict(self) -> dict:
        return {"level": self.level, "title": self.title, "printed": self.printed,
                "page": self.page, "conf": round(self.conf, 2), "note": self.note}


def _is_number_only(text: str) -> bool:
    return bool(_NUM_ONLY.match(text)) or bool(_ROMAN_ONLY.match(text))


def parse_toc_page(title_lines: list[Line], num_lines: list[Line] | None,
                   page_idx: int) -> list[Entry]:
    heading = _heading_line(title_lines)
    body = [l for l in title_lines if 0.02 < l.cy < 0.94]
    if heading is not None:
        tol = max(0.006, heading.h * 1.5)
        body = [l for l in body if abs(l.cy - heading.cy) > tol]

    nums = number_candidates(num_lines)
    rows = group_rows(body)

    entries: list[Entry] = []
    for row in rows:
        cy = _median([l.cy for l in row])
        if len(row) == 1 and _is_number_only(row[0].text):
            continue
        matched = None
        if nums:
            near = [(abs(cy - ncy), n, c) for ncy, n, c in nums if abs(cy - ncy) < 0.011]
            if near:
                near.sort()
                matched = near[0]
        num = None
        num_line = None
        roman = False
        if matched:
            num = matched[1]
        else:
            num, num_line = row_number(row)

        roman = False
        if num is not None and num < 0:
            roman = True
            num = -num

        parts = [l.text for l in row if l is not num_line]
        text = "".join(parts)
        if num is not None and num_line is None:
            text = re.sub("[" + _LEADER_CLS + _SEP_CLS + r"\s]*\d{1,4}[\s" + _LEADER_CLS + r"]*$", "", text)
        title = clean_title(text)
        if not title:
            continue
        if num is None and (len(title) <= 2 or _is_number_only(title)):
            continue
        entries.append(Entry(
            title=title,
            printed=num,
            x0=min(l.x for l in row),
            y=min(l.y for l in row),
            page_in_toc=page_idx,
            pattern=pattern_level(title),
            conf=min(l.conf for l in row),
            note="前置页" if roman else "",
        ))
    return entries


def merge_wrapped(entries: list[Entry]) -> list[Entry]:
    """标题换行时，下一行常常才是带页码的那行，两者要拼回去。"""
    out: list[Entry] = []
    i = 0
    while i < len(entries):
        e = entries[i]
        # 只合并"没有编号的短碎片"，否则会把上一级标题和它的子条目误并成一条
        if e.printed is None and e.pattern is None and i + 1 < len(entries):
            nxt = entries[i + 1]
            gap = nxt.y - e.y
            frag = clean_title(e.title)
            if (nxt.page_in_toc == e.page_in_toc and nxt.printed is not None
                    and len(frag) <= 12
                    and 0 < gap < 0.045 and nxt.x0 >= e.x0 - 0.025):
                joined = clean_title(e.title + nxt.title)
                out.append(Entry(
                    title=joined, printed=nxt.printed, x0=e.x0, y=e.y,
                    page_in_toc=e.page_in_toc, pattern=pattern_level(joined),
                    conf=min(e.conf, nxt.conf),
                ))
                i += 2
                continue
        out.append(e)
        i += 1
    return out


def drop_running_headers(pages: list[list[Entry]]) -> None:
    """去掉反复出现的书眉（如书名、'目次'）。"""
    from collections import Counter
    cnt: Counter[str] = Counter()
    for ents in pages:
        seen = set()
        for e in ents:
            if e.y < 0.14 and len(e.title) <= 12:
                seen.add(e.title)
        for s in seen:
            cnt[s] += 1
    repeat = {s for s, c in cnt.items() if c >= 2 and len(s) <= 12}
    for ents in pages:
        ents[:] = [e for e in ents if not (e.y < 0.14 and e.title in repeat)]


def assign_levels(entries: list[Entry]) -> list[Entry]:
    """按编号模式定层级；没有编号的按缩进跟随上一级。"""
    merged: list[Entry] = []
    for e in entries:
        if e.printed is None and merged and merged[-1].pattern is None and e.pattern is None:
            merged[-1].title = clean_title(merged[-1].title.rstrip(_LEADERS) + e.title)
            continue
        merged.append(e)

    present = sorted({e.pattern for e in merged if e.pattern})
    mapping = {p: i + 1 for i, p in enumerate(present)}

    # 缩进用栈来判断"回到上一层"，否则从节回到章的位置时会一直沿用上一层级
    stack: list[float] = []
    for e in merged:
        while len(stack) > 1 and e.x0 < stack[-1] - 0.012:
            stack.pop()
        if not stack or e.x0 > stack[-1] + 0.012:
            stack.append(e.x0)
        e.indent_level = len(stack)

    prev_level = 0
    for e in merged:
        if e.pattern is not None and e.pattern in mapping:
            lvl = mapping[e.pattern]
        else:
            lvl = getattr(e, "indent_level", 1)
        if prev_level and lvl > prev_level + 1:
            lvl = prev_level + 1
        e.level = max(1, lvl)
        e.title = re.sub(r"[\s" + _LEADER_CLS + r"]+$", "", e.title).strip()
        prev_level = e.level
    return merged


def parse_toc(doc: fitz.Document, region: TocRegion, use_text: bool,
              progress=None) -> list[Entry]:
    per_page: list[list[Entry]] = []
    for k, p in enumerate(region.pages):
        cached = region.cache.get(p)
        if cached is None:
            cached = toc_page_lines(doc, p, use_text)
            region.cache[p] = cached
        title_lines, num_lines = cached
        per_page.append(parse_toc_page(title_lines, num_lines, k))
        if progress:
            progress(k + 1, len(region.pages))
    drop_running_headers(per_page)
    flat = [e for ents in per_page for e in ents]
    return assign_levels(merge_wrapped(flat))
