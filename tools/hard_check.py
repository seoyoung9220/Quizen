"""난이도 '상' 재측정 + 시간 분석.
QUIZEN_DATA_DIR에 있는 DB에서 제목에 <doc>이 들어간 PDF를 골라, 그 이력 그대로 '상' 객관식을 만든다.
호출마다 읽기(prompt)·쓰기(출력) 시간을 모아 어디에 시간이 드는지 보여 준다.

사용법: QUIZEN_DATA_DIR=<폴더> python tools/hard_check.py <doc 제목 일부> [--num 5] [--difficulty 상] [--pdf <등록할 PDF>]
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

from quizen import config, verify  # noqa: E402
from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.llm import use_only  # noqa: E402
from quizen.pipeline import build_quiz  # noqa: E402
from quizen.store import Store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("doc")
    ap.add_argument("--num", type=int, default=5)
    ap.add_argument("--difficulty", default="상")
    ap.add_argument("--pdf")
    a = ap.parse_args()
    s = Store()
    if a.pdf:
        p = Path(a.pdf)
        s.add_document(p, p.name, analyze_pdf(p)["pages"])
    d = next(x["id"] for x in s.tree()["documents"] if a.doc in x["title"])
    hist = s.history([d])
    print(f"PDF: {s.document(d)['title']} · 이전 문제 {len(hist.questions)}개 · 구간 사용 {hist.chunk_usage}")
    use_only(config.MODEL)
    verify.TIMINGS.clear()
    t = time.time()
    qs, rep = build_quiz(s.extracted([d]), a.num, a.difficulty, verifier=verify.verify_question, history=hist,
                         rng=random.Random(), progress=lambda st, i, n, m: print(f"  [{st}] {m}", flush=True))
    secs = time.time() - t
    print(f"\n결과 {len(qs)}/{a.num}문제 · {secs:.0f}초 (문제당 {secs / max(len(qs), 1):.0f}초) · 시간 상한 걸림 {rep.timed_out}")
    print(f"폐기 {dict(rep.discarded)} · 재생성 {rep.regen_rounds_used} · 보충 {rep.fallback_chunks} · "
          f"JSON 실패 {rep.gen.parse_failures}/{rep.gen.calls}")
    for e in rep.gen.errors:
        print("  오류:", e[:160])
    for x in rep.discard_samples:
        print("  폐기:", x[:150])
    for q in qs:
        print(f"  - [p{q.page}] {q.question[:90]}")
    # 시간 분석
    rows = rep.gen.timings + verify.TIMINGS
    agg = {}
    for k in ("생성", "검증"):
        rs = [r for r in rows if r["kind"] == k]
        if rs:
            agg[k] = {"호출": len(rs), "전체초": round(sum(r["wall"] for r in rs)),
                      "읽기초": round(sum(r["prompt_s"] for r in rs)), "쓰기초": round(sum(r["out_s"] for r in rs)),
                      "읽기토큰_평균": round(sum(r["prompt_tokens"] or 0 for r in rs) / len(rs)),
                      "쓰기토큰_평균": round(sum(r["out_tokens"] or 0 for r in rs) / len(rs)),
                      "쓰기속도_토큰/초": round(sum(r["out_tokens"] or 0 for r in rs) / max(sum(r["out_s"] for r in rs), 1e-9), 1),
                      "읽기속도_토큰/초": round(sum(r["prompt_tokens"] or 0 for r in rs) / max(sum(r["prompt_s"] for r in rs), 1e-9), 1)}
    print("\n[시간 분석]", json.dumps(agg, ensure_ascii=False, indent=1))
    print("[기타] 전체", round(secs), "초 중 모델 호출 외", round(secs - sum(r["wall"] for r in rows)), "초")
    out = config.PROJECT_DIR / "data" / "phase_b" / f"hard_check_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({"produced": len(qs), "seconds": secs, "timed_out": rep.timed_out,
                               "discarded": dict(rep.discarded), "errors": rep.gen.errors, "timings": rows,
                               "agg": agg, "kinds": dict(Counter(r["kind"] for r in rows))},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print("저장:", out)


if __name__ == "__main__":
    main()
