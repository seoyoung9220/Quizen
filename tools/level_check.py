"""난이도 기준 점검: 새 DB에 PDF를 등록하고, 한 난이도로 문제를 만들어 문제·보기·정답·근거를 전부 보여 준다.
사람이 읽고 '정말 그 난이도인지' 판단하기 위한 도구다. 실제 기록을 건드리지 않도록 빈 QUIZEN_DATA_DIR에서 실행한다.

사용법: QUIZEN_DATA_DIR=<빈 폴더> python tools/level_check.py <PDF> <난이도> [--mcq 5] [--short 0] [--essay 0]
"""
import argparse
import dataclasses
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from quizen import config, verify  # noqa: E402
from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.llm import use_only  # noqa: E402
from quizen.pipeline import build_mixed  # noqa: E402
from quizen.store import Store  # noqa: E402


def as_dict(q) -> dict:
    d = dataclasses.asdict(q) if dataclasses.is_dataclass(q) else dict(vars(q))
    return {k: v for k, v in d.items() if not k.startswith("_")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("difficulty", choices=["하", "중", "상"])
    ap.add_argument("--mcq", type=int, default=5)
    ap.add_argument("--short", type=int, default=0)
    ap.add_argument("--essay", type=int, default=0)
    a = ap.parse_args()
    counts = {"mcq": a.mcq, "short": a.short, "essay": a.essay}

    s = Store()
    p = Path(a.pdf)
    d = s.add_document(p, p.name, analyze_pdf(p)["pages"])
    use_only(config.MODEL)
    t = time.time()
    qs, rep = build_mixed(s.extracted([d]), counts, a.difficulty, verifier=verify.verify_question,
                          history=s.history([d]), rng=random.Random(),
                          progress=lambda st, i, n, m: print(f"  [{st}] {m}", flush=True))
    secs = time.time() - t
    print(f"\n[{a.difficulty}] {len(qs)}/{sum(counts.values())}문제 · {secs:.0f}초 "
          f"(문제당 {secs / max(len(qs), 1):.0f}초) · 시간 상한 {rep.timed_out}")
    print(f"폐기 {dict(rep.discarded)} · JSON 실패 {rep.gen.parse_failures}/{rep.gen.calls}")
    for e in rep.gen.errors:
        print("  오류:", e[:160])
    for x in rep.discard_samples:
        print("  폐기:", x[:200])
    items = [as_dict(q) for q in qs]
    for i, q in enumerate(items, 1):
        print(f"\n--- {i}. ({q.get('qtype', 'mcq')}) p{q.get('page')}")
        print(q["question"])
        for j, c in enumerate(q.get("choices") or []):
            print(f"   {'★' if j == q.get('answer_index') else ' '} {j + 1}) {c}")
        if q.get("qtype") == "short":
            print("   정답:", q.get("answer_text"), q.get("accepted") or "")
        if q.get("qtype") == "essay":
            print("   모범 답안:", q.get("answer_text"))
            for r in q.get("rubric") or []:
                print("   채점 기준:", r.get("point"))
        print("   근거:", q["evidence"])
    out = config.PROJECT_DIR / "data" / "phase_b" / f"level_check_{a.difficulty}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({"difficulty": a.difficulty, "counts": counts, "produced": len(qs), "seconds": secs,
                               "timed_out": rep.timed_out, "discarded": dict(rep.discarded),
                               "discard_samples": rep.discard_samples, "errors": rep.gen.errors,
                               "parse_failures": rep.gen.parse_failures, "calls": rep.gen.calls,
                               "questions": items}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("\n저장:", out)


if __name__ == "__main__":
    main()
