"""수정 확인: 이력을 이어 쓰며 하→중→상 세 회차를 연속 생성해
(1) 회차마다 다른 구간이 나오는지 (2) 회차 사이 중복 문제가 없는지 (3) 오답 풀이가 페이지 원문을 쓰는지 확인한다.
사용자 PDF의 실제 이력을 건드리지 않도록 시험용 이력 파일을 쓰고 끝나면 지운다.

사용법: python tools/fix_check.py <PDF> [--num 5]
"""
import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from quizen import config  # noqa: E402
from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.dedup import duplicate_reason  # noqa: E402
from quizen.explain import explain_stream  # noqa: E402
from quizen.history import History  # noqa: E402
from quizen.pipeline import build_quiz  # noqa: E402
from quizen.quiz import QuizSession  # noqa: E402
from quizen.verify import verify_question  # noqa: E402

NOT_FOUND = ("확인할 수 없", "자료에 없", "포함되어 있지 않", "언급되어 있지 않", "제시되어 있지 않")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--num", type=int, default=5)
    a = ap.parse_args()

    ext = analyze_pdf(Path(a.pdf))
    pages = {p["page"]: p["text"] for p in ext["pages"]}
    hist = History("test-fixcheck-" + ext["sha256"])
    hist.path.unlink(missing_ok=True)
    rng = random.Random()
    out = {"sets": []}
    try:
        for diff in ("하", "중", "상"):
            t = time.time()
            qs, rep = build_quiz(ext, a.num, diff, verifier=verify_question, history=hist, rng=rng)
            secs = time.time() - t
            ranges = [f"{c['page_start']}-{c['page_end']}" for c in rep.chunks_used]
            # 이전 회차 문제들과 다시 비교 (파이프라인이 걸렀어야 하므로 0이어야 함)
            leaked = [q.question[:40] for q in qs if duplicate_reason(q.question, q.choices, q.evidence, hist.questions)]
            hist.record(qs, rep.chunks_used)

            sess = QuizSession(qs)
            keys = {p.qid: sess._keys[p.qid].answer_index for p in sess.public}
            wrong = {p.qid: (keys[p.qid] + 1) % 4 for p in sess.public}
            g = sess.grade(wrong)
            r = g.results[-1]
            te = time.time()
            text = "".join(explain_stream(r, pages))
            not_found = [m for m in NOT_FOUND if m in text]

            s = {"difficulty": diff, "produced": len(qs), "seconds": round(secs, 1),
                 "per_question": round(secs / max(len(qs), 1), 1),
                 "gen_seconds": round(rep.gen.seconds, 1), "verify_seconds": round(rep.verify_seconds, 1),
                 "chunks": ranges, "pages": sorted({q.page for q in qs}),
                 "discarded": dict(rep.discarded), "discard_samples": rep.discard_samples,
                 "leaked_duplicates": leaked,
                 "questions": [q.question for q in qs],
                 "explain": {"question": r.question, "page": r.page, "seconds": round(time.time() - te, 1),
                             "not_found_phrases": not_found, "text": text}}
            out["sets"].append(s)
            print(f"[{diff}] {len(qs)}/{a.num}문제 {secs:.0f}s (문제당 {s['per_question']}s, 생성 {s['gen_seconds']}s"
                  f" / 검증 {s['verify_seconds']}s) 구간 {ranges} 페이지 {s['pages']}")
            print(f"     폐기 {s['discarded'] or '없음'} {rep.discard_samples}")
            print(f"     이전 회차와 중복 새어 나옴: {leaked or '없음'}")
            print(f"     오답 풀이 {s['explain']['seconds']}s, '자료에 없음'류 표현: {not_found or '없음'}")
            for q in qs:
                print(f"       - {q.question}")
            print(flush=True)
        all_pages = sorted({p for s in out["sets"] for p in s["pages"]})
        all_chunks = sorted({c for s in out["sets"] for c in s["chunks"]})
        print(f"세 회차 합계: 구간 {len(all_chunks)}/{len(ext['chunks'])}개 {all_chunks}, 페이지 {all_pages}")
        dest = config.DATA_DIR / "phase_b" / f"fixcheck_{time.strftime('%Y%m%d_%H%M%S')}.json"
        dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"저장: {dest}")
    finally:
        hist.path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
