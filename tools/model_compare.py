"""모델 비교: 같은 PDF·같은 조건으로 모델마다 하/중/상 N문제씩 세 회차를 만들어 속도·품질 지표를 잰다.

사용법: python tools/model_compare.py <PDF> gemma4:12b qwen3.8:27b [--num 5]
- Mac 메모리(32GB) 때문에 한 번에 한 모델만 올린다: 다른 모델을 내리고 → 올리고 → 예열 → 측정.
- 예열: 모델을 새로 올린 직후 몇 분간 읽기가 느린 구간이 있어(실측), 측정 전에 같은 크기의 요청을 몇 번 보낸다.
- 회차 사이 중복 검사는 모델마다 새 시험용 이력으로 한다 (사용자 실제 이력은 건드리지 않음).
결과: data/phase_b/model_compare_<시각>.json
"""
import argparse
import json
import random
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from quizen import config, verify  # noqa: E402
from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.history import History  # noqa: E402
from quizen.llm import _local_tokenizer, client  # noqa: E402
from quizen.pipeline import build_quiz  # noqa: E402

H = config.OLLAMA_HOST


def ps() -> list[dict]:
    return httpx.get(f"{H}/api/ps", timeout=10).json().get("models", [])


def unload_all() -> None:
    for m in ps():
        httpx.post(f"{H}/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=60)
    for _ in range(30):
        if not ps():
            return
        time.sleep(1)


def swapouts() -> int:
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", "macstudio", "vm_stat"], capture_output=True, text=True).stdout
    line = next(l for l in out.splitlines() if l.startswith("Swapouts"))
    return int(line.split()[-1].rstrip("."))


def warm_up(text: str) -> list[float]:
    """같은 크기(약 2,500토큰)의 서로 다른 글을 읽혀 '느린 구간'을 지나게 한다. 읽기 시간 기록."""
    times = []
    for i in range(4):
        r = client().generate(model=config.MODEL, prompt=f"[{i}] " + text, raw=True, keep_alive=config.KEEP_ALIVE,
                              options={"num_ctx": config.NUM_CTX, "num_predict": 1})
        times.append(round((r.prompt_eval_duration or 0) / 1e9, 1))
    return times


def run_model(model: str, ext: dict, num: int) -> dict:
    config.MODEL = model
    if _local_tokenizer(model) is None:
        print(f"  ! {model} 토크나이저 파일이 없어 토큰 수를 모델에 직접 묻습니다 (느림)")
    print(f"\n######## {model} ########", flush=True)
    unload_all()
    t = time.time()
    client().generate(model=model, prompt="hi", raw=True, keep_alive=config.KEEP_ALIVE,
                      options={"num_ctx": config.NUM_CTX, "num_predict": 1})
    load_s = time.time() - t
    loaded = ps()
    print(f"로딩 {load_s:.1f}s, 메모리 {loaded[0]['size'] / 1e9:.1f}GB ctx={loaded[0].get('context_length')}")
    warm = warm_up(ext["chunks"][3]["text"])
    print(f"예열 읽기시간 {warm}", flush=True)

    hist = History(f"test-compare-{model}-{ext['sha256']}")
    hist.path.unlink(missing_ok=True)
    s0 = swapouts()
    rng = random.Random(11)          # 두 모델이 같은 구간 추첨 순서를 쓰도록 고정
    sets = []
    try:
        for diff in ("하", "중", "상"):
            verify.TIMINGS.clear()
            t = time.time()
            qs, rep = build_quiz(ext, num, diff, verifier=verify.verify_question, history=hist, rng=rng)
            secs = time.time() - t
            hist.record(qs, rep.chunks_used)
            vt = list(verify.TIMINGS)
            s = {"difficulty": diff, "produced": len(qs), "seconds": round(secs, 1),
                 "per_question": round(secs / max(len(qs), 1), 1),
                 "gen_calls": rep.gen.calls, "gen_seconds": round(rep.gen.seconds, 1),
                 "verify_calls": len(vt), "verify_seconds": round(rep.verify_seconds, 1),
                 "gen_prompt_s": [t["prompt_s"] for t in rep.gen.timings],
                 "gen_out_tok_s": [round(t["out_tokens"] / t["out_s"], 1) for t in rep.gen.timings if t["out_s"]],
                 "verify_prompt_s": [t["prompt_s"] for t in vt],
                 "discarded": dict(rep.discarded), "discard_samples": rep.discard_samples,
                 "parse_errors": rep.gen.errors, "fallback_chunks": rep.fallback_chunks,
                 "chunks": [f"{c['page_start']}-{c['page_end']}" for c in rep.chunks_used],
                 "questions": [q.to_dict() for q in qs]}
            sets.append(s)
            print(f"[{diff}] {len(qs)}/{num} {secs:.0f}s (문제당 {s['per_question']}s) 생성 {rep.gen.calls}회 "
                  f"{s['gen_seconds']}s / 검증 {len(vt)}회 {s['verify_seconds']}s | 폐기 {s['discarded'] or '없음'}",
                  flush=True)
    finally:
        hist.path.unlink(missing_ok=True)
    after = ps()
    return {"model": model, "load_seconds": round(load_s, 1), "warmup_prompt_s": warm,
            "memory_gb_loaded": round(loaded[0]["size"] / 1e9, 2),
            "memory_gb_after": round(after[0]["size"] / 1e9, 2) if after else None,
            "swapouts_delta": swapouts() - s0, "sets": sets,
            "total_questions": sum(s["produced"] for s in sets),
            "avg_per_question": round(sum(s["seconds"] for s in sets) / max(sum(s["produced"] for s in sets), 1), 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("models", nargs="+")
    ap.add_argument("--num", type=int, default=5)
    a = ap.parse_args()
    ext = analyze_pdf(Path(a.pdf))
    results = [run_model(m, ext, a.num) for m in a.models]
    for r in results:
        print(f"\n== {r['model']}: 평균 {r['avg_per_question']}s/문제, {r['total_questions']}문제, "
              f"메모리 {r['memory_gb_loaded']}→{r['memory_gb_after']}GB, 스왑아웃 +{r['swapouts_delta']}")
    out = config.DATA_DIR / "phase_b" / f"model_compare_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장: {out}")


if __name__ == "__main__":
    main()
