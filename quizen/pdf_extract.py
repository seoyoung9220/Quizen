"""PDF에서 페이지별 텍스트를 추출한다.

슬라이드 PDF에서 흔한 문제를 보정한다.
- 한국어 띄어쓰기 소실: PDF에 공백 문자가 없고 글자 위치만 띄워진 경우,
  글자 사이 간격이 글자 크기의 일정 비율보다 크면 공백을 넣는다.
- 글머리 기호 깨짐: Wingdings 기호가 '¡' 등으로 읽히는 것을 '•'로 바꾼다.
- 모든 페이지에 반복되는 머리글(예: 과목명)과 쪽 번호를 제거한다.
"""
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pymupdf

GAP_RATIO = 0.15          # 글자 간격 > 글자 크기 × 0.15 이면 단어 경계로 본다
REPEAT_RATIO = 0.5        # 전체 페이지의 절반 이상에 나오는 짧은 줄은 머리글로 본다
# '¡'는 이 PDF에서 실제로 확인된 깨진 글머리 기호, ··는 Wingdings 글머리 기호가
# 유니코드 사용자 정의 영역으로 읽힌 경우다.
BULLET_MAP = str.maketrans({"¡": "•", "": "•", "": "•", "": "•"})


@dataclass
class Page:
    page: int        # 1부터 시작
    text: str


# 기호 전용 글꼴(Wingdings 등)의 글자는 일반 문자로 읽힌다 (예: 체크 표시 → 'P', 화살표 → 'à').
# 글꼴 이름으로 구분해 의미가 같은 기호로 바꾼다. 화살표 외에는 모두 글머리 기호로 본다.
DINGBAT_FONTS = ("Wingdings", "Webdings")
DINGBAT_ARROWS = set("àèðÜÞ")


def _dingbat(c: str) -> str:
    if c.isspace():
        return c
    return "→" if c in DINGBAT_ARROWS else "•"


def _line_text(line: dict) -> str:
    """rawdict의 한 줄을 글자 좌표로 다시 조립하며 빠진 공백을 복원한다."""
    out: list[str] = []
    prev = None
    for span in line["spans"]:
        size = span["size"] or 10
        dingbat = any(f in span["font"] for f in DINGBAT_FONTS)
        for ch in span["chars"]:
            c = _dingbat(ch["c"]) if dingbat else ch["c"]
            if prev is not None:
                gap = ch["bbox"][0] - prev["bbox"][2]
                if gap > size * GAP_RATIO and not c.isspace() and not prev["c"].isspace():
                    out.append(" ")
            out.append(c)
            prev = ch
    return "".join(out)


def _page_lines(page: pymupdf.Page) -> list[str]:
    lines: list[str] = []
    for block in page.get_text("rawdict")["blocks"]:
        if block.get("type") != 0:     # 0 = 텍스트 블록 (1은 이미지)
            continue
        for line in block["lines"]:
            t = re.sub(r"[ \t ]+", " ", _line_text(line).translate(BULLET_MAP)).strip()
            if t:
                lines.append(t)
    return lines


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def extract_pages(path: str | Path) -> list[Page]:
    doc = pymupdf.open(path)
    raw = [_page_lines(p) for p in doc]

    # 머리글/바닥글 후보: 여러 페이지에 똑같이 나오는 짧은 줄
    freq = Counter(line for lines in raw for line in set(lines))
    repeated = {l for l, n in freq.items() if n >= max(2, len(raw) * REPEAT_RATIO) and len(l) <= 40}

    pages: list[Page] = []
    for i, lines in enumerate(raw, start=1):
        kept = [l for l in lines if l not in repeated and not re.fullmatch(r"\d{1,3}", l)]
        pages.append(Page(page=i, text="\n".join(kept)))
    return pages
