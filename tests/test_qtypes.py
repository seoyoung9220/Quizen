"""문제 유형(객관식·단답형·서술형) 테스트. 모델 대신 가짜 응답을 쓴다. 실행: python tests/test_qtypes.py"""
import os
os.environ["QUIZEN_OFFLINE"] = "1"   # 테스트는 절대 모델(Mac Studio)을 부르지 않는다
import random
import sys
import types
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import quizen.grade_llm as gl  # noqa: E402
import quizen.pipeline as pl  # noqa: E402
import quizen.verify as vf  # noqa: E402
from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.explain import build_explain_messages  # noqa: E402
from quizen.quiz import QuestionResult, QuizSession  # noqa: E402
from quizen.schemas import (GeneratedEssay, GeneratedQuestion, GeneratedShort, RubricItem,  # noqa: E402
                            VerifyShortResult)
from quizen.validate import ChunkIndex, ValidQuestion, validate  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from test_store import PDF, PAGES, Sandbox  # noqa: E402

EXT = analyze_pdf(PDF)
CHUNK = next(c for c in EXT["chunks"] if "에포크" in c["text"])
IDX = ChunkIndex(CHUNK["text"])
EV = "에포크 (epoch)는 훈련셋을 한번 반복하는 것"
EV2 = "학습률이 너무 크지 않고, 충분한 시간이 주어지면, GD가 전역 최소값으로 가깝게 접근할 수 있음을 보장"


# ------------------------------------------------------------------ 코드 검증
def test_short_answer_must_be_in_evidence():
    ok, why = validate(GeneratedShort(question="훈련셋을 한 번 반복하는 단위는?", answer="에포크", accepted=["epoch", "EPOCH",
                       "이터레이션"], difficulty="하", evidence=EV), IDX, "9주차", 1)
    assert ok and ok.qtype == "short" and ok.answer_text == "에포크", why
    assert ok.accepted == ["epoch"], ok.accepted          # 자료에 있는 표기만, 대소문자 중복·자료에 없는 말 제거
    # 자료에 나오지만 정답에 붙어 있지 않은 말(같은 문장의 다른 단어)은 인정 답안이 될 수 없다
    ok2, _ = validate(GeneratedShort(question="?", answer="에포크", accepted=["훈련셋", "반복하는 것"],
                                     difficulty="하", evidence=EV), IDX, "x", 1)
    assert ok2 and ok2.accepted == [], ok2.accepted


def test_accepted_must_be_adjacent_real_case():
    """실측 버그 재현: 정답 '전체 훈련셋'의 인정 답안으로 반대 개념 '한 개 샘플'이 들어가면 안 된다."""
    sq = lambda t: "".join(t.split())
    chunk = next(c for c in EXT["chunks"] if "전체훈련셋" in sq(c["text"]) and "한개샘플" in sq(c["text"]))
    idx = ChunkIndex(chunk["text"])
    # 원문에서 두 줄에 걸친 문장 (앞선 실측에서 모델이 실제로 낸 근거)
    ev = "배치 경사 하강법의 주요 문제는 매 스텝에서 전체 훈련셋을 사용해서 그레이디언트를 계산하므로 훈련셋이 커지면 매우 느려짐"
    v, why = validate(GeneratedShort(question="배치 경사 하강법이 매 스텝에서 쓰는 데이터는?", answer="전체 훈련셋",
                                     accepted=["한 개 샘플", "하나의 샘플"], difficulty="중", evidence=ev), idx, "x", 1)
    assert v and v.accepted == [], (v.accepted if v else why)
    from quizen.verify import short_matches
    assert not short_matches("한 개 샘플", v.answer_text, v.accepted)


def test_same_short_answer_is_duplicate():
    """실측: 다른 질문인데 단답 정답이 둘 다 '볼록 함수' → 중복. 객관식에는 적용하지 않는다."""
    from quizen.dedup import duplicate_reason
    prev = [{"question": "선형 회귀 MSE 비용 함수는 어떤 성질의 함수인가?", "choices": [], "evidence": "MSE 비용 함수는 볼록 함수",
             "answer": "볼록 함수"}]
    r = duplicate_reason("로지스틱 회귀 비용 함수는 어떤 성질이라 전역 최소값을 보장하는가?", [],
                         "이 비용 함수는 볼록 함수(convex)이므로 경사 하강법을 사용하면", prev, answer="볼록함수")
    assert r.startswith("같은 정답"), r
    assert duplicate_reason("로지스틱 회귀 비용 함수는 어떤 성질이라 전역 최소값을 보장하는가?", [],
                            "이 비용 함수는 볼록 함수(convex)이므로 경사 하강법을 사용하면", prev) == ""


def test_adjacent_forms_kept():
    from quizen.validate import _adjacent, answer_norm as n
    t = n("● 확률적 경사 하강법(SGD)은 … Stochastic GD 확률적 경사 하강법 구현")
    assert _adjacent(t, n("확률적 경사 하강법"), n("SGD")) and _adjacent(t, n("확률적 경사 하강법"), n("Stochastic GD"))
    assert not _adjacent(t, n("확률적 경사 하강법"), n("구현하는"))
    bad, why = validate(GeneratedShort(question="?", answer="반복 단위", difficulty="하", evidence=EV), IDX, "x", 1)
    assert bad is None and "근거 문장에 없음" in why


def test_short_answer_length():
    """실측: 설명구('…비용 함수의 경우')가 단답 정답으로 나옴 → 용어(3단어 이하)만 받는다. 괄호 속 다른 표기는 세지 않는다."""
    from quizen.validate import short_too_long
    assert short_too_long("선형 회귀가 아닌 지역 최소값이 존재하는 비용 함수의 경우")
    assert short_too_long("훈련셋에 가장 잘 맞도록")
    for ok in ["에포크", "볼록 함수", "확률적 경사 하강법", "언더피팅 (과소적합)", "배치 경사 하강법(Batch GD)"]:
        assert not short_too_long(ok), ok
    ev = "에포크 (epoch)는 훈련셋을 한번 반복하는 것"
    bad, why = validate(GeneratedShort(question="?", answer="훈련셋을 한번 반복하는 것", difficulty="하", evidence=ev),
                        IDX, "x", 1)
    assert bad is None and "너무 김" in why, why


def test_short_answer_terms_and_comma_list():
    """사용자 결정(2026-11-18): 단답은 용어 하나, 여러 개면 쉼표로 구분. 문장은 서술형이다."""
    from quizen.validate import short_is_sentence, split_items
    assert split_items("에포크, 배치 ,  미니배치") == ["에포크", "배치", "미니배치"]
    # 실측(상 단답)에서 나온 문장형 정답 → 버림
    for bad in ["힘들 수 있음", "훈련셋을 반복", "알고리즘이 멈춤 없음", "같은 스케일을 갖는 것"]:
        assert short_is_sentence(bad), bad
    for ok in ["특성 수", "볼록 함수", "평가 지표", "오버피팅", "에포크, 확률적 경사 하강법"]:
        assert not short_is_sentence(ok), ok
    v, why = validate(GeneratedShort(question="이 문장에 나온 두 용어는?", answer="에포크, 훈련셋", accepted=["epoch"],
                                     difficulty="하", evidence=EV), IDX, "x", 1)
    assert v and v.answer_text == "에포크, 훈련셋" and v.accepted == ["epoch"], why
    bad, why = validate(GeneratedShort(question="?", answer="에포크, 학습률", difficulty="하", evidence=EV), IDX, "x", 1)
    assert bad is None and "근거 문장에 없음: 학습률" in why, why
    bad, why = validate(GeneratedShort(question="?", answer="에포크, 에포크", difficulty="하", evidence=EV), IDX, "x", 1)
    assert bad is None and "겹침" in why, why
    bad, why = validate(GeneratedShort(question="?", answer="훈련셋을 한번", difficulty="하", evidence=EV), IDX, "x", 1)
    assert bad is None and "문장" in why, why


def test_short_list_grading():
    """쉼표 목록: 순서 무관, 빠지거나 더 쓰면 오답, 다른 표기는 그 항목 자리에만."""
    sm = vf.short_matches
    assert sm("훈련셋, 에포크", "에포크, 훈련셋", [], evidence=EV)
    assert sm("epoch, 훈련셋", "에포크, 훈련셋", ["epoch"], evidence=EV)
    assert not sm("에포크", "에포크, 훈련셋", [], evidence=EV)                      # 하나 빠짐
    assert not sm("에포크, 훈련셋, 학습률", "에포크, 훈련셋", [], evidence=EV)      # 더 씀
    assert not sm("에포크, epoch", "에포크, 훈련셋", ["epoch"], evidence=EV)       # 같은 항목을 두 번
    # 검증자가 '와'로 이어 쓴 답은 검증에서만 받아 준다
    assert sm("에포크와 훈련셋", "에포크, 훈련셋", [], allow_contain=True, evidence=EV)
    assert not sm("에포크와 훈련셋", "에포크, 훈련셋", [], evidence=EV)


def test_telltale_distractor_rejected():
    """오답 보기에 자료에 없는 단정 표현('자동으로' 등)이 있으면 버린다. 자료에 있는 표현이면 허용."""
    from quizen.validate import telltale_choice
    chunk = "[[PAGE 1]]\n경사 하강법 사용 시 반드시 모든 특성이 같은 스케일을 갖도록 해야 함"
    ch = ["스케일을 맞춘다", "학습률이 자동으로 조정된다", "반드시 같은 스케일이어야 한다는 규칙은 없다", "c"]
    assert telltale_choice(ch, 0, chunk) == "자동으로"
    assert telltale_choice(["a", "반드시 b", "c", "d"], 0, chunk) == ""
    assert telltale_choice(["항상 빠르다", "b", "c", "d"], 0, chunk) == ""     # 정답 보기는 검사하지 않음
    bad, why = validate(GeneratedQuestion(question="?", choices=["에포크", "학습률이 즉시 바뀜", "c", "d"], answer_index=0,
                                          difficulty="중", evidence=EV), IDX, "x", 1)
    assert bad is None and "티 나는 오답" in why, why


def test_broken_formula_question_rejected():
    """실측(2026-11-18): 근거의 수식이 깨져 있는데('𝜃 = 𝑿& - 𝑿 *') 행렬 곱 조합을 묻는 문제 → 버림."""
    from quizen.validate import leans_on_broken_formula as lb
    ev = "결과를 바로 얻을 수 있는 수식 (mathematical equation) ;𝜃 = 𝑿& - 𝑿 *"
    assert lb(ev, "연구자가 직접 수행해야 하는 핵심적인 행렬 연산의 조합은? X의 전치와 X를 곱한 역행렬")
    assert not lb(ev, "정규 방정식은 반복 없이 결과를 바로 얻는 방법인가?")             # 수식을 묻지 않음
    assert not lb("비용 함수를 최소화하는 모델 파라미터 𝜽를 찾는 과정", "파라미터를 찾는 행렬 계산은?")  # 문장 속 기호 하나
    assert not lb("예측 계산 복잡도는 𝑚, 𝑛 에 선형적 증가", "예측 단계에서도 역행렬 계산이 필요한가?")


def test_partial_range_answer():
    """실측: 자료 '약 5.3에서 8배'인데 정답 '5.3' → 범위의 한쪽 끝만 쓴 정답은 버린다."""
    from quizen.validate import is_partial_range
    ev = "특성 수가 두 배로 늘어나면 계산 시간이 대략 약 5.3에서 8배로 증가"
    assert is_partial_range("5.3", ev) and is_partial_range("8배", ev) and is_partial_range("8", ev)
    assert not is_partial_range("1.6cm", "결정 경계는 약 1.6cm 근방에서 만들어짐")
    assert not is_partial_range("GPU", ev)
    assert not is_partial_range("2", "특성이 2개인 경우, 3~5 사이")
    # 실측(2026-11-18): 숫자 뒤 괄호 설명 때문에 범위인 줄 몰라 '5.3'이 정답으로 통과함
    ev2 = "특성 수가 2배로 늘어나면 계산 시간은 5.3 (=22.4) 에서 8(=23) 배 증가"
    assert is_partial_range("5.3", ev2) and is_partial_range("8", ev2) and is_partial_range("8배", ev2)


def test_essay_rubric_evidence_checked():
    good = GeneratedEssay(question="경사 하강법이 전역 최소값에 가까워지는 조건과 에포크의 뜻을 설명하시오.",
                          model_answer="학습률이 너무 크지 않고 시간이 충분해야 하며, 에포크는 훈련셋을 한 번 반복하는 것이다.",
                          rubric=[RubricItem(point="학습률이 너무 크지 않아야 함", evidence=EV2),
                                  RubricItem(point="에포크 = 훈련셋 한 번 반복", evidence=EV)],
                          difficulty="중", evidence=EV2)
    v, why = validate(good, IDX, "9주차", 1)
    assert v and v.qtype == "essay" and len(v.rubric) == 2 and all(r["page"] for r in v.rubric), why
    assert set(v.evidence_pages) >= {v.rubric[0]["page"], v.rubric[1]["page"]}
    bad = good.model_copy(update={"rubric": [good.rubric[0], RubricItem(point="x", evidence="자료에 없는 지어낸 문장입니다")]})
    v2, why2 = validate(bad, IDX, "9주차", 1)
    assert v2 is None and "채점 기준 2" in why2


# ------------------------------------------------------------------ 단답 비교·검증
def test_short_matches():
    sm = vf.short_matches
    assert sm("경사하강법", "경사 하강법", []) and sm("Gradient-Descent", "경사 하강법", ["Gradient Descent"])
    assert not sm("경사 하강법이 아니다", "경사 하강법", [])                        # 채점: 포함은 정답 아님
    # 검증: 정답·인정 답안 표기를 지우고 남는 글자가 2자 이하일 때만 같다고 본다
    assert sm("경사 하강법 (Gradient Descent)", "경사 하강법", ["Gradient Descent"], allow_contain=True)
    assert sm("5.3배", "5.3", [], allow_contain=True)
    assert not sm("5.3에서 8배", "5.3", [], allow_contain=True)        # 실측: 정답이 범위의 일부뿐인 문제
    assert not sm("경사 하강법은 반복 최적화", "경사 하강법", [], allow_contain=True)
    assert not sm("", "x", []) and not sm("   ", "경사", [])


def test_verify_short_and_essay():
    q = ValidQuestion(question="?", choices=[], answer_index=0, difficulty="하", evidence=EV, page=19,
                      evidence_pages=[19], evidence_mode="full", source_document="x", chunk_id=1,
                      qtype="short", answer_text="에포크", accepted=["epoch"])
    orig = vf.solve_short
    try:
        vf.solve_short = lambda *a, **k: VerifyShortResult(answer="Epoch", ambiguous=False, reason="")
        assert vf.verify_question(q, CHUNK["text"])[0]
        vf.solve_short = lambda *a, **k: VerifyShortResult(answer="반복", ambiguous=False, reason="")
        assert not vf.verify_question(q, CHUNK["text"])[0]
        vf.solve_short = lambda *a, **k: VerifyShortResult(answer="에포크", ambiguous=True, reason="")
        assert not vf.verify_question(q, CHUNK["text"])[0]
    finally:
        vf.solve_short = orig
    assert vf.verify_question(ValidQuestion(**{**q.to_dict(), "qtype": "essay"}), CHUNK["text"])[0]


# ------------------------------------------------------------------ 섞기·저장·채점
def mixed_questions() -> list[ValidQuestion]:
    base = dict(difficulty="중", evidence=EV, page=19, evidence_pages=[19], evidence_mode="full",
                source_document="9주차", chunk_id=1)
    mcq = [ValidQuestion(question=f"M{i}", choices=[f"M{i}정답", "b", "c", "d"], answer_index=0, **base) for i in range(4)]
    short = [ValidQuestion(question=f"S{i}", choices=[], answer_index=0, qtype="short", answer_text="에포크",
                           accepted=["epoch"], **base) for i in range(3)]
    essay = [ValidQuestion(question="E0", choices=[], answer_index=0, qtype="essay", answer_text="모범 답안",
                           rubric=[{"point": "p1", "evidence": EV, "page": 19}, {"point": "p2", "evidence": EV2, "page": 14},
                                   {"point": "p3", "evidence": EV, "page": 19}], **base)]
    return mcq + short + essay


def test_session_shuffles_only_mcq():
    s = QuizSession(mixed_questions(), seed=4)
    keys = [s._keys[p.qid] for p in s.public]
    mcq = [(p, k) for p, k in zip(s.public, keys) if p.qtype == "mcq"]
    assert sorted(Counter(k.answer_index for _, k in mcq).values()) == [1, 1, 1, 1]   # 객관식 4개 → 0~3 한 번씩
    assert all(p.choices[k.answer_index].endswith("정답") for p, k in mcq)
    for p, k in zip(s.public, keys):
        if p.qtype != "mcq":
            assert p.choices == () and k.answer_index == -1 and k.answer_text


def test_store_grade_flow():
    with Sandbox() as st:
        d = st.add_document(PDF, PDF.name, PAGES)
        quiz = st.create_quiz(QuizSession(mixed_questions(), seed=1), [d], "중", 8)
        view = st.question_view(quiz)
        assert [v["qtype"] for v in view].count("short") == 3 and all("answer" not in v for v in view)
        rs = {r["question"]: r for r in st.results(quiz)}
        assert rs["S0"]["answer_text"] == "에포크" and rs["S0"]["accepted"] == ["epoch"]
        assert len(rs["E0"]["rubric"]) == 3 and rs["E0"]["answer_index"] is None
        # 답 쓰기: S0 정확히 일치(띄어쓰기·대소문자 무시), S1 다른 표현(모델 필요), S2 안 씀, E0 씀, 객관식 하나 맞힘
        st.save_answer(rs["S0"]["id"], user_text=" EPOCH ")
        st.save_answer(rs["S1"]["id"], user_text="훈련셋 1회 반복")
        st.save_answer(rs["E0"]["id"], user_text="학습률이 작아야 하고...")
        st.save_answer(rs["M0"]["id"], rs["M0"]["answer_index"])
        pending = st.grade(quiz)
        assert set(pending) == {rs["S1"]["id"], rs["E0"]["id"]}, pending
        after = {r["question"]: r for r in st.results(quiz)}
        assert after["S0"]["correct"] and after["S0"]["auto_detail"]["method"] == "code"
        assert not after["S2"]["correct"] and after["S2"]["auto_detail"]["reason"] == "답을 쓰지 않음"
        # 모델 채점 결과 넣기 + 내가 바꾼 판정은 유지
        st.override(rs["E0"]["id"], True, "같은 뜻이야", "")
        st.set_auto_grade(rs["S1"]["id"], True, 1.0, {"method": "model", "reason": "같은 뜻"})
        st.set_auto_grade(rs["E0"]["id"], False, 1 / 3, {"method": "model", "points": [], "feedback": ""})
        final = {r["question"]: r for r in st.results(quiz)}
        assert final["S1"]["correct"] and final["E0"]["correct"] and final["E0"]["auto_correct"] == 0
        assert st.score(quiz) == (8, 4)                    # M0, S0, S1, E0(판정 변경)


# ------------------------------------------------------------------ C-6 오답 노트·재시험
def test_wrong_notes_and_retake_all_types():
    with Sandbox() as st:
        ml = st.create_folder("머신러닝")
        sub = st.create_folder("중간", ml)
        d = st.add_document(PDF, PDF.name, PAGES, folder_id=sub)
        quiz = st.create_quiz(QuizSession(mixed_questions(), seed=1), [d], "중", 8)
        rs = {r["question"]: r for r in st.results(quiz)}
        st.save_answer(rs["M0"]["id"], rs["M0"]["answer_index"])               # 맞힘, 나머지 7개 틀림/안 씀
        st.save_answer(rs["S0"]["id"], user_text="반복")
        for i in st.grade(quiz):
            st.set_auto_grade(i, False, 0.0, {"method": "model"})
        wrong = st.wrong_questions([d])
        assert len(wrong) == 7 and {w["qtype"] for w in wrong} == {"mcq", "short", "essay"}
        assert st.folder_doc_ids(ml) == [d]                                    # 하위 폴더까지 포함
        w0 = next(w for w in wrong if w["question"] == "S0")
        assert w0["user_text"] == "반복" and w0["quiz_name"].endswith("(1)")

        retake, skipped = st.create_retake([w["id"] for w in wrong])
        q2 = st.quiz(retake)
        assert skipped == 0 and q2["name"].endswith("재시험") and q2["parent_quiz_id"] == quiz and q2["produced"] == 7
        r2 = {r["question"]: r for r in st.results(retake)}
        assert r2["S1"]["answer_text"] == "에포크" and r2["S1"]["accepted"] == ["epoch"] and len(r2["E0"]["rubric"]) == 3
        for r in r2.values():                                                 # 객관식은 새로 섞여도 정답 보기는 같음
            if r["qtype"] == "mcq":
                assert r["choices"][r["answer_index"]].endswith("정답")
        # 재시험: M1·S1은 맞히고, 나머지는 또 틀림
        st.save_answer(r2["M1"]["id"], r2["M1"]["answer_index"])
        st.save_answer(r2["S1"]["id"], user_text="에포크")
        for i in st.grade(retake):
            st.set_auto_grade(i, False, 0.0, {"method": "model"})
        left = st.wrong_questions([d])
        names = sorted(w["question"] for w in left)
        assert "M1" not in names and "S1" not in names and len(left) == 5, names
        assert all(w["quiz_id"] == retake for w in left)                      # 같은 문제는 가장 최근 시도로 한 번만

        # 재시험의 재시험: 원래 문제를 계속 기억한다
        r3, _ = st.create_retake([w["id"] for w in left])
        roots = {r["origin_question_id"] for r in st.results(r3)}
        assert roots == {rs[n]["id"] for n in names}

        # 원본 PDF가 지워진 문제는 재시험에서 뺀다 (여러 PDF를 섞은 회차에서 PDF 하나를 지운 경우)
        st.conn.execute("UPDATE questions SET doc_id=NULL WHERE id=?", (left[0]["id"],)); st.conn.commit()
        _, skipped = st.create_retake([left[0]["id"], left[1]["id"]])
        assert skipped == 1


# ------------------------------------------------------------------ 모델 채점 (가짜 응답)
class FakeChat:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    def chat(self, **kw):
        self.calls += 1
        return types.SimpleNamespace(message=types.SimpleNamespace(content=self.content))


def run_with(fake, fn, q):
    orig = gl.client
    gl.client = lambda: fake
    try:
        return fn(q)
    finally:
        gl.client = orig


def test_grade_essay_scoring():
    q = {"question": "Q", "answer_text": "모범", "user_text": "내 답",
         "rubric": [{"point": "p1", "evidence": EV}, {"point": "p2", "evidence": EV2}, {"point": "p3", "evidence": EV}]}
    ok, score, d = run_with(FakeChat('{"points":[{"index":0,"met":true,"comment":"a"},{"index":2,"met":true,"comment":"c"}],'
                                     '"feedback":"좋아요"}'), gl.grade_essay, q)
    assert ok and abs(score - 2 / 3) < 1e-3 and [p["met"] for p in d["points"]] == [True, False, True]
    assert d["points"][1]["comment"] == "채점 결과 없음"           # 모델이 빠뜨린 기준은 미충족
    ok, score, _ = run_with(FakeChat('{"points":[{"index":0,"met":true,"comment":"a"}],"feedback":""}'), gl.grade_essay, q)
    assert not ok and abs(score - 1 / 3) < 1e-3                     # 1/3 < 60% → 오답
    fake = FakeChat("깨진 응답")
    ok, score, d = run_with(fake, gl.grade_essay, q)
    assert not ok and d.get("error") and fake.calls == 3            # 형식 오류면 2번 더 묻고 포기


def test_grade_short_model():
    q = {"question": "Q", "answer_text": "에포크", "accepted": [], "user_text": "훈련셋 1회 반복", "evidence": EV}
    ok, score, d = run_with(FakeChat('{"equivalent":true,"reason":"같은 개념"}'), gl.grade_short, q)
    assert ok and score == 1.0 and d["reason"] == "같은 개념"


# ------------------------------------------------------------------ 유형별 묶음 생성
def test_build_mixed():
    seen = []

    def fake(chunk_text, difficulty, num, existing, stats, model=None, conditions="", qtype="mcq", **kw):
        seen.append((qtype, len(existing)))
        lines = [l for l in chunk_text.splitlines() if len(l) > 25 and "[[PAGE" not in l]
        out = []
        for k in range(num):
            ev = lines[(len(seen) * 3 + k) % len(lines)]
            if qtype == "mcq":
                out.append(GeneratedQuestion(question=f"M{len(seen)}-{k} {ev[:15]}", choices=[f"{ev[:6]}{c}{k}" for c in "가나다라"],
                                             answer_index=0, difficulty="중", evidence=ev))
            elif qtype == "short":
                word = next(w for w in ev.split() if len(w) >= 2)
                out.append(GeneratedShort(question=f"S{len(seen)}-{k} {ev[:15]}", answer=word, difficulty="중", evidence=ev))
            else:
                out.append(GeneratedEssay(question=f"E{len(seen)}-{k} {ev[:15]}", model_answer=ev, difficulty="중", evidence=ev,
                                          rubric=[RubricItem(point="a", evidence=ev), RubricItem(point="b", evidence=ev)]))
        return out

    orig = pl.generate_for_chunk
    pl.generate_for_chunk = fake
    try:
        qs, rep = pl.build_mixed(EXT, {"mcq": 2, "short": 2, "essay": 1}, "중", rng=random.Random(2))
    finally:
        pl.generate_for_chunk = orig
    assert Counter(q.qtype for q in qs) == {"mcq": 2, "short": 2, "essay": 1}, Counter(q.qtype for q in qs)
    assert rep.requested == 5 and rep.produced == 5
    assert [t for t, _ in seen][:1] == ["mcq"] and seen[-1][0] == "essay"
    assert {pc["qtype"] for pc in rep.per_chunk} == {"mcq", "short", "essay"}


# ------------------------------------------------------------------ 오답 풀이 메시지
def test_explain_messages_for_short_and_essay():
    r = QuestionResult(number=1, question="Q", choices=(), difficulty="하", user_index=None, answer_index=-1,
                       correct=False, evidence=EV, page=19, source_document="x", chunk_id=1, qtype="short",
                       user_text="반복", answer_text="에포크")
    u = build_explain_messages(r, "ctx")[1]["content"]
    assert "정답: 에포크" in u and "내가 고른 답: 반복" in u and "주관식" in u
    r2 = QuestionResult(**{**r.__dict__, "qtype": "essay", "user_text": "", "answer_text": "모범 답안"})
    u2 = build_explain_messages(r2, "ctx")[1]["content"]
    assert "(모범 답안) 모범 답안" in u2 and "(답을 쓰지 않음)" in u2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v) and v.__module__ == "__main__"]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 통과")
    sys.exit(1 if failed else 0)
