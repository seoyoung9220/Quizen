"""저장된 문제 세트를 B-5 정답 숨김 검증에 다시 돌려 본다.

사용법: python tools/verify_saved.py <quiz.json> <extracted.json>
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quizen.validate import ValidQuestion  # noqa: E402
from quizen.verify import verify_question  # noqa: E402


def main(quiz_path: str, extracted_path: str) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    quiz = json.loads(Path(quiz_path).read_text(encoding="utf-8"))
    ext = json.loads(Path(extracted_path).read_text(encoding="utf-8"))
    chunks = {c["chunk_id"]: c["text"] for c in ext["chunks"]}
    total = 0.0
    for k, d in enumerate(quiz["questions"], 1):
        q = ValidQuestion(**d)
        t = time.time()
        ok, why = verify_question(q, chunks[q.chunk_id])
        dt = time.time() - t
        total += dt
        print(f"[{k}] {'통과' if ok else '폐기'} ({dt:.1f}s) 정답 {q.answer_index} | {q.question[:45]}")
        print(f"     {why}")
    print(f"\n문제당 평균 검증 시간 {total / len(quiz['questions']):.1f}s")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
