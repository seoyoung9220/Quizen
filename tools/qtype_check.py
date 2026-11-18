"""문제 유형 실제 모델 확인: 객관식·단답형·서술형을 섞어 만들고, 답을 다양하게 써서 채점·오답 풀이까지 본다.

사용법: QUIZEN_DATA_DIR=<임시 폴더> python tools/qtype_check.py <PDF> [--mcq 2 --short 2 --essay 2] [--difficulty 중]
답 쓰는 방식 (채점이 합리적인지 보기 위해 일부러 다양하게):
- 단답 1번째: 정답을 다른 표기(인정 답안이 있으면 그것, 없으면 띄어쓰기를 바꾼 정답)  → 정답이어야 함
- 단답 2번째: 관련은 있지만 다른 용어 (근거 문장의 다른 단어)                       → 오답이어야 함
- 서술 1번째: 모범 답안의 앞 절반만                                                → 부분 점수
- 서술 2번째: 질문과 상관없는 문장                                                 → 0점
"""
import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.explain import explain_stream  # noqa: E402
from quizen.grade_llm import grade_pending  # noqa: E402
from quizen.pipeline import build_mixed  # noqa: E402
from quizen.quiz import QuestionResult, QuizSession  # noqa: E402
from quizen.store import Store  # noqa: E402
from quizen.verify import verify_question  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--mcq", type=int, default=2)
    ap.add_argument("--short", type=int, default=2)
    ap.add_argument("--essay", type=int, default=2)
    ap.add_argument("--difficulty", default="중")
    a = ap.parse_args()
    s = Store()
    p = Path(a.pdf)
    d = s.add_document(p, p.name, analyze_pdf(p)["pages"], title="9주차")
    ext = s.extracted([d])
    counts = {"mcq": a.mcq, "short": a.short, "essay": a.essay}
    t = time.time()
    qs, rep = build_mixed(ext, counts, a.difficulty, verifier=verify_question, history=s.history([d]),
                          rng=random.Random(8), progress=lambda st, i, n, m: print(f"  [{st}] {m}", flush=True))
    gen_s = time.time() - t
    quiz = s.create_quiz(QuizSession(qs), [d], a.difficulty, sum(counts.values()), scope={"counts": counts})
    print(f"\n생성 {len(qs)}/{sum(counts.values())}문제 {gen_s:.0f}s, 폐기 {dict(rep.discarded) or '없음'}")
    for x in rep.discard_samples:
        print("   -", x[:110])

    rows = s.results(quiz)
    shorts = [r for r in rows if r["qtype"] == "short"]
    essays = [r for r in rows if r["qtype"] == "essay"]
    for r in rows:
        if r["qtype"] == "mcq":
            s.save_answer(r["id"], r["answer_index"])
    if shorts:
        r = shorts[0]
        alt = r["accepted"][0] if r["accepted"] else (r["answer_text"].replace(" ", "") if " " in r["answer_text"]
                                                     else " ".join(r["answer_text"]))
        s.save_answer(r["id"], user_text=alt)
    if len(shorts) > 1:
        r = shorts[1]
        words = [w for w in r["evidence"].split() if len(w) >= 2 and w not in r["answer_text"]]
        s.save_answer(r["id"], user_text=words[len(words) // 2] if words else "모름")
    if essays:
        r = essays[0]
        s.save_answer(r["id"], user_text=r["answer_text"][: max(20, len(r["answer_text"]) // 2)])
    if len(essays) > 1:
        s.save_answer(essays[1]["id"], user_text="오늘 날씨가 맑아서 산책하기 좋았다.")

    t = time.time()
    pending = s.grade(quiz)
    grade_pending(s, pending)
    print(f"\n채점: 모델 채점 {len(pending)}문제 {time.time() - t:.0f}s, 점수 {s.score(quiz)}")
    for r in s.results(quiz):
        print(f"\n[{r['number']}] {r['qtype']} {'정답' if r['correct'] else '오답'} | {r['question']}")
        if r["qtype"] == "mcq":
            continue
        print(f"   내 답: {r['user_text']}")
        print(f"   정답/모범: {r['answer_text']}" + (f"  (인정: {r['accepted']})" if r["accepted"] else ""))
        dt = r["auto_detail"] or {}
        if r["qtype"] == "short":
            print(f"   채점({dt.get('method')}): {dt.get('reason')}")
        else:
            print(f"   점수 {r['auto_score']}: " + "; ".join(f"{'O' if p['met'] else 'X'} {p['point']} — {p['comment']}"
                                                   for p in dt.get("points", [])))
            print(f"   피드백: {dt.get('feedback')}")
            print(f"   기준 근거 페이지: {[x['page'] for x in r['rubric']]}")
    wrong = [r for r in s.results(quiz) if not r["correct"] and r["qtype"] != "mcq"]
    if wrong:
        r = wrong[0]
        res = QuestionResult(number=r["number"], question=r["question"], choices=(), difficulty=r["difficulty"],
                             user_index=None, answer_index=-1, correct=False, evidence=r["evidence"], page=r["page"],
                             source_document=r["source_document"], chunk_id=0, evidence_pages=tuple(r["evidence_pages"]),
                             qtype=r["qtype"], user_text=r["user_text"] or "", answer_text=r["answer_text"] or "")
        t = time.time()
        text = "".join(explain_stream(res, s.document_pages(d)))
        print(f"\n[오답 풀이 {r['qtype']} {time.time() - t:.0f}s]\n{text}")


if __name__ == "__main__":
    main()
