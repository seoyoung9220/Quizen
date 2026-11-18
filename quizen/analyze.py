"""PDF 분석(추출 + 토큰 계산 + 청크 분할)을 한 번에 하고, 파일 해시 기준으로 결과를 저장해 둔다.

같은 PDF를 다시 올리면 저장된 결과를 바로 쓴다. 설정(모델, 청크 크기)이 바뀌었으면 다시 분석한다.
"""
import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from . import config
from .chunker import make_chunks, page_marker
from .llm import TokenCounter
from .pdf_extract import extract_pages, file_sha256

Progress = Callable[[str, float], None]   # (설명, 0~1 진행률)


def _noop(*_a) -> None:
    pass


def analyze_pdf(path: Path, progress: Progress = _noop) -> dict:
    sha = file_sha256(path)
    out = config.DATA_DIR / "extracted" / f"{sha[:16]}.json"
    settings = {"model": config.MODEL, "chunk_target_tokens": config.CHUNK_TARGET_TOKENS,
                "chunk_overlap_tokens": config.CHUNK_OVERLAP_TOKENS}
    if out.exists():
        cached = json.loads(out.read_text(encoding="utf-8"))
        if all(cached.get(k) == v for k, v in settings.items()):
            progress("저장된 분석 결과 사용", 1.0)
            return cached

    progress("텍스트 추출 중", 0.0)
    pages = extract_pages(path)

    counter = TokenCounter()
    for i, p in enumerate(pages, 1):          # 페이지 토큰을 먼저 세며 진행률 표시 (이미 센 페이지는 캐시)
        progress(f"토큰 계산 중 ({i}/{len(pages)}페이지)", i / len(pages))
        counter("\n".join([page_marker(p.page), *p.text.splitlines()]))
    counter.save()

    progress("청크 분할 중", 1.0)
    chunks = make_chunks(pages, counter, config.CHUNK_TARGET_TOKENS, config.CHUNK_OVERLAP_TOKENS)
    counter.save()

    data = {"source_document": path.name, "sha256": sha, "page_count": len(pages), **settings,
            "pages": [asdict(p) for p in pages], "chunks": [asdict(c) for c in chunks]}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
