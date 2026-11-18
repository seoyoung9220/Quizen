"""B-2: PDF → 페이지 텍스트 → 청크 를 실행하고 결과를 data/extracted/<파일명>.json 으로 저장한다.

사용법: python tools/extract_chunks.py <PDF 경로> [--verify]
  --verify : 청크 전체를 모델로 다시 세서 추정 토큰 수와의 오차를 확인한다 (느림).
"""
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quizen import config  # noqa: E402
from quizen.chunker import make_chunks  # noqa: E402
from quizen.llm import TokenCounter  # noqa: E402
from quizen.pdf_extract import extract_pages, file_sha256  # noqa: E402


def main(pdf: str, verify: bool) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    path = Path(pdf)
    counter = TokenCounter()

    t0 = time.time()
    pages = extract_pages(path)
    t1 = time.time()
    chunks = make_chunks(pages, counter, config.CHUNK_TARGET_TOKENS, config.CHUNK_OVERLAP_TOKENS)
    t2 = time.time()
    counter.save()

    out_dir = config.DATA_DIR / "extracted"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{path.stem}.json"
    out.write_text(json.dumps({
        "source_document": path.name,
        "sha256": file_sha256(path),
        "page_count": len(pages),
        "model": config.MODEL,
        "chunk_target_tokens": config.CHUNK_TARGET_TOKENS,
        "chunk_overlap_tokens": config.CHUNK_OVERLAP_TOKENS,
        "pages": [asdict(p) for p in pages],
        "chunks": [asdict(c) for c in chunks],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"파일: {path.name} / {len(pages)}페이지")
    print(f"추출 {t1 - t0:.1f}s, 토큰 계산+청크 분할 {t2 - t1:.1f}s "
          f"(모델 호출 {counter.calls}회, 나머지는 캐시)")
    print(f"청크 목표 {config.CHUNK_TARGET_TOKENS} / overlap {config.CHUNK_OVERLAP_TOKENS} 토큰\n")
    print(f"{'청크':>4} {'페이지':>9} {'새 시작':>6} {'overlap':>7} {'추정토큰':>8} {'글자':>6}"
          + (f" {'실측토큰':>8} {'오차':>6}" if verify else ""))
    for c in chunks:
        row = (f"{c.chunk_id:>4} {c.page_start:>4}-{c.page_end:<4} {c.new_page_start:>6} "
               f"{c.overlap_tokens:>7} {c.tokens:>8} {len(c.text):>6}")
        if verify:
            real = counter(c.text)
            row += f" {real:>8} {(c.tokens - real) / real:>+6.1%}"
        print(row)
    if verify:
        counter.save()
    print(f"\n저장: {out}")


if __name__ == "__main__":
    main(sys.argv[1], "--verify" in sys.argv[2:])
