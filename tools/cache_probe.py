"""속도 진단: 생성 직후 검증에서 자료(프롬프트 캐시)를 다시 읽는 원인 찾기.
같은 청크로 '생성 → 검증'을 하되 검증 요청의 설정을 하나씩 바꿔, 검증의 읽기 시간을 비교한다.
변형 사이에는 다른 청크를 한 번 읽혀 캐시를 비운다.

사용법: python tools/cache_probe.py
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


def chat(messages, fmt, num_predict, temperature):
    t = time.time()
    r = client().chat(model=config.MODEL, messages=messages, format=fmt, think=False, keep_alive=config.KEEP_ALIVE,
                      options={"num_ctx": config.NUM_CTX, "temperature": temperature, "num_predict": num_predict})
    return r, time.time() - t


def line(tag, r, w):
    print(f"  {tag:<28} 전체 {w:5.1f}s | 읽기 {r.prompt_eval_count:>5}토큰 {r.prompt_eval_duration / NS:5.1f}s"
          f" | 쓰기 {r.eval_count:>4}토큰 {r.eval_duration / NS:5.1f}s", flush=True)
    return r.prompt_eval_duration / NS


def main() -> None:
    chunks = analyze_pdf(PDF)["chunks"]
    c, other = chunks[3], chunks[6]
    vfmt = VerifyResult.model_json_schema()

    def flush():
        chat(prompts.build_verify_messages(other["text"], "캐시 비우기", "1) a\n2) b\n3) c\n4) d"), vfmt, 5, 0)

    def gen(num_predict=1100):
        gm = prompts.build_gen_messages(c["text"], "중", 2, _existing_block([]))
        r, w = chat(gm, batch_schema(2), num_predict, 0.5)
        line("생성", r, w)
        q = GeneratedBatch.model_validate_json(r.message.content).questions[0]
        return prompts.build_verify_messages(c["text"], q.question, format_choices(q.choices))

    variants = [
        ("기본 (앱과 같음)", dict(fmt=vfmt, num_predict=300, temperature=0)),
        ("검증 temperature=0.5", dict(fmt=vfmt, num_predict=300, temperature=0.5)),
        ("검증 format 없음", dict(fmt=None, num_predict=300, temperature=0)),
        ("검증 num_predict=1100", dict(fmt=vfmt, num_predict=1100, temperature=0)),
    ]
    results = {}
    for name, kw in variants:
        print(f"\n[{name}]")
        flush()
        vm = gen()
        r, w = chat(vm, kw["fmt"], kw["num_predict"], kw["temperature"])
        s1 = line("검증 1 (생성 직후)", r, w)
        r, w = chat(vm, kw["fmt"], kw["num_predict"], kw["temperature"])
        s2 = line("검증 2 (검증 다음)", r, w)
        results[name] = (round(s1, 1), round(s2, 1))
    print("\n[요약] 검증 읽기 시간 (생성 직후, 검증 다음):")
    for k, v in results.items():
        print(f"  {k:<28} {v}")


if __name__ == "__main__":
    main()
