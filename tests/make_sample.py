"""生成一本小样书，用来做端到端回归测试（不依赖外部 PDF）。"""
from __future__ import annotations

import sys

import fitz

FONT = "china-s"          # PyMuPDF 内置简体中文字体


def make(path: str) -> None:
    doc = fitz.open()

    # 封面
    p = doc.new_page(width=400, height=600)
    p.insert_text((110, 200), "中国舟船小史", fontname=FONT, fontsize=26)
    p.insert_text((150, 240), "测试用书", fontname=FONT, fontsize=13)

    # 目录页（页码从 1 开始，正文另有页码，制造两套编号）
    p = doc.new_page(width=400, height=600)
    p.insert_text((175, 70), "目  录", fontname=FONT, fontsize=18)
    toc = [
        ("第一章  舟船的起源", 1, 0),
        ("第一节  独木舟", 1, 40),
        ("一、筏与皮囊", 1, 80),
        ("二、早期木船", 3, 80),
        ("第二节  木板船", 4, 40),
        ("第二章  秦汉造船", 6, 0),
        ("第一节  楼船", 6, 40),
        ("一、水军与楼船", 6, 80),
        ("二、漕运船", 8, 80),
    ]
    y = 110
    for title, page, indent in toc:
        p.insert_text((60 + indent, y), title, fontname=FONT, fontsize=11)
        # 引导点
        p.insert_text((300, y), "……", fontname=FONT, fontsize=11)
        p.insert_text((340, y), str(page), fontname=FONT, fontsize=10)
        y += 26

    # 正文：页码与目录里的印刷页码严格对应（同一页上可能有多个标题）
    pages = {
        1: [("第一章  舟船的起源", 20), ("第一节  独木舟", 13), ("一、筏与皮囊", 11)],
        2: [],
        3: [("二、早期木船", 11)],
        4: [("第二节  木板船", 13)],
        5: [],
        6: [("第二章  秦汉造船", 20), ("第一节  楼船", 13), ("一、水军与楼船", 11)],
        7: [],
        8: [("二、漕运船", 11)],
        9: [],
        10: [],
    }
    for printed in range(1, 11):
        p = doc.new_page(width=400, height=600)
        p.insert_text((60, 40), "中国舟船小史", fontname=FONT, fontsize=9)
        y = 130
        for text, size in pages[printed]:
            p.insert_text((70, y), text, fontname=FONT, fontsize=size)
            y += 34
        for k in range(10):
            p.insert_text((60, y + 20 + k * 24),
                          "这一页是正文内容，用来测试目录对齐是否准确。"[:26],
                          fontname=FONT, fontsize=10)
        p.insert_text((185, 575), f"•{printed}•", fontname=FONT, fontsize=10)

    doc.save(path)
    doc.close()
    print(f"已生成样书：{path}")


if __name__ == "__main__":
    make(sys.argv[1] if len(sys.argv) > 1 else "testdata/合成样书.pdf")
