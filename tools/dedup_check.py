"""C-7 확인: 실제 앱과 같은 흐름(DB 등록 → DB 이력 → build_mixed → 회차 저장 → 구간 기록)으로
같은 PDF에서 회차를 연달아 만들고, 중복이 걸러지는지 본다.

사용법: QUIZEN_DATA_DIR=<임시 폴더> python tools/dedup_check.py <PDF> [--rounds 4 --mcq 3 --short 2]
결과: 회차별 생성 수·중복 폐기·재생성, 마지막에 전체 문제를 서로 다시 비교해 새어 나온 중복 목록
"""
import argparse
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from quizen import config  # noqa: E402
from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.dedup import duplicate_reason  # noqa: E402
from quizen.history import chunk_key  # noqa: E402
from quizen.pipeline import build_mixed  # noqa: E402
from quizen.quiz import QuizSession  # noqa: E402
from quizen.store import Store  # noqa: E402
from quizen.verify import verify_question  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--mcq", type=int, default=3)
    ap.add_argument("--short", type=int, default=2)
    ap.add_argument("--difficulty", default="중")
    a = ap.parse_args()
    s = Store()
    p = Path(a.pdf)
    d = s.add_document(p, p.name, analyze_pdf(p)["pages"], title="9주차")
    counts = {"mcq": a.mcq, "short": a.short}
    summary = []
    for rnd in range(1, a.rounds + 1):
        ext = s.extracted([d])
        hist = s.history([d])                       # 앱과 같이 매 회차 DB에서 이력을 새로 읽는다
        t = time.time()
        qs, rep = build_mixed(ext, counts, a.difficulty, verifier=verify_question, history=hist,
                              rng=random.Random())
        secs = time.time() - t
        keys = {c["chunk_id"]: chunk_key(c) for c in ext["chunks"]}
        quiz = s.create_quiz(QuizSession(qs), [d], a.difficulty, sum(counts.values()), chunk_keys=keys,
                             report={"discarded": dict(rep.discarded)})
        hist.record(qs, rep.chunks_used)
        dups = [x for x in rep.discard_samples if x.startswith("[중복]")]
        summary.append({"round": rnd, "produced": len(qs), "seconds": round(secs), "discarded": dict(rep.discarded),
                        "regen": rep.regen_rounds_used, "fallback": rep.fallback_chunks,
                        "chunks": [f"{c['page_start']}-{c['page_end']}" for c in rep.chunks_used],
                        "history_size": len(hist.questions)})
        print(f"[회차 {rnd}] {len(qs)}/{sum(counts.values())}문제 {secs:.0f}s | 이전 문제 {len(hist.questions)}개와 비교 | "
              f"폐기 {dict(rep.discarded) or '없음'} | 재생성 {rep.regen_rounds_used} · 보충 {rep.fallback_chunks}", flush=True)
        for x in dups:
            print("     중복 폐기:", x[:120])
        for q in qs:
            ans = f" → {q.answer_text}" if q.qtype == "short" else ""
            print(f"     - [{q.qtype} p{q.page}] {q.question[:70]}{ans}")

    # 전체 문제를 서로 다시 비교: 파이프라인이 놓친 중복이 있는지
    rows = [r for qz in s.conn.execute("SELECT id FROM quizzes ORDER BY id") for r in s.results(qz["id"])]
    leaked = []
    for i, r in enumerate(rows):
        prev = [{"question": x["question"], "choices": x["choices"], "evidence": x["evidence"],
                 "answer": x["answer_text"] if x["qtype"] == "short" else ""} for x in rows[:i]]
        why = duplicate_reason(r["question"], r["choices"], r["evidence"], prev,
                               answer=r["answer_text"] if r["qtype"] == "short" else "")
        if why:
            leaked.append(f"{r['question'][:50]} ⇐ {why}")
    usage = s.history([d]).chunk_usage
    print(f"\n전체 {len(rows)}문제, 새어 나온 중복 {len(leaked)}개")
    for x in leaked:
        print("   -", x)
    print("구간 사용 횟수:", dict(sorted(usage.items())))
    print("페이지 분포:", dict(sorted(Counter(r["page"] for r in rows).items())))
    out = config.PROJECT_DIR / "data" / "phase_b" / f"dedup_check_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({"summary": summary, "leaked": leaked, "usage": usage,
                               "questions": [{"q": r["question"], "type": r["qtype"], "page": r["page"],
                                              "answer": r["answer_text"]} for r in rows]},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print("저장:", out)


if __name__ == "__main__":
    main()
