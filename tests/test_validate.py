"""B-4 코드 검증 테스트. 실행: python tests/test_validate.py"""
import os
os.environ["QUIZEN_OFFLINE"] = "1"   # 테스트는 절대 모델(Mac Studio)을 부르지 않는다
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quizen.validate import ChunkIndex, check_evidence  # noqa: E402

EXTRACTED = ROOT / "data" / "extracted" / "sample.json"


def test_page_assignment_basic():
    text = "[[PAGE 3]]\n가나다 라마바\n[[PAGE 4]]\n• 경사 하강법은 반복적으로 파라미터를 조정\n[[PAGE 5]]\n끝"
    idx = ChunkIndex(text)
    r = check_evidence("경사하강법은 반복적으로 파라미터를 조정", idx)
    assert r.ok and r.page == 4 and r.mode == "full", r


def test_whitespace_bullet_quote_ignored():
    idx = ChunkIndex("[[PAGE 1]]\n● 지역 최소값은 없고 '단 하나의' 전역 최소값만 존재")
    r = check_evidence("지역 최소값은 없고 “단 하나의” 전역최소값만 존재", idx)
    assert r.ok and r.page == 1, r


def test_fabricated_rejected():
    idx = ChunkIndex("[[PAGE 1]]\n학습률은 하강하는 스텝의 크기를 결정")
    r = check_evidence("학습률은 모델의 층 수를 결정한다", idx)
    assert not r.ok, r


SLIDE = ("[[PAGE 5]]\n•회귀 모델 훈련은 RMSE를 최소화하는\n모델 파라미터 𝜽를 찾는 과정\n•(문제를 간략하기 위해) RMSE 보다\n"
         "MSE (평균 제곱 오차)를 최소화하는\n모델 파라미터 𝜽를 찾는 과정\n[[PAGE 14]]\n"
         "• 어떤 두 점을 선택해 선을 그어도 곡선위에 위치\n• 지역 최소값은 없고 '단 하나의' 전역 최소값만 존재\n"
         "• 연속된 함수이고 기울기가 갑자기 변하지 않음\n경사 하강법 Gradient (∇) descent\n전역 최소값\n비용\n평지\n"
         "지역 최소값\n모델 파라미터\n(특성)\n[[PAGE 15]]\n경사 하강법의 문제점\n•(임의 초기값 설정에 의해)\n"
         "왼쪽에서 시작하면 학습에\n의해 지역 최소값에 수렴")


def test_pieces_accepts_dropped_math_and_figure_labels():
    """C-9 실측: 수학 기호를 빼먹거나 그림 라벨을 건너뛰고 이어 쓴 근거는 받아 준다 (조각 모드)."""
    idx = ChunkIndex(SLIDE)
    r = check_evidence("(문제를 간략하기 위해) RMSE 보다 MSE (평균 제곱 오차)를 최소화하는 모델 파라미터를 찾는 과정", idx)
    assert r.ok and r.mode == "pieces" and r.page == 5, r
    r = check_evidence("연속된 함수이고 기울기가 갑자기 변하지 않음 경사 하강법의 문제점 (임의 초기값 설정에 의해) "
                       "왼쪽에서 시작하면 학습에 의해 지역 최소값에 수렴", idx)
    assert r.ok and r.mode == "pieces" and r.pages == [14, 15], r


def test_ellipsis_in_evidence():
    """C-9 실측: 모델이 근거를 '...'로 줄여 씀 → 줄인 앞뒤 조각이 각각 원문에 있으면 통과, 지어낸 조각이 있으면 폐기."""
    idx = ChunkIndex(SLIDE)
    r = check_evidence("연속된 함수이고 기울기가 갑자기 변하지 않음...", idx)
    assert r.ok and r.page == 14, r
    r = check_evidence("지역 최소값은 없고 '단 하나의' 전역 최소값만 존재 … 왼쪽에서 시작하면 학습에 의해 지역 최소값에 수렴", idx)
    assert r.ok and r.pages == [14, 15], r
    r = check_evidence("연속된 함수이고 기울기가 갑자기 변하지 않음... 따라서 항상 빠르게 수렴한다", idx)
    assert not r.ok, r


def test_salvage_truncated_output():
    """출력 한도에서 잘린 JSON: 완성된 문제는 살리고 잘린 마지막 문제만 버린다."""
    import json as _j
    from quizen.generate import _salvage
    one = {"question": "Q1", "choices": ["a", "b", "c", "d"], "answer_index": 0, "difficulty": "상",
           "evidence": "학습률은 하강하는 스텝의 크기를 결정"}
    content = '{"questions":[' + _j.dumps(one, ensure_ascii=False) + ', {"question":"Q2 잘린 문'
    got = _salvage(content, "mcq")
    assert [g.question for g in got] == ["Q1"], got


def test_pieces_still_rejects_changes():
    idx = ChunkIndex(SLIDE)
    # 부정어('없고')를 건너뛰면 뜻이 뒤집힘 → 폐기
    r = check_evidence("어떤 두 점을 선택해 선을 그어도 곡선위에 위치 지역 최소값은 단 하나의 전역 최소값만 존재", idx)
    assert not r.ok, r
    # 모델이 덧붙인 말 → 폐기
    r = check_evidence("연속된 함수이고 기울기가 갑자기 변하므로 경사 하강법의 문제점이 생긴다", idx)
    assert not r.ok, r
    # 너무 멀리 건너뜀(60자 초과) → 폐기
    r = check_evidence("회귀 모델 훈련은 RMSE를 최소화하는 왼쪽에서 시작하면 학습에 의해 지역 최소값에 수렴", idx)
    assert not r.ok, r


def test_partial_fabrication_rejected():
    """구간 중 하나라도 원문에 없으면 전체 폐기."""
    idx = ChunkIndex("[[PAGE 1]]\n학습률은 하강하는 스텝의 크기를 결정\n[[PAGE 2]]\n에포크는 훈련셋을 한번 반복하는 것")
    r = check_evidence("학습률은 하강하는 스텝의 크기를 결정 • 에포크는 모델을 저장하는 단위", idx)
    assert not r.ok, r


def test_too_short_rejected():
    idx = ChunkIndex("[[PAGE 1]]\n학습률")
    assert not check_evidence("학습률", idx).ok


def test_contiguous_across_pages():
    """페이지 경계를 넘어 이어지는 문장: 전체 일치, 시작 페이지 7, 걸친 페이지 7·8."""
    idx = ChunkIndex("[[PAGE 7]]\n학습률은 하강하는 스텝의 크기를 결정\n[[PAGE 8]]\n에포크는 훈련셋을 한번 반복하는 것")
    r = check_evidence("학습률은 하강하는 스텝의 크기를 결정 • 에포크는 훈련셋을 한번 반복하는 것", idx)
    assert r.ok and r.mode == "full" and r.page == 7 and r.pages == [7, 8], r


def test_segments_non_contiguous():
    """떨어진 두 문장을 이어 붙인 경우: 구간별 일치, 각 구간의 페이지 기록."""
    idx = ChunkIndex("[[PAGE 7]]\n학습률은 하강하는 스텝의 크기를 결정\n중간에 다른 설명이 있음\n"
                     "[[PAGE 8]]\n에포크는 훈련셋을 한번 반복하는 것")
    r = check_evidence("에포크는 훈련셋을 한번 반복하는 것 • 학습률은 하강하는 스텝의 크기를 결정", idx)
    assert r.ok and r.mode == "segments" and r.page == 8 and r.pages == [7, 8], r


def test_garbled_math_rejected():
    """B-4 실행에서 정답이 틀린 문제를 만든 실제 evidence (다항 특성, 위첨자 깨짐)."""
    d = json.loads(EXTRACTED.read_text(encoding="utf-8"))
    idx = ChunkIndex(d["chunks"][4]["text"])
    r = check_evidence("특성 𝑎, 𝑏 , 차수=3이면, 𝑎:, 𝑎;, 𝑏:, 𝑏;, 𝑎𝑏, 𝑎:𝑏, 𝑎𝑏: 특성 추가", idx)
    assert not r.ok and "수식" in r.reason, r


def test_prose_with_one_symbol_kept():
    """설명 문장에 기호가 한두 개 섞인 경우는 통과 (B-3에서 실제로 나온 evidence)."""
    d = json.loads(EXTRACTED.read_text(encoding="utf-8"))
    hits = [check_evidence("𝜼 (에타)는 학습률 •하강하는 스텝의 크기를 결정", ChunkIndex(c["text"])).ok
            for c in d["chunks"]]
    assert any(hits), hits


def test_real_cases_from_b3():
    """B-3에서 원문 불일치로 나왔던 실제 evidence 2건."""
    d = json.loads(EXTRACTED.read_text(encoding="utf-8"))
    c4 = ChunkIndex(d["chunks"][3]["text"])
    r = check_evidence("미니배치 경사 하강법이 확률적 경사 하강법보다 최소값에 더 가까이 도달할 수 있으나, "
                       "한편으로는 지역 최소값에서 빠져나오기가 더 힘들 수 있음 (특히 선형 회귀가 아닌 "
                       "지역 최소값이 존재하는 비용 함수의 경우)", c4)
    assert r.ok and r.mode == "full", r
    c6 = ChunkIndex(d["chunks"][5]["text"])
    r = check_evidence("자유도가 높은 모델 (예, 고차 다항 회귀 모델)은 높은 분산을 가지기 쉬워 훈련 데이터에 "
                       "오버피팅 (과대적합)되는 경향이 있음 •모델의 복잡도가 커지면 통상적으로 분산은 "
                       "늘어나고 편향은 줄어듦", c6)
    assert r.ok and r.mode == "segments", r
    # 페이지를 실제 페이지 텍스트로 교차 확인
    for p in r.pages:
        assert any(p == pg["page"] for pg in d["pages"])
    print(f"    청크6 구간 페이지 = {r.pages}")


def test_page_matches_page_text():
    """코드가 정한 페이지에 정말 그 문장이 있는지 페이지 원문으로 교차 확인."""
    from quizen.validate import normalize
    d = json.loads(EXTRACTED.read_text(encoding="utf-8"))
    ev = "에포크 (epoch)는 훈련셋을 한번 반복하는 것"
    for c in d["chunks"]:
        r = check_evidence(ev, ChunkIndex(c["text"]))
        if r.ok:
            page_text = next(p["text"] for p in d["pages"] if p["page"] == r.page)
            assert normalize(ev) in normalize(page_text)
            print(f"    '{ev}' → 청크 {c['chunk_id']}, {r.page}페이지 (페이지 원문에서 재확인)")
            return
    raise AssertionError("어느 청크에서도 못 찾음")


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
