"""폐기 → 재생성 흐름 테스트. 모델 대신 미리 정한 답을 돌려주는 가짜 생성기를 쓴다.
실행: python tests/test_pipeline.py
"""
import os
os.environ["QUIZEN_OFFLINE"] = "1"   # 테스트는 절대 모델(Mac Studio)을 부르지 않는다
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import quizen.pipeline as pl  # noqa: E402
from quizen.schemas import GeneratedQuestion  # noqa: E402

CHUNK = "[[PAGE 3]]\n• 학습률은 하강하는 스텝의 크기를 결정\n[[PAGE 4]]\n에포크는 훈련셋을 한번 반복하는 것"
EXTRACTED = {
    "source_document": "sample.pdf",
    "pages": [{"page": 3, "text": "• 학습률은 하강하는 스텝의 크기를 결정"},
              {"page": 4, "text": "에포크는 훈련셋을 한번 반복하는 것"}],
    "chunks": [{"chunk_id": 1, "page_start": 3, "page_end": 4, "new_page_start": 3, "text": CHUNK}],
}


def q(text, evidence, page_claim=None):
    # 보기는 문제마다 다르게 (같으면 중복 검사의 '보기 구성 유사'에 걸림)
    return GeneratedQuestion(question=text, choices=[f"{text}-{c}" for c in "가나다라"], answer_index=1,
                             difficulty="중", evidence=evidence)


def run(script, **kw_build):
    """script: 호출 순서대로 돌려줄 문제 리스트들. 호출 기록을 함께 돌려준다."""
    calls = []

    def fake(chunk_text, difficulty, num, existing, stats, model=None, conditions="", **kw):
        calls.append({"num": num, "existing": list(existing)})
        stats.calls += 1
        return script[len(calls) - 1] if len(calls) <= len(script) else []

    orig = pl.generate_for_chunk
    pl.generate_for_chunk = fake
    try:
        qs, rep = pl.build_quiz(EXTRACTED, 2, "중", **kw_build)
    finally:
        pl.generate_for_chunk = orig
    return qs, rep, calls


def test_regenerates_discarded():
    qs, rep, calls = run([
        [q("Q1", "학습률은 하강하는 스텝의 크기를 결정"), q("Q2", "학습률은 모델 층 수를 결정")],  # Q2 환각
        [q("Q3", "에포크는 훈련셋을 한번 반복하는 것")],
    ])
    assert [x.question for x in qs] == ["Q1", "Q3"], qs
    assert [c["num"] for c in calls] == [2, 1], calls            # 부족한 1개만 다시 요청
    assert calls[1]["existing"] == ["Q2", "Q1"], calls           # 버린 문제·통과한 문제 모두 중복 방지 목록으로 전달
    assert rep.discarded["evidence 원문 불일치"] == 1 and rep.regen_rounds_used == 1
    assert qs[0].page == 3 and qs[1].page == 4                   # 페이지는 코드가 결정
    assert all(x.source_document == "sample.pdf" for x in qs)    # 파일명도 코드가 결정


def test_gives_up_after_two_empty_rounds():
    bad = [q("X", "원문에 없는 문장입니다 정말로")]
    qs, rep, calls = run([bad, bad, bad, bad])
    assert len(calls) == 2, calls                                # 두 번 연속 하나도 못 건지면 3번째는 안 함
    assert qs == [] and rep.produced == 0 and rep.requested == 2
    assert calls[1]["existing"] == ["X"], calls                  # 버린 문제는 다음 요청에 '이미 출제'로 알려 줌


def test_keeps_regenerating_while_progress():
    """한 문제씩이라도 건지면 재생성 2회까지 계속한다."""
    ok1 = [q("Q1", "학습률은 하강하는 스텝의 크기를 결정"), q("X", "원문에 없는 문장입니다 정말로")]
    qs, rep, calls = run([ok1, [q("Y", "원문에 없는 문장 또 하나입니다")], [q("Q3", "에포크는 훈련셋을 한번 반복하는 것")]])
    assert len(calls) == 3 and [x.question for x in qs] == ["Q1", "Q3"], (calls, qs)
    assert calls[2]["existing"] == ["X", "Y", "Q1"], calls


def test_time_budget_stops_generation():
    """시간 상한을 넘으면 새 생성 요청을 보내지 않고, 그때까지 통과한 문제로 끝낸다."""
    import time as _t
    first = [q("Q1", "학습률은 하강하는 스텝의 크기를 결정")]
    orig = pl.generate_for_chunk

    def slow(*a, **k):                       # 첫 호출이 상한(0.05초)을 다 써 버림
        _t.sleep(0.1)
        a[4].calls += 1
        return first
    pl.generate_for_chunk = slow
    try:
        qs, rep = pl.build_quiz(EXTRACTED, 2, "중", time_budget=0.05)
    finally:
        pl.generate_for_chunk = orig
    assert [x.question for x in qs] == ["Q1"] and rep.timed_out and rep.gen.calls == 1, (qs, rep.gen.calls)
    qs, rep, calls = run([first, [q("Q3", "에포크는 훈련셋을 한번 반복하는 것")]])
    assert len(qs) == 2 and not rep.timed_out                   # 기본 상한(문제당 120초)에서는 영향 없음


def test_hard_gets_longer_output():
    from quizen.generate import num_predict_for, output_budget
    assert num_predict_for("mcq", 5, "상") > num_predict_for("mcq", 5, "중") == 2300
    assert output_budget("mcq", 2, "상", 4000) == num_predict_for("mcq", 2, "상") == 1900     # 자리 넉넉: 2배 그대로
    b = output_budget("mcq", 6, "상", 4000)                       # 자료+지시 약 4천 토큰: 남은 자리까지만
    assert b + 4000 <= 8192 and b >= num_predict_for("mcq", 6, "상", factor=1.5), b
    assert output_budget("mcq", 6, "상", 6000) == num_predict_for("mcq", 6, "상", factor=1.5)  # 자리 부족: 예전 값 보장
    assert output_budget("mcq", 3, "중", 4000) == num_predict_for("mcq", 3, "중")              # '중'은 그대로


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
