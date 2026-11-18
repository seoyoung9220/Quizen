"""출제 범위 좁히기: 페이지 범위와 주제 키워드.

범위는 모델에게 글로 부탁하지 않고 코드로 처리한다.
- 페이지 범위: 범위 안의 페이지만으로 청크를 다시 만든다. 모델은 범위 밖 내용을 아예 보지 못한다.
- 주제 키워드: 키워드가 들어 있는 청크만 남긴다 (띄어쓰기·대소문자 무시).
범위 밖 청크로 모델을 부르지 않으니 시간도 덜 든다.
"""
from dataclasses import asdict

from . import config
from .chunker import make_chunks
from .llm import TokenCounter
from .pdf_extract import Page


def _norm(s: str) -> str:
    return "".join(s.split()).lower()


def parse_keywords(text: str) -> list[str]:
    return [k.strip() for k in text.replace("\n", ",").split(",") if k.strip()]


def restrict(extracted: dict, page_from: int | None = None, page_to: int | None = None,
             keywords: list[str] | None = None) -> dict:
    """범위를 적용한 새 extracted를 돌려준다. 원본은 바꾸지 않는다.

    키워드가 있는데 맞는 청크가 하나도 없으면 chunks가 빈 목록이 된다 (화면에서 안내).
    """
    n = extracted["page_count"]
    a = max(1, page_from or 1)
    b = min(n, page_to or n)
    out = dict(extracted)
    out["scope"] = {"page_from": a, "page_to": b, "keywords": keywords or []}

    if (a, b) != (1, n):
        if len(extracted.get("doc_ids", [])) > 1:
            raise ValueError("여러 PDF를 함께 출제할 때는 페이지 범위를 지정할 수 없습니다 (PDF마다 페이지 번호가 겹침).")
        pages = [p for p in extracted["pages"] if a <= p["page"] <= b]
        chunks = make_chunks([Page(page=p["page"], text=p["text"]) for p in pages], TokenCounter(),
                             config.CHUNK_TARGET_TOKENS, config.CHUNK_OVERLAP_TOKENS)
        out["pages"] = pages
        out["chunks"] = [asdict(c) for c in chunks]
        # DB에서 온 자료면 PDF 번호·이름을 다시 붙인다 (출제 이력·복습 위치에 필요)
        first = extracted["chunks"][0] if extracted["chunks"] else {}
        if first.get("doc_id") is not None:
            for c in out["chunks"]:
                c.update(doc_id=first["doc_id"], source_document=first["source_document"],
                         chunk_id=first["doc_id"] * 1000 + c["chunk_id"])

    if keywords:
        keys = [_norm(k) for k in keywords]
        out["chunks"] = [c for c in out["chunks"] if any(k in _norm(c["text"]) for k in keys)]
    return out
