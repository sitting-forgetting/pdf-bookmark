"""PDF 体检：判断文字层、已有书签、页面尺寸等，决定走文本路线还是 OCR 路线。"""
from __future__ import annotations

from dataclasses import dataclass, field

import fitz


@dataclass
class Profile:
    path: str
    pages: int
    width: float
    height: float
    rotation: int
    has_text: bool
    text_pages: int            # 抽样中有文字层的页数
    sampled: int
    outline_len: int
    outline_sample: list[tuple[int, str, int]] = field(default_factory=list)
    encrypted: bool = False

    @property
    def kind(self) -> str:
        if self.text_pages == 0:
            return "scan"       # 纯图片扫描件
        if self.text_pages / max(1, self.sampled) > 0.6:
            return "text"       # 有正常文字层
        return "mixed"          # 混合（部分页图片、部分页有字）

    def summary(self) -> str:
        kind_cn = {"scan": "扫描件（无文字层）", "text": "文字版", "mixed": "混合"}[self.kind]
        return (
            f"{self.pages} 页 · {self.width:.0f}×{self.height:.0f}pt · {kind_cn} · "
            f"已有书签 {self.outline_len} 条"
        )


def profile(path: str, sample: int = 24) -> Profile:
    doc = fitz.open(path)
    try:
        n = doc.page_count
        first = doc[0].rect
        step = max(1, n // sample)
        idxs = list(range(0, n, step))[:sample]
        text_pages = 0
        for i in idxs:
            try:
                txt = doc[i].get_text("text").strip()
            except Exception:
                txt = ""
            if len(txt) > 60:
                text_pages += 1
        toc = doc.get_toc(simple=True)
        return Profile(
            path=path,
            pages=n,
            width=first.width,
            height=first.height,
            rotation=doc[0].rotation,
            has_text=text_pages > 0,
            text_pages=text_pages,
            sampled=len(idxs),
            outline_len=len(toc),
            outline_sample=toc[:10],
            encrypted=doc.is_encrypted,
        )
    finally:
        doc.close()
