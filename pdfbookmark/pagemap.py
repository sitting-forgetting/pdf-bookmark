"""印刷页码 → PDF 物理页 的映射。

思路：把每页页脚（或页眉）上的印刷页码读出来，得到一串 (物理页, 印刷页码)。
书的页码常有多次重排（封面/前言/目录/正文各编各的），所以先按"页码回到 1 或
突然跳变"切段，再在正文那一段里做偏移量拟合。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field

import fitz

from .ocr import scan_region

_CJK = re.compile(r"[\u4e00-\u9fff]")
_CIRCLED = {chr(0x2460 + i): str(i + 1) for i in range(20)}   # ①..⑳
_NUM = re.compile(r"^[^\d]{0,3}(\d{1,4})[^\d]{0,3}$")
# 页眉里页码常和书名/篇名挤在同一行：「12 王国维卷」「王国维卷 12」
_LEAD_NUM = re.compile(r"^[^\d]{0,3}(\d{1,4})[\s｜|·•:：]+")
_TAIL_NUM = re.compile(r"[^\d][\s｜|·•:：]+(\d{1,4})[\s｜|·•:：]*$")


@dataclass
class PageNum:
    index: int            # 0-based 物理页
    num: int | None
    raw: str = ""
    conf: float = 0.0
    x: float = -1.0

    @property
    def phys(self) -> int:
        return self.index + 1


@dataclass
class Segment:
    anchors: list[tuple[int, int]] = field(default_factory=list)   # (物理页1-based, 印刷页码)

    @property
    def start(self) -> int:
        return self.anchors[0][0]

    @property
    def end(self) -> int:
        return self.anchors[-1][0]

    @property
    def offset(self) -> int:
        """物理页 - 印刷页码 的众数偏移。"""
        offs = [p - n for p, n in self.anchors]
        from collections import Counter
        return Counter(offs).most_common(1)[0][0]

    @property
    def offset_spread(self) -> int:
        offs = [p - n for p, n in self.anchors]
        return max(offs) - min(offs) if offs else 0

    def __len__(self) -> int:
        return len(self.anchors)



def text_page_number(page: "fitz.Page") -> tuple[int | None, str, float, float]:
    """从文字层读页码。

    坑：PDF 常把「159」拆成三个独立的单字符文本框，逐行匹配只会读到其中一位；
    必须先把同一行的数字碎片按 x 拼回去。另外页眉里的「５　参考书目」是章号，
    不是页码，所以只认纯数字片段。
    """
    w, h = page.rect.width, page.rect.height
    frags: list[tuple[float, float, float, str]] = []
    try:
        data = page.get_text("dict")
    except Exception:
        return None, "", 0.0, -1.0
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            txt = "".join(s.get("text", "") for s in line.get("spans", []))
            if not txt.strip():
                continue
            x0, y0, x1, y1 = line["bbox"]
            cy = (y0 + y1) / 2 / h
            if 0.22 < cy < 0.86:          # 只要页眉页脚区
                continue
            frags.append((x0 / w, x1 / w, cy, txt.strip()))
    if not frags:
        return None, "", 0.0, -1.0

    frags.sort(key=lambda f: (round(f[2], 3), f[0]))
    rows: list[list[tuple[float, float, float, str]]] = []
    cur = [frags[0]]
    for f in frags[1:]:
        if abs(f[2] - cur[-1][2]) < 0.008:
            cur.append(f)
        else:
            rows.append(cur)
            cur = [f]
    rows.append(cur)

    for row in rows:
        row.sort(key=lambda f: f[0])
        i = len(row) - 1
        while i >= 0:
            if row[i][3].isdigit():
                j = i
                while (j - 1 >= 0 and row[j - 1][3].isdigit()
                       and row[j][0] - row[j - 1][1] < 0.06):
                    j -= 1
                digits = "".join(f[3] for f in row[j:i + 1])
                try:
                    num = int(digits)
                except ValueError:
                    break
                if 0 < num <= 4000:
                    return num, digits, 1.0, row[j][0]
            i -= 1
    return None, "", 0.0, -1.0


def _pick_number(lines: list[tuple[str, float, float, float]]) -> PageNum_fields:
    """从页眉/页脚条带的 OCR 行里挑出页码。

    三种形态，规则各不相同，混淆了就会把正文/脚注里的数字当成页码：
      ① 整行就是页码：「•38•」「•⑨」
      ② 页眉左边是数字：「12 王国维卷」
      ③ 页眉右边是数字：「导言 13」
    """
    best = None
    for item in lines:
        text, x, w, conf = item
        t = text.strip()
        if not t:
            continue
        right = x + w
        num = None

        # ① 整行纯页码（允许圈码、点线、竖线这些装饰），行内不能有汉字
        pure = t.translate(str.maketrans(_CIRCLED))
        if not _CJK.search(pure):
            m = _NUM.match(pure)
            if m:
                num = int(m.group(1))

        if num is None:
            m = _LEAD_NUM.match(t)          # ② 「12 王国维卷」
            if m and x < 0.30:
                num = int(m.group(1))
            else:
                m = _TAIL_NUM.search(t)     # ③ 「导言 13」
                if m and right > 0.62:
                    num = int(m.group(1))

        if num is None or not (0 < num <= 4000):
            continue

        edge = 1.0 if (x < 0.28 or right > 0.70) else 0.0
        score = conf + edge * 0.5
        if best is None or score > best[0]:
            best = (score, num, t, conf, x)
    if best is None:
        return PageNum_fields(None, "", 0.0, -1.0)
    return PageNum_fields(best[1], best[2], best[3], best[4])


@dataclass
class PageNum_fields:
    num: int | None
    raw: str
    conf: float
    x: float


_CACHE_DIR = os.path.join(tempfile.gettempdir(), "pdfbookmark_cache")
# 改了页码识别规则就加一，免得命中旧规则算出来的缓存
_CACHE_VERSION = 3


def _cache_path(path: str) -> str:
    st = os.stat(path)
    key = hashlib.sha1(
        f"{os.path.abspath(path)}|{st.st_mtime_ns}|{st.st_size}|v{_CACHE_VERSION}".encode()
    ).hexdigest()[:16]
    return os.path.join(_CACHE_DIR, key + ".json")


def _load_cache(path: str) -> list[PageNum] | None:
    try:
        with open(_cache_path(path), encoding="utf-8") as f:
            data = json.load(f)
        return [PageNum(d["index"], d["num"], d["raw"], d["conf"], d["x"]) for d in data]
    except (OSError, KeyError, ValueError, TypeError):
        return None


def _save_cache(path: str, nums: list[PageNum]) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        st = os.stat(path)
        key = hashlib.sha1(
            f"{os.path.abspath(path)}|{st.st_mtime_ns}|{st.st_size}|v{_CACHE_VERSION}".encode()
        ).hexdigest()[:16]
        with open(os.path.join(_CACHE_DIR, key + ".json"), "w", encoding="utf-8") as f:
            json.dump([{"index": p.index, "num": p.num, "raw": p.raw, "conf": p.conf, "x": p.x} for p in nums], f)
    except OSError:
        pass


def scan_numbers(path: str, doc: fitz.Document, use_text: bool,
                 workers: int = 6, progress=None) -> list[PageNum]:
    n = doc.page_count
    cached = _load_cache(path)
    if cached and len(cached) == n:
        if progress:
            progress(n, n)
        return cached
    nums = _scan_numbers_uncached(path, doc, use_text, workers, progress)
    if any(p.num is not None for p in nums):
        _save_cache(path, nums)
    return nums


def _scan_numbers_uncached(path: str, doc: fitz.Document, use_text: bool,
                           workers: int = 6, progress=None) -> list[PageNum]:
    n = doc.page_count
    nums: list[PageNum] = [PageNum(i, None) for i in range(n)]

    if use_text:
        for i in range(n):
            num, raw, conf, x = text_page_number(doc[i])
            nums[i] = PageNum(i, num, raw, conf, x)
        if sum(1 for p in nums if p.num is not None) >= n * 0.3:
            return nums

    # OCR 路线：先用少量页面探一下页码到底在页脚还是页眉，别把两边都全扫一遍
    indices = list(range(n))
    probe = sorted(set(indices[:6] + indices[n // 2: n // 2 + 6] + indices[-6:]))
    bands = [(0.86, 1.0), (0.0, 0.22)]
    for y0, y1 in bands:
        raw = scan_region(path, probe, y0, y1, dpi=300, level="accurate", workers=workers)
        hits = sum(1 for i in probe if _pick_number(raw.get(i, [])).num is not None)
        if hits >= max(2, len(probe) // 3):
            bands = [(y0, y1)]
            break

    for y0, y1 in bands:
        raw = scan_region(path, indices, y0, y1, dpi=300, level="accurate",
                          workers=workers, progress=progress)
        cand = []
        hits = 0
        for i in range(n):
            f = _pick_number(raw.get(i, []))
            cand.append(PageNum(i, f.num, f.raw, f.conf, f.x))
            if f.num is not None:
                hits += 1
        if hits >= n * 0.25:
            return cand
    return nums


def build_pagemap(nums: list[PageNum], body_start: int | None,
                  toc_pages: list[int] | None = None) -> "PageMap":
    """用"偏移量众数"找正文段。

    正文里每页都满足 物理页 - 印刷页码 = 常数，OCR 读错的个别页码会得到离群的
    偏移量，靠众数天然被剔除；前言、目录各自另起一套页码，偏移量不同，也会被排
    除在正文段之外。
    """
    from collections import Counter

    cand = [pn for pn in nums if pn.num is not None]
    if not cand:
        return PageMap(None, [])
    if body_start is not None:
        after = [pn for pn in cand if pn.index + 1 >= body_start - 1]
        if len(after) >= 10:
            cand = after

    offs = Counter(pn.index + 1 - pn.num for pn in cand)
    mode_off, mode_cnt = offs.most_common(1)[0]
    if mode_cnt < 5 and body_start is None:
        return PageMap(None, [])

    anchors = [(pn.index + 1, pn.num) for pn in cand
               if abs((pn.index + 1 - pn.num) - mode_off) <= 2]
    anchors.sort()
    monotone: list[tuple[int, int]] = []
    for a in anchors:
        if monotone and a[1] <= monotone[-1][1]:
            continue
        monotone.append(a)
    seg = Segment(monotone)
    return PageMap(seg, [seg])


class PageMap:
    """把目录里的印刷页码换算成 PDF 页码。"""

    def __init__(self, segment: Segment | None, all_segments: list[Segment]):
        self.segment = segment
        self.segments = all_segments
        self._anchors = sorted(segment.anchors) if segment else []

    @property
    def ok(self) -> bool:
        return bool(self.segment) and len(self.segment) >= 3

    @property
    def offset(self) -> int:
        return self.segment.offset if self.segment else 0

    def to_phys(self, printed: int) -> int | None:
        if not self._anchors:
            return None
        # 找最近的两个锚点
        lo = None
        hi = None
        for phys, num in self._anchors:
            if num <= printed:
                lo = (phys, num)
            if num >= printed and hi is None:
                hi = (phys, num)
        if lo and hi and lo[1] == hi[1]:
            return lo[0] + (printed - lo[1])
        if lo and hi:
            off_lo = lo[0] - lo[1]
            off_hi = hi[0] - hi[1]
            if abs(off_lo - off_hi) <= 1:
                return printed + off_lo
            # 中间有插图/插页，线性插值
            if hi[1] > lo[1]:
                ratio = (printed - lo[1]) / (hi[1] - lo[1])
                return round(lo[0] + ratio * (hi[0] - lo[0]))
        if lo:
            return printed + (lo[0] - lo[1])
        if hi:
            return printed + (hi[0] - hi[1])
        return None

    def describe(self) -> str:
        if not self.segment:
            return "未识别到页码"
        return (f"正文段：物理第 {self.segment.start} 页起，共 {len(self.segment)} 个页码锚点，"
                f"偏移 {self.offset:+d}（偏移波动 {self.segment.offset_spread}）")


def choose_segment(segs: list[Segment], body_start: int | None,
                   toc_pages: list[int]) -> Segment | None:
    """挑出"正文"那一段：优先包含正文起始页的那段，否则取跨度最大的那段。"""
    if not segs:
        return None
    if body_start is not None:
        start = body_start + 1
        for s in segs:
            if s.start - 2 <= start <= s.end + 2:
                return s
    # 排除目录页所在段
    toc_set = {p + 1 for p in toc_pages}
    cands = [s for s in segs if not (toc_set & {p for p, _ in s.anchors})]
    cands = cands or segs
    return max(cands, key=lambda s: (s.end - s.start, len(s)))
