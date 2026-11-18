"""Phase B 완료 기준 확인: 하/중/상 각 N문제 생성 → 풀이 → 채점 → 오답 풀이, 시간·메모리 실측.

사용법: python tools/phase_b_check.py <PDF> [--num 5]
- 각 세트에서 일부러 2문제를 틀리게 답해 오답 풀이까지 확인한다.
- Mac Studio의 모델 메모리(/api/ps)를 5초마다 기록해 최대값을 낸다.
- 결과는 data/phase_b/<시각>.json 에 저장한다.
"""
import argparse
import json
import sys
import threading
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from quizen import config  # noqa: E402
from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.explain import explain_stream  # noqa: E402
from quizen.pipeline import build_quiz  # noqa: E402
from quizen.quiz import QuizSession  # noqa: E402
from quizen.validate import ChunkIndex, check_evidence, normalize  # noqa: E402
from quizen.verify import verify_question  # noqa: E402


class MemSampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.samples: list[tuple[float, float, float]] = []   # (시각, size GB, size_vram GB)
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                for m in httpx.get(f"{config.OLLAMA_HOST}/api/ps", timeout=5).json().get("models", []):
                    if m["name"] == config.MODEL:
                        self.samples.append((time.time(), m["size"] / 1e9, m.get("size_vram", 0) / 1e9))
            except Exception:  # noqa: BLE001
                pass
            self.stop.wait(5)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--num", type=int, default=5)
    a = ap.parse_args()

    mem = MemSampler()
    mem.start()
    t0 = time.time()
    extracted = analyze_pdf(Path(a.pdf))
    analyze_s = time.time() - t0
    chunks = {c["chunk_id"]: c["text"] for c in extracted["chunks"]}
    print(f"PDF 분석 {analyze_s:.1f}s ({extracted['page_count']}페이지, 청크 {len(chunks)}개)\n")

    report = {"pdf": extracted["source_document"], "model": config.MODEL, "analyze_seconds": analyze_s, "sets": []}
    all_positions = Counter()
    for diff in ("하", "중", "상"):
        print(f"===== 난이도 {diff} =====", flush=True)
        t = time.time()
        qs, rep = build_quiz(extracted, a.num, diff, verifier=verify_question,
                             progress=lambda s, i, n, m: print(f"  [{s}] {m}", flush=True))
        gen_s = time.time() - t

        # 코드로 재확인: (1) evidence가 청크 원문에 있음 (2) 코드가 정한 페이지가 같음
        # (3) evidence의 앞부분 20자가 그 페이지 원문(이웃 페이지 포함)에 실제로 있음
        ev_ok = []
        page_norm = {p["page"]: normalize(p["text"]) for p in extracted["pages"]}
        for q in qs:
            chk = check_evidence(q.evidence, ChunkIndex(chunks[q.chunk_id]))
            head = normalize(q.evidence)[:20]
            near = page_norm.get(q.page, "") + page_norm.get(q.page + 1, "")
            ev_ok.append(chk.ok and chk.page == q.page and head in near)

        sess = QuizSession(qs)
        keys = {p.qid: sess._keys[p.qid].answer_index for p in sess.public}
        # 섞은 뒤 정답 번호의 보기 = 원래 정답 보기?
        idx_ok = [p.choices[keys[p.qid]] == q.choices[q.answer_index] for p, q in zip(sess.public, qs)]
        moved = sum(list(p.choices) != q.choices for p, q in zip(sess.public, qs))
        positions = Counter(keys.values())
        all_positions.update(positions)

        # 앞의 (n-2)개는 맞히고 마지막 2개는 일부러 틀린 답
        answers = {}
        for i, p in enumerate(sess.public):
            answers[p.qid] = keys[p.qid] if i < len(qs) - 2 else (keys[p.qid] + 1) % 4
        g = sess.grade(answers)

        explanations = []
        for r in g.results:
            if r.correct:
                continue
            te = time.time()
            text = "".join(explain_stream(r))
            explanations.append({"number": r.number, "seconds": round(time.time() - te, 1),
                                 "question": r.question, "evidence": r.evidence, "text": text})

        s = {
            "difficulty": diff, "requested": rep.requested, "produced": rep.produced,
            "seconds": round(gen_s, 1), "per_question": round(gen_s / max(rep.produced, 1), 1),
            "gen_seconds": round(rep.gen.seconds, 1), "verify_seconds": round(rep.verify_seconds, 1),
            "model_calls": rep.gen.calls, "regen_rounds": rep.regen_rounds_used,
            "discarded": dict(rep.discarded), "discard_samples": rep.discard_samples,
            "parse_errors": rep.gen.errors,
            "evidence_ok": ev_ok, "answer_index_ok": idx_ok, "choices_moved": moved,
            "answer_positions": dict(sorted(positions.items())),
            "grade": {"total": g.total, "correct": g.correct, "accuracy": g.accuracy},
            "questions": [q.to_dict() for q in qs],
            "explanations": explanations,
        }
        report["sets"].append(s)
        print(f"  → {rep.produced}/{rep.requested}문제, {gen_s:.0f}s (문제당 {s['per_question']}s, "
              f"생성 {s['gen_seconds']}s / 검증 {s['verify_seconds']}s), 폐기 {dict(rep.discarded) or '없음'}")
        print(f"  → evidence {sum(ev_ok)}/{len(ev_ok)}, 정답 인덱스 {sum(idx_ok)}/{len(idx_ok)}, "
              f"보기 순서 바뀜 {moved}/{len(qs)}, 정답 위치 {dict(sorted(positions.items()))}")
        print(f"  → 채점 {g.correct}/{g.total} ({g.accuracy:.0%}), 오답 풀이 "
              f"{[e['seconds'] for e in explanations]}s\n", flush=True)

    mem.stop.set()
    total_q = sum(s["produced"] for s in report["sets"])
    total_s = sum(s["seconds"] for s in report["sets"])
    report["avg_seconds_per_question"] = round(total_s / max(total_q, 1), 1)
    report["answer_positions_all"] = dict(sorted(all_positions.items()))
    if mem.samples:
        report["memory_gb_max"] = round(max(x[1] for x in mem.samples), 2)
        report["memory_gb_min"] = round(min(x[1] for x in mem.samples), 2)
        report["gpu_ratio_min"] = round(min(x[2] / x[1] for x in mem.samples if x[1]), 3)
        report["memory_samples"] = len(mem.samples)
    out = config.DATA_DIR / "phase_b" / f"{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"전체 {total_q}문제, 문제당 평균 {report['avg_seconds_per_question']}s, 정답 위치 합계 {report['answer_positions_all']}")
    print(f"Mac 모델 메모리 {report.get('memory_gb_min')}~{report.get('memory_gb_max')}GB, "
          f"GPU 비율 최소 {report.get('gpu_ratio_min')} (샘플 {report.get('memory_samples')}개)")
    print(f"저장: {out}")


if __name__ == "__main__":
    main()
