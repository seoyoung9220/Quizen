"""B-6 보기 섞기·정답 분리·채점 테스트. 실행: python tests/test_quiz.py"""
import os
os.environ["QUIZEN_OFFLINE"] = "1"   # 테스트는 절대 모델(Mac Studio)을 부르지 않는다
import dataclasses
import json
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quizen.quiz import PublicQuestion, QuizSession, balanced_positions  # noqa: E402
from quizen.validate import ValidQuestion  # noqa: E402


def make(n: int, answer_index: int = 0) -> list[ValidQuestion]:
    return [ValidQuestion(question=f"Q{i}", choices=[f"Q{i}-정답", f"Q{i}-오답1", f"Q{i}-오답2", f"Q{i}-오답3"]
                          if answer_index == 0 else [f"Q{i}-오답1", f"Q{i}-정답", f"Q{i}-오답2", f"Q{i}-오답3"],
                          answer_index=answer_index, difficulty="중", evidence="근거 문장입니다",
                          page=3, evidence_pages=[3], evidence_mode="full",
                          source_document="a.pdf", chunk_id=1) for i in range(n)]


def test_new_index_points_to_original_correct_text():
    """섞은 뒤 정답 번호의 보기 = 원래 정답 보기."""
    for seed in range(200):
        qs = make(7, answer_index=seed % 2)
        s = QuizSession(qs, seed=seed)
        for pq in s.public:
            key = s._keys[pq.qid]
            assert pq.choices[key.answer_index].endswith("-정답"), (seed, pq.choices, key.answer_index)
            assert sorted(pq.choices) == sorted(qs[pq.number - 1].choices)     # 보기를 잃거나 바꾸지 않음


def test_positions_balanced():
    """모든 문제의 원래 정답이 0번이어도, 섞은 뒤 정답 위치는 0~3에 고르게 (최대 1개 차이)."""
    for n in (1, 3, 5, 15, 20, 37):
        for seed in range(50):
            s = QuizSession(make(n, 0), seed=seed)
            cnt = Counter(s._keys[p.qid].answer_index for p in s.public)
            counts = [cnt.get(i, 0) for i in range(4)]
            assert max(counts) - min(counts) <= 1, (n, seed, counts)


def test_order_actually_shuffled():
    """오답 보기 순서도 섞인다: 200회 중 원래 순서 그대로인 비율이 낮아야 한다."""
    same = 0
    for seed in range(200):
        s = QuizSession(make(1, 0), seed=seed)
        same += list(s.public[0].choices) == make(1, 0)[0].choices
    assert same < 30, same      # 이론상 1/24 ≈ 8회


def test_public_has_no_answer():
    s = QuizSession(make(3), seed=1)
    fields = {f.name for f in dataclasses.fields(PublicQuestion)}
    assert fields == {"qid", "number", "question", "choices", "difficulty", "qtype"}, fields
    dumped = json.dumps([dataclasses.asdict(p) for p in s.public], ensure_ascii=False)
    for bad in ("answer", "evidence", "page", "근거"):
        assert bad not in dumped, bad


def test_grading():
    s = QuizSession(make(4), seed=7)
    keys = {p.qid: s._keys[p.qid].answer_index for p in s.public}
    answers = {}
    for i, p in enumerate(s.public):
        answers[p.qid] = keys[p.qid] if i < 3 else (keys[p.qid] + 1) % 4   # 3개 맞히고 1개 틀림
    r = s.grade(answers)
    assert (r.total, r.correct) == (4, 3) and abs(r.accuracy - 0.75) < 1e-9
    assert [x.correct for x in r.results] == [True, True, True, False]
    assert r.results[3].page == 3 and r.results[3].source_document == "a.pdf"
    assert s.submitted


def test_unanswered_is_wrong():
    s = QuizSession(make(2), seed=3)
    r = s.grade({})
    assert r.correct == 0 and all(x.user_index is None for x in r.results)


def test_balanced_positions_counts():
    rng = random.Random(0)
    assert sorted(balanced_positions(8, rng)) == [0, 0, 1, 1, 2, 2, 3, 3]


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
