"""문제 세트를 만들어 data/quizzes/ 에 저장하고 요약을 출력한다.

사용법: python tools/build_quiz.py <extracted.json> --num 5 --difficulty 중 [--verify]
"""
import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quizen import config  # noqa: E402
from quizen.pipeline import build_quiz  # noqa: E402


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("extracted")
    ap.add_argument("--num", type=int, default=5)
    ap.add_argument("--difficulty", default="중", choices=["하", "중", "상"])
    ap.add_argument("--verify", action="store_true", help="B-5 정답 숨김 검증 사용")
    a = ap.parse_args()

    d = json.loads(Path(a.extracted).read_text(encoding="utf-8"))
    verifier = None
    if a.verify:
        from quizen.verify import verify_question
        verifier = verify_question

    def progress(stage, i, n, msg):
        print(f"  [{stage}] {msg}", flush=True)

    qs, rep = build_quiz(d, a.num, a.difficulty, verifier=verifier, progress=progress)

    out_dir = config.DATA_DIR / "quizzes"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{a.difficulty}{a.num}.json"
    rep_d = asdict(rep)
    rep_d["discarded"] = dict(rep.discarded)
    out.write_text(json.dumps({"model": config.MODEL, "difficulty": a.difficulty, "report": rep_d,
                               "questions": [q.to_dict() for q in qs]}, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print(f"\n요청 {rep.requested} → 최종 {rep.produced}문제 / 전체 {rep.seconds:.1f}s "
          f"(생성 {rep.gen.seconds:.1f}s, 검증 {rep.verify_seconds:.1f}s)")
    print(f"모델 호출 {rep.gen.calls}회, 모델이 낸 문제 {rep.generated}개, 재생성 라운드 {rep.regen_rounds_used}회")
    print(f"폐기: {dict(rep.discarded) or '없음'}  구조 오류 기록: {rep.gen.errors or '없음'}")
    for s in rep.discard_samples:
        print("   -", s)
    if rep.produced:
        print(f"문제당 평균 {rep.seconds / rep.produced:.1f}s")
    print("청크별:", ", ".join(f"#{c['chunk_id']}(p{c['pages']}) {c['produced']}/{c['requested']}" for c in rep.per_chunk))
    for k, q in enumerate(qs, 1):
        print(f"\n[{k}] ({q.difficulty}) {q.question}")
        for i, c in enumerate(q.choices):
            print(f"   {'*' if i == q.answer_index else ' '} {i}. {c}")
        print(f"   근거 {q.source_document} p.{q.page} {q.evidence_pages} [{q.evidence_mode}]: {q.evidence}")
    print(f"\n저장: {out}")


if __name__ == "__main__":
    main()
