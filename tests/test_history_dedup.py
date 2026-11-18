"""구간 돌려쓰기, 중복 검사, 오답 풀이 맥락 테스트 (모델 호출 없음).
실행: python tests/test_history_dedup.py
"""
import os
os.environ["QUIZEN_OFFLINE"] = "1"   # 테스트는 절대 모델(Mac Studio)을 부르지 않는다
import random
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import quizen.pipeline as pl  # noqa: E402
from quizen import config  # noqa: E402
from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.dedup import duplicate_reason  # noqa: E402
from quizen.explain import build_explain_messages, page_context  # noqa: E402
from quizen.generate import allocate  # noqa: E402
from quizen.history import History, chunk_key  # noqa: E402
from quizen.quiz import QuestionResult  # noqa: E402
from quizen.schemas import GeneratedQuestion  # noqa: E402

PDF = ROOT / "samples" / "sample.pdf"
EXT = analyze_pdf(PDF)


# ------------------------------------------------------------ 구간 돌려쓰기
def test_no_rng_is_top_weights():
    a = allocate(EXT["chunks"], EXT["pages"], 5)
    assert sum(a) == 5 and max(a) == 1


def test_usage_rotates_chunks():
    """이력이 쌓이면 이전에 쓴 구간 대신 안 쓴 구간이 뽑힌다 (rng 없이도)."""
    chunks, pages = EXT["chunks"], EXT["pages"]
    usage: dict[str, int] = {}
    seen = []
    for _ in range(3):
        a = allocate(chunks, pages, 5, usage=usage)
        picked = [chunk_key(c) for c, n in zip(chunks, a) if n]
        seen.append(set(picked))
        for k in picked:
            usage[k] = usage.get(k, 0) + 1
    assert len(set().union(*seen)) == len(chunks), seen        # 세 번 만에 8개 구간을 모두 씀


def test_random_sampling_covers_all_chunks():
    """rng를 쓰면 이력이 없어도 회차마다 구간이 섞인다. 200회 중 모든 청크가 뽑혀야 한다."""
    rng = random.Random(0)
    cnt = Counter()
    for _ in range(200):
        a = allocate(EXT["chunks"], EXT["pages"], 5, rng=rng)
        assert sum(a) == 5 and max(a) == 1
        cnt.update(i for i, n in enumerate(a) if n)
    assert len(cnt) == len(EXT["chunks"]), cnt
    # 정보량이 큰 청크가 더 자주 뽑힌다 (가장 작은 청크 8번 < 가장 큰 청크 4번)
    assert cnt[7] < cnt[3], cnt


def test_many_questions_still_proportional():
    a = allocate(EXT["chunks"], EXT["pages"], 20, usage={}, rng=random.Random(1))
    assert sum(a) == 20 and min(a) >= 1


# ------------------------------------------------------------ 중복 검사
PREV = [{"question": "에포크(epoch)의 정의에 해당하는 것은?",
         "choices": ["훈련셋을 한번 반복하는 것", "파라미터 초기화", "그레이디언트 계산", "특성 스케일 조정"],
         "evidence": "에포크 (epoch)는 훈련셋을 한번 반복하는 것"}]


def test_similar_question_is_duplicate():
    r = duplicate_reason("에포크의 정의로 옳은 것은?", ["a1", "b1", "c1", "d1"], "다른 근거 문장입니다 충분히 길게", PREV)
    assert r.startswith("질문 유사"), r


def test_same_evidence_is_duplicate():
    r = duplicate_reason("훈련셋을 한 번 모두 사용하는 단위를 무엇이라 하는가?", ["가", "나", "다", "라"],
                         "에포크 (epoch)는 훈련셋을 한번 반복하는 것", PREV)
    assert r.startswith("같은 근거"), r


def test_same_choices_is_duplicate():
    r = duplicate_reason("다음 중 학습 단위에 대한 설명은?",
                         ["특성 스케일 조정", "훈련셋을 한번 반복하는 것", "그레이디언트 계산", "파라미터 초기화"],
                         "완전히 다른 근거 문장이 여기에 들어감", PREV)
    assert r.startswith("보기 구성"), r


def test_boilerplate_does_not_make_duplicates():
    """상투 문구만 같고 주제가 다른 질문은 중복이 아니다."""
    prev = [{"question": "경사 하강법에서 학습률에 대한 설명으로 옳은 것은?", "choices": ["a", "b", "c", "d"],
             "evidence": "학습률이 너무 크면 골짜기를 가로질러 반대편으로 건너뛰게 되어"}]
    r = duplicate_reason("미니배치 경사 하강법의 하드웨어 활용에 대한 설명으로 옳은 것은?", ["e", "f", "g", "h"],
                         "행렬 연산에 최적화된 하드웨어를 사용해 성능 향상 기대", prev)
    assert r == "", r


def test_batch_vs_minibatch_not_duplicate():
    """Phase B 실측에서 오탐이 났던 쌍: 배치 vs 미니배치 비교는 다른 문제다."""
    prev = [{"question": "배치 경사 하강법(Batch GD)과 확률적 경사 하강법(SGD)의 특징에 대한 설명으로 옳은 것은?",
             "choices": ["a", "b", "c", "d"], "evidence": "배치 경사 하강법의 주요 문제는 매 스텝에서 전체 훈련셋을 사용"}]
    r = duplicate_reason("미니배치 경사 하강법(Mini-batch GD)과 확률적 경사 하강법(SGD)의 비교에 대한 설명으로 옳은 것은?",
                         ["e", "f", "g", "h"], "미니배치를 어느정도 크게 하면 파라미터 공간에서 덜 불규칙하게 수렴", prev)
    assert r == "", r


def test_real_phase_b_repeats_detected():
    """Phase B 측정에서 실제로 세트마다 반복된 미니배치 문제 2개 (난이도만 다름)."""
    prev = [{"question": "미니배치 경사 하강법(Mini-batch GD)과 확률적 경사 하강법(SGD)의 비교에 대한 설명으로 옳은 것은?",
             "choices": ["a1", "b1", "c1", "d1"],
             "evidence": "미니배치를 어느정도 크게 하면 파라미터 공간에서 확률적 경사 하강법보다 덜 불규칙하게 수렴"}]
    r = duplicate_reason("미니배치 경사 하강법(Mini-batch GD)이 확률적 경사 하강법(SGD)과 비교하여 지역 최소값에 대한 "
                         "수렴 특성에서 보이는 차이로 가장 적절한 것은?", ["a2", "b2", "c2", "d2"],
                         "미니배치를 어느정도 크게 하면 파라미터 공간에서 확률적 경사 하강법보다 덜 불규칙하게 수렴 "
                         "미니배치 경사 하강법이 확률적 경사 하강법보다 최소값에 더 가까이 도달할 수 있으나", prev)
    assert r, "중복을 못 잡음"


def test_different_question_not_duplicate():
    r = duplicate_reason("학습률이 너무 클 때 생기는 현상은?",
                         ["발산할 수 있다", "수렴이 느리다", "지역 최소값에 갇힌다", "변화 없다"],
                         "학습률이 너무 크면 골짜기를 가로질러 반대편으로 건너뛰게 되어", PREV)
    assert r == "", r


def test_pipeline_drops_duplicates_and_regenerates():
    """1차 생성에서 이전 회차와 같은 문제 → 중복 폐기 → 재생성분은 통과. 이전 문제는 프롬프트에도 전달."""
    chunk = next(c for c in EXT["chunks"] if "에포크" in c["text"])
    ext = {**EXT, "chunks": [chunk]}
    hist = History("test-sha")
    hist.questions = list(PREV)
    calls = []

    def fake(chunk_text, difficulty, num, existing, stats, model=None, conditions="", **kw):
        calls.append(list(existing))
        if len(calls) == 1:
            return [GeneratedQuestion(question="에포크(epoch)의 정의에 해당하는 것은?",
                                      choices=["훈련셋을 한번 반복하는 것", "가", "나", "다"], answer_index=0,
                                      difficulty="중", evidence="에포크 (epoch)는 훈련셋을 한번 반복하는 것")]
        return [GeneratedQuestion(question="경사 하강법이 전역 최소값에 가깝게 접근하기 위한 조건은?",
                                  choices=["학습률이 너무 크지 않고 시간이 충분할 때", "학습률이 매우 클 때",
                                           "특성 수가 적을 때", "훈련셋이 작을 때"], answer_index=0,
                                  difficulty="중",
                                  evidence="학습률이 너무 크지 않고, 충분한 시간이 주어지면, GD가 전역 최소값으로 "
                                           "가깝게 접근할 수 있음을 보장")]

    orig = pl.generate_for_chunk
    pl.generate_for_chunk = fake
    try:
        qs, rep = pl.build_quiz(ext, 1, "중", history=hist)
    finally:
        pl.generate_for_chunk = orig
    assert rep.discarded["중복"] == 1, rep.discarded
    assert len(qs) == 1 and qs[0].question.startswith("경사 하강법이 전역"), qs
    assert PREV[0]["question"] in calls[0], calls[0]          # 이전 회차 문제가 '이미 출제된 문제'로 전달됨
    assert rep.chunks_used == [chunk]


def test_history_roundtrip():
    sha = "ffff" * 16
    h = History.load(sha)
    try:
        assert h.chunk_usage == {} and h.questions == []
        q = GeneratedQuestion(question="Q", choices=["a", "b", "c", "d"], answer_index=0, difficulty="하",
                              evidence="근거 문장입니다 충분히")
        h.record([q], [EXT["chunks"][0]])
        h2 = History.load(sha)
        assert h2.chunk_usage == {chunk_key(EXT["chunks"][0]): 1} and h2.questions[0]["question"] == "Q"
    finally:
        h.path.unlink(missing_ok=True)


# ------------------------------------------------------------ 오답 풀이 맥락
def test_explain_gets_full_page_text():
    pages = {p["page"]: p["text"] for p in EXT["pages"]}
    r = QuestionResult(number=1, question="Q", choices=("a", "b", "c", "d"), difficulty="상", user_index=1,
                       answer_index=0, correct=False, evidence="훈련셋과 검증셋 오차 곡선 사이에 공간 있음",
                       page=35, source_document="x.pdf", chunk_id=6, evidence_pages=(35,))
    ctx = page_context(pages, r)
    assert ctx.startswith("[35페이지]") and pages[35] in ctx
    user = build_explain_messages(r, ctx)[1]["content"]
    assert user.index("<관련 페이지 원문>") < user.index("<자료 근거>")


def test_explain_context_capped():
    pages = {1: "가나다 " * 3000, 2: "라마바 " * 3000}
    r = QuestionResult(number=1, question="Q", choices=("a", "b", "c", "d"), difficulty="하", user_index=None,
                       answer_index=0, correct=False, evidence="e", page=1, source_document="x",
                       chunk_id=1, evidence_pages=(1, 2))
    ctx = page_context(pages, r)
    assert "[1페이지]" in ctx and "[2페이지]" not in ctx      # 상한을 넘는 두 번째 페이지는 뺌


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
