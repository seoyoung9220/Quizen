"""B-3 시험: 추출 결과(JSON)의 특정 청크로 문제를 생성해 본다.

사용법: python tools/gen_test.py <extracted.json> --chunk 3 --difficulty 하 --num 3
        python tools/gen_test.py <extracted.json> --alloc 20    (배분 결과만 출력)
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quizen.generate import GenStats, allocate, generate_for_chunk, info_score  # noqa: E402


def squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("extracted")
    ap.add_argument("--chunk", type=int)
    ap.add_argument("--difficulty", default="중")
    ap.add_argument("--num", type=int, default=3)
    ap.add_argument("--alloc", type=int)
    ap.add_argument("--existing", default="", help="이미 출제된 문제 저장 파일(jsonl)")
    a = ap.parse_args()
    d = json.loads(Path(a.extracted).read_text(encoding="utf-8"))
    chunks, pages = d["chunks"], d["pages"]

    if a.alloc:
        alloc = allocate(chunks, pages, a.alloc)
        print(f"{a.alloc}문제 배분 (최대 {6}/청크)")
        for c, n in zip(chunks, alloc):
            print(f"  청크 {c['chunk_id']} p{c['new_page_start']}-{c['page_end']}: "
                  f"정보량 {info_score(c, pages):>4} → {n}문제")
        print(f"  합계 {sum(alloc)}")
        return

    chunk = next(c for c in chunks if c["chunk_id"] == a.chunk)
    existing_path = Path(a.existing) if a.existing else None
    existing = []
    if existing_path and existing_path.exists():
        existing = [json.loads(l)["question"] for l in existing_path.read_text(encoding="utf-8").splitlines() if l]

    stats = GenStats()
    qs = generate_for_chunk(chunk["text"], a.difficulty, a.num, existing, stats)
    print(f"청크 {a.chunk} (p{chunk['page_start']}-{chunk['page_end']}) 난이도 {a.difficulty} 요청 {a.num}"
          f" → {len(qs)}개 / 호출 {stats.calls}회 / {stats.seconds:.1f}s / 파싱 실패 {stats.parse_failures} {stats.errors}")
    body = squash(chunk["text"])
    for k, q in enumerate(qs, 1):
        hit = squash(q.evidence) in body
        print(f"\n[{k}] ({q.difficulty}) {q.question}")
        for i, c in enumerate(q.choices):
            print(f"   {'*' if i == q.answer_index else ' '} {i}. {c}")
        print(f"   evidence(원문 포함={hit}): {q.evidence}")
    if existing_path:
        with existing_path.open("a", encoding="utf-8") as f:
            for q in qs:
                f.write(json.dumps({"question": q.question}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
