"""속도 진단 3: 자료를 '별도 대화 턴'으로 두면 생성 → 검증이 자료를 한 번만 읽는지.
  배치 A(현재): [system, user(자료 + 역할 지시 + 요청)]
  배치 B(시험): [system, user(자료), assistant(확인), user(역할 지시 + 요청)]
청크 앞에 실험 번호를 붙여 매번 '처음 보는 자료'로 만든다.
사용법: python tools/cache_probe3.py
"""
import sys
import time
import uuid
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
    print(f"  {tag:<6} 전체 {w:5.1f}s | 읽기 {r.prompt_eval_count:>5}토큰 {r.prompt_eval_duration / NS:5.1f}s"
          f" | 쓰기 {r.eval_count:>4}토큰 {r.eval_duration / NS:5.1f}s", flush=True)
    return w


def layout_b(msgs: list[dict]) -> list[dict]:
    """배치 A 메시지를 배치 B로: user 내용을 '자료'와 '나머지'로 나눈다."""
    user = msgs[1]["content"]
    k = user.index("</자료>") + len("</자료>")
    return [msgs[0], {"role": "user", "content": user[:k]},
            {"role": "assistant", "content": "자료를 확인했습니다."},
            {"role": "user", "content": user[k:].strip()}]


def run(text: str, b: bool) -> float:
    f = layout_b if b else (lambda m: m)
    total = 0.0
    r, w = chat(f(prompts.build_gen_messages(text, "중", 2, _existing_block([]))), batch_schema(2), 1100, 0.5)
    total += line("생성", r, w)
    for i, q in enumerate(GeneratedBatch.model_validate_json(r.message.content).questions[:2], 1):
        r, w = chat(f(prompts.build_verify_messages(text, q.question, format_choices(q.choices))), VFMT, 300, 0)
        total += line(f"검증{i}", r, w)
    return total


def main() -> None:
    chunks = analyze_pdf(PDF)["chunks"]
    res = {}
    for name, b, c in (("배치 B", True, chunks[2]), ("배치 A", False, chunks[4]),
                       ("배치 B", True, chunks[5]), ("배치 A", False, chunks[1])):
        text = f"(실험 {uuid.uuid4().hex[:8]})\n" + c["text"]
        print(f"\n[{name}] 청크 {c['chunk_id']}")
        res.setdefault(name, []).append(round(run(text, b)))
    print("\n[요약] 청크당 전체 시간:", res)


if __name__ == "__main__":
    main()
