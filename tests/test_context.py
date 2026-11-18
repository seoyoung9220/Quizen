"""컨텍스트 넘침 방지와 부족분 보충 테스트 (모델 호출 없음). 실행: python tests/test_context.py"""
import os
os.environ["QUIZEN_OFFLINE"] = "1"   # 테스트는 절대 모델(Mac Studio)을 부르지 않는다
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import quizen.pipeline as pl  # noqa: E402
from quizen import config  # noqa: E402
from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.explain import build_explain_messages, page_context  # noqa: E402
from quizen.generate import EXISTING_MAX, MAX_PER_CHUNK, build_fitting_messages  # noqa: E402
from quizen.llm import ContextOverflow, fit_context, prompt_tokens  # noqa: E402
from quizen.prompts import CONDITIONS_MAX, build_verify_messages  # noqa: E402
from quizen.quiz import QuestionResult  # noqa: E402
from quizen.schemas import GeneratedQuestion  # noqa: E402

EXT = analyze_pdf(ROOT / "samples" / "sample.pdf")
LONG_Q = "학습률과 경사 하강법의 수렴 특성에 대한 설명으로 옳은 것을 고르시오 " * 2


def test_worst_case_generation_fits():
    """가장 큰 청크 + 최대 문제 수 + 이미 출제된 문제 최대 + 조건 최대 길이도 8,192 안에 들어간다."""
    big = max(EXT["chunks"], key=lambda c: c["tokens"])
    num_predict = 400 * MAX_PER_CHUNK + 300
    msgs = build_fitting_messages(big["text"], "상", MAX_PER_CHUNK, [LONG_Q] * EXISTING_MAX,
                                  "꿁" * CONDITIONS_MAX, num_predict)
    n = prompt_tokens(msgs)
    assert n + num_predict <= config.NUM_CTX, (n, num_predict)
    assert LONG_Q.strip() in msgs[1]["content"]            # 이 정도면 목록을 줄이지 않아도 들어감
    print(f"    최악의 경우 {n} + {num_predict} = {n + num_predict} / {config.NUM_CTX}")


def test_existing_list_trimmed_when_needed():
    """이미 출제된 문제 목록이 길면 오래된 것부터 줄여서 맞춘다."""
    big = max(EXT["chunks"], key=lambda c: c["tokens"])
    old = config.NUM_CTX
    # 출력 1500토큰 + 이미 출제된 문제 몇 개만 들어갈 여유(약 400토큰)
    config.NUM_CTX = prompt_tokens(build_fitting_messages(big["text"], "중", 3, [], "", 0)) + 1500 + 400
    try:
        msgs = build_fitting_messages(big["text"], "중", 3, [f"{i}번 {LONG_Q}" for i in range(EXISTING_MAX)], "", 1500)
        user = msgs[1]["content"]
        assert f"{EXISTING_MAX - 1}번" in user and "0번 " not in user      # 최근 것은 남고 오래된 것부터 빠짐
    finally:
        config.NUM_CTX = old


def test_overflow_raises():
    old = config.NUM_CTX
    config.NUM_CTX = 500
    try:
        try:
            fit_context(build_verify_messages(EXT["chunks"][0]["text"], "Q", "0. a"), 300)
            raise AssertionError("ContextOverflow가 나지 않음")
        except ContextOverflow:
            pass
    finally:
        config.NUM_CTX = old


def test_explain_worst_case_fits():
    pages = {p["page"]: p["text"] for p in EXT["pages"]}
    r = QuestionResult(number=1, question=LONG_Q, choices=tuple([LONG_Q] * 4), difficulty="상", user_index=0,
                       answer_index=1, correct=False, evidence=LONG_Q * 3, page=34, source_document="x.pdf",
                       chunk_id=6, evidence_pages=tuple(range(30, 40)))
    fit_context(build_explain_messages(r, page_context(pages, r)), 800)


def test_use_only_unloads_other_models():
    """모델을 바꿀 때 다른 모델만 내리고(keep_alive=0), 쓸 모델은 건드리지 않는다. (가짜 Mac 응답)"""
    import types
    import quizen.llm as llm
    calls = []

    class FakeClient:
        def ps(self):
            return types.SimpleNamespace(models=[types.SimpleNamespace(model="qwen3.8:27b"),
                                                 types.SimpleNamespace(model="gemma4:12b")])

        def generate(self, **kw):
            calls.append(kw)

    orig = llm.client
    llm.client = lambda: FakeClient()
    try:
        assert llm.use_only("gemma4:12b") == ["qwen3.8:27b"]
        assert calls == [{"model": "qwen3.8:27b", "prompt": "", "keep_alive": 0}], calls
        calls.clear()
        assert llm.use_only("gemma4:12b") == ["qwen3.8:27b"] and len(calls) == 1
    finally:
        llm.client = orig


def test_fallback_fills_shortage_from_other_chunk():
    """한 구간에서 재생성까지 모두 실패하면 다른 구간에서 부족분을 채운다."""
    chunks = EXT["chunks"][2:4]
    ext = {**EXT, "chunks": chunks}
    # rng 없이 1문제를 요청하면 가중치가 큰 구간이 배정된다 → 그 구간을 실패시킨다
    first = max(chunks, key=lambda c: pl.chunk_weights([c], EXT["pages"])[0])
    other = chunks[1] if first is chunks[0] else chunks[0]
    calls = []

    def fake(chunk_text, difficulty, num, existing, stats, model=None, conditions="", **kw):
        calls.append(chunk_text[:20])
        if chunk_text == first["text"]:          # 배정된 구간: 항상 원문에 없는 근거 → 폐기
            return [GeneratedQuestion(question=f"Q{len(calls)} 지어낸 문제", choices=["a", "b", "c", "d"],
                                      answer_index=0, difficulty="중", evidence="원문에 전혀 없는 문장입니다 정말로")]
        # 보충 구간: 그 구간 원문에 실제로 있는 문장을 근거로 쓴다
        line = next(l for l in other["text"].splitlines() if len(l) > 25 and "[[PAGE" not in l)
        return [GeneratedQuestion(question="보충 구간에서 만든 문제는?", choices=["한 개", "전체", "미니배치", "검증셋"],
                                  answer_index=0, difficulty="중", evidence=line)]

    orig = pl.generate_for_chunk
    pl.generate_for_chunk = fake
    try:
        # 1문제를 요청하면 정보량이 큰 첫 구간만 배정됨 → 2번 연속 실패 → 둘째 구간에서 보충
        qs, rep = pl.build_quiz(ext, 1, "중", rng=None)
    finally:
        pl.generate_for_chunk = orig
    assert rep.fallback_chunks == 1 and len(qs) == 1, (rep.fallback_chunks, qs, calls)
    assert qs[0].chunk_id == other["chunk_id"]
    assert len(calls) == 3, calls                     # 배정 구간 2번(연속 실패로 조기 포기) + 보충 1번


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 통과")
    sys.exit(1 if failed else 0)
