"""진단 1) 생성 직후 검증에서 자료 재사용(프롬프트 캐시)이 되는지 호출별로 측정
     2) 조건 입력창: 같은 청크에서 조건 없음 / 조건 있음 비교

사용법: python tools/diag_cache_and_conditions.py
"""
import json
import statistics
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


def chat(messages, fmt, num_predict, temperature):
    t = time.time()
    r = client().chat(model=config.MODEL, messages=messages, format=fmt, think=False, keep_alive=config.KEEP_ALIVE,
                      options={"num_ctx": config.NUM_CTX, "temperature": temperature, "num_predict": num_predict})
    return r, time.time() - t


def timing(r, wall):
    return (f"전체 {wall:5.1f}s | 읽기 {r.prompt_eval_count:>5}토큰 {r.prompt_eval_duration / NS:5.1f}s"
            f" | 쓰기 {r.eval_count:>4}토큰 {r.eval_duration / NS:5.1f}s | 로딩 {r.load_duration / NS:4.1f}s")


def main() -> None:
    d = analyze_pdf(PDF)
    chunks = d["chunks"]

    print("=== 1) 생성 → 검증 캐시 재사용 (청크 2~6, 하 난이도 1문제씩) ===")
    ratios = []
    for c in chunks[1:6]:
        gm = prompts.build_gen_messages(c["text"], "하", 1, _existing_block([]))
        r, w = chat(gm, batch_schema(1), 700, 0.5)
        print(f"청크 {c['chunk_id']} 생성  " + timing(r, w))
        q = GeneratedBatch.model_validate_json(r.message.content).questions[0]
        vm = prompts.build_verify_messages(c["text"], q.question, format_choices(q.choices))
        r2, w2 = chat(vm, VerifyResult.model_json_schema(), 300, 0)
        print(f"청크 {c['chunk_id']} 검증  " + timing(r2, w2))
        ratios.append(r2.prompt_eval_duration / max(r.prompt_eval_duration, 1))
    print(f"검증 읽기시간 / 생성 읽기시간 비율: {[round(x, 2) for x in ratios]}\n")

    print("=== 2) 조건 입력창 효과 (청크 4, 중 난이도 3문제) ===")
    cond = "실제 상황에 적용하는 예시 상황을 문제에 넣을 것. 보기는 각각 25자 이내로 짧게."
    c = chunks[3]
    for label, cc in (("조건 없음", ""), ("조건 있음", cond)):
        gm = prompts.build_gen_messages(c["text"], "중", 3, _existing_block([]), conditions=cc)
        r, w = chat(gm, batch_schema(3), 1500, 0.5)
        qs = GeneratedBatch.model_validate_json(r.message.content).questions
        lens = [len(ch) for q in qs for ch in q.choices]
        print(f"[{label}] {w:.1f}s, {len(qs)}문제, 보기 평균 {statistics.mean(lens):.1f}자 / 최대 {max(lens)}자,"
              f" 25자 이하 보기 {sum(x <= 25 for x in lens)}/{len(lens)}")
        for q in qs:
            print(f"   - {q.question}")
            for ch in q.choices:
                print(f"       · {ch}")


if __name__ == "__main__":
    main()
