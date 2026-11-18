"""페이지 텍스트를 토큰 기준 청크로 나눈다.

- 각 페이지 앞에 [[PAGE n]] 표시를 넣는다. 나중에 evidence가 어느 페이지에서 왔는지 코드로 찾을 때 쓴다.
- 토큰 수는 모델 토크나이저로 페이지마다 실제로 센다 (한국어 약 2자, 영어 약 6자가 1토큰이라
  글자 수 기준으로 자르면 오차가 크다).
- 청크 토큰 수는 구성 조각의 토큰 합으로 계산한다. 페이지의 일부만 쓰는 경우는 그 페이지의
  '글자당 토큰' 비율로 추정한다. (전체를 다시 세면 27B 모델에서 청크당 20초 이상 걸림)
- 직전 청크 끝에서 overlap 토큰만큼을 다음 청크 앞에 겹쳐 넣는다. 마지막 페이지가 overlap보다
  크면 그 페이지의 끝 줄들만 가져온다.
- 한 페이지가 목표 크기보다 크면 줄 단위로 나누고, 조각마다 같은 [[PAGE n]] 표시를 붙인다.
"""
from dataclasses import dataclass, field
from typing import Callable

from .pdf_extract import Page

SEP = "\n\n"


def page_marker(n: int) -> str:
    return f"[[PAGE {n}]]"


@dataclass
class Piece:
    """청크를 구성하는 단위: 한 페이지 또는 큰 페이지의 일부."""
    page: int
    lines: list[str]
    tokens: float                 # 마커 포함 토큰 수 (실측 또는 비율 추정)
    per_char: float               # 이 페이지의 글자당 토큰 비율 (부분 추정용)

    @property
    def text(self) -> str:
        return "\n".join([page_marker(self.page), *self.lines])


@dataclass
class Chunk:
    chunk_id: int
    page_start: int
    page_end: int
    new_page_start: int           # overlap을 뺀, 이 청크에서 새로 시작하는 페이지
    overlap_tokens: int           # 앞 청크에서 겹쳐 가져온 양 (추정)
    text: str
    tokens: int                   # 조각 토큰 합 (추정)
    pages: list[int] = field(default_factory=list)


def _estimate(lines: list[str], page: int, per_char: float) -> float:
    return len("\n".join([page_marker(page), *lines])) * per_char


def _pieces(pages: list[Page], count: Callable[[str], int], target: int) -> list[Piece]:
    out: list[Piece] = []
    for p in pages:
        lines = p.text.splitlines()
        full = "\n".join([page_marker(p.page), *lines])
        t = count(full)                              # 페이지 단위로만 실제 토큰을 센다
        per_char = t / max(len(full), 1)
        if t <= target:
            out.append(Piece(p.page, lines, t, per_char))
            continue
        buf: list[str] = []
        for line in lines:
            if buf and _estimate(buf + [line], p.page, per_char) > target:
                out.append(Piece(p.page, buf, _estimate(buf, p.page, per_char), per_char))
                buf = []
            buf.append(line)
        if buf:
            out.append(Piece(p.page, buf, _estimate(buf, p.page, per_char), per_char))
    return out


def _overlap_head(prev: list[Piece], overlap: int) -> list[Piece]:
    """직전 청크의 조각들 끝에서부터 overlap 토큰 안에 들어가는 만큼을 가져온다."""
    head: list[Piece] = []
    used = 0.0
    for piece in reversed(prev):
        if used + piece.tokens <= overlap:
            head.insert(0, piece)
            used += piece.tokens
            continue
        # 조각 전체는 너무 크면, 끝 줄부터 남은 예산만큼만 가져온다
        tail: list[str] = []
        for line in reversed(piece.lines):
            if used + _estimate([line, *tail], piece.page, piece.per_char) > overlap:
                break
            tail.insert(0, line)
        if tail:
            head.insert(0, Piece(piece.page, tail,
                                 _estimate(tail, piece.page, piece.per_char), piece.per_char))
        break
    return head


def make_chunks(pages: list[Page], count: Callable[[str], int],
                target: int, overlap: int) -> list[Chunk]:
    pieces = _pieces(pages, count, target)
    chunks: list[Chunk] = []
    prev_body: list[Piece] = []
    i = 0
    while i < len(pieces):
        head = _overlap_head(prev_body, overlap) if chunks else []
        head_tokens = sum(p.tokens for p in head)

        body: list[Piece] = []
        total = head_tokens
        while i < len(pieces) and (not body or total + pieces[i].tokens <= target):
            body.append(pieces[i])
            total += pieces[i].tokens
            i += 1

        members = head + body
        pages_in = sorted({p.page for p in members})
        chunks.append(Chunk(
            chunk_id=len(chunks) + 1,
            page_start=pages_in[0],
            page_end=pages_in[-1],
            new_page_start=body[0].page,
            overlap_tokens=round(head_tokens),
            text=SEP.join(p.text for p in members),
            tokens=round(total),
            pages=pages_in,
        ))
        prev_body = members
    return chunks
