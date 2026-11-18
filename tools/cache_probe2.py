"""속도 진단 2: '예열' 요청(출력 1토큰짜리 검증 형식 요청)을 먼저 보내면 생성·검증이 자료를 다시 읽지 않는지.
처음 보는 청크로만 비교한다 (이미 본 청크는 캐시가 남아 있어 비교가 안 됨).
  대조군: 생성 → 검증1 → 검증2
  예열군: 예열 → 생성 → 검증1 → 검증2
사용법: python tools/cache_probe2.py <예열 청크 번호들> -- <대조 청크 번호들>   예) 1 5 -- 0 7
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from quizen import config, prompts  # noqa: E402
from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.generate import _existing_block  # noqa: E402
from quizen.llm import client  # noqa: E402
from quizen.schemas import GeneratedBatch, VerifyResult, batch_schema  # noqa: E402
from quizen.verify import format_choices  # noqa: E402

PDF = Path("samples/sample.pdf")
NS = 1e9
VFMT = VerifyResult.model_json_schema()


def chat(messages, fmt, num_predict, temperature):
    t = time.time()
    r = client().chat(model=config.MODEL, messages=messages, format=fmt, think=False, keep_alive=config.KEEP_ALIVE,
                      options={"num_ctx": config.NUM_CTX, "temperature": temperature, "num_predict": num_predict})
    return r, time.time() - t


def line(tag, r, w):
    print(f"  {tag:<10} 전체 {w:5.1f}s | 읽기 {r.prompt_eval_count:>5}토큰 {r.prompt_eval_duration / NS:5.1f}s"
          f" | 쓰기 {r.eval_count:>4}토큰 {r.eval_duration / NS:5.1f}s", flush=True)
    return w


def run(c, warm: bool) -> float:
    total = 0.0
    if warm:
        # 예열 2: 요청이 자료 바로 뒤에서 끝난다 (생성·검증 모두 여기서 갈라짐)
        wm = [{"role": "system", "content": prompts.SHARED_SYSTEM},
              {"role": "user", "content": prompts.MATERIAL.format(pdf_chunk=c["text"])}]
        r, w = chat(wm, None, 1, 0)
        total += line("예열", r, w)
    gm = prompts.build_gen_messages(c["text"], "중", 2, _existing_block([]))
    r, w = chat(gm, batch_schema(2), 1100, 0.5)
    total += line("생성", r, w)
    qs = GeneratedBatch.model_validate_json(r.message.content).questions
    for i, q in enumerate(qs[:2], 1):
        r, w = chat(prompts.build_verify_messages(c["text"], q.question, format_choices(q.choices)), VFMT, 300, 0)
        total += line(f"검증{i}", r, w)
    return total


def main() -> None:
    args = sys.argv[1:]
    k = args.index("--")
    warm_ids, ctrl_ids = [int(x) for x in args[:k]], [int(x) for x in args[k + 1:]]
    chunks = analyze_pdf(PDF)["chunks"]
    res = {"예열": [], "대조": []}
    for a, b in zip(warm_ids, ctrl_ids):
        print(f"\n[예열] 청크 {a}")
        res["예열"].append(run(chunks[a], True))
        print(f"[대조] 청크 {b}")
        res["대조"].append(run(chunks[b], False))
    print("\n[요약] 청크당 전체 시간(예열 포함):", {k: [round(x) for x in v] for k, v in res.items()})


if __name__ == "__main__":
    main()
