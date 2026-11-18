"""B-6: 보기 섞기, 정답 분리, 채점.

- 보기와 '정답 여부'를 한 묶음으로 섞어서, 섞은 뒤 정답이 몇 번에 있는지 코드가 계산한다.
- 정답 위치가 특정 번호에 몰리지 않게, 문제 수만큼 0~3번을 고르게 나눈 목록을 먼저 만들고
  그 목록을 섞어 문제마다 정답 자리를 배정한다. (예: 5문제 → 0,1,2,3 한 번씩 + 1개 무작위)
- 화면에 줄 정보(PublicQuestion)에는 정답·근거가 없다. 정답·근거(AnswerKey)는 채점 쪽만 가진다.
- 정답과 해설은 제출해야 grade()로 돌려받을 수 있다.
"""
import random
import uuid
from dataclasses import dataclass, field

from .validate import ValidQuestion


@dataclass(frozen=True)
class PublicQuestion:
    """풀이 화면용. 정답 정보가 전혀 없다."""
    qid: str
    number: int
    question: str
    choices: tuple[str, ...]
    difficulty: str
    qtype: str = "mcq"


@dataclass(frozen=True)
class AnswerKey:
    """채점용. 사용자에게는 제출 후에만 보여 준다."""
    qid: str
    answer_index: int
    evidence: str
    page: int
    evidence_pages: tuple[int, ...]
    source_document: str
    chunk_id: int
    qtype: str = "mcq"
    answer_text: str = ""                   # 단답 정답 / 서술 모범 답안
    accepted: tuple = ()                    # 단답 인정 답안
    rubric: tuple = ()                      # 서술 채점 기준


@dataclass
class QuestionResult:
    number: int
    question: str
    choices: tuple[str, ...]
    difficulty: str
    user_index: int | None
    answer_index: int
    correct: bool
    evidence: str
    page: int
    source_document: str
    chunk_id: int
    evidence_pages: tuple[int, ...] = ()
    qtype: str = "mcq"
    user_text: str = ""                     # 단답·서술에서 쓴 답
    answer_text: str = ""                   # 단답 정답 / 서술 모범 답안


@dataclass
class GradeResult:
    total: int
    correct: int
    results: list[QuestionResult] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def balanced_positions(n: int, rng: random.Random) -> list[int]:
    """n개 문제의 정답 자리. 0~3이 최대 1개 차이로 고르게 나오게 한다."""
    base = [i % 4 for i in range(n)]
    extra_order = [0, 1, 2, 3]
    rng.shuffle(extra_order)                 # 4로 나눠떨어지지 않을 때 남는 자리도 무작위로
    base = [extra_order[p] for p in base]
    rng.shuffle(base)
    return base


def shuffle_question(q: ValidQuestion, target: int, rng: random.Random) -> tuple[list[str], int]:
    """정답 보기를 target 자리에 두고, 오답 보기 셋은 나머지 자리에 무작위로 배치한다."""
    paired = [(c, i == q.answer_index) for i, c in enumerate(q.choices)]   # (보기, 정답 여부) 묶음
    correct = next(p for p in paired if p[1])
    wrong = [p for p in paired if not p[1]]
    rng.shuffle(wrong)
    placed = wrong[:target] + [correct] + wrong[target:]
    new_answer = next(i for i, (_, ok) in enumerate(placed) if ok)       # 섞은 뒤 정답 위치를 다시 계산
    return [c for c, _ in placed], new_answer


class QuizSession:
    """문제 세트 한 회차. 정답 키는 이 객체 안에만 있다."""

    def __init__(self, questions: list[ValidQuestion], seed: int | None = None):
        rng = random.Random(seed)
        # 정답 위치 고르게 나누기는 객관식끼리만 한다 (단답·서술은 보기가 없음)
        n_mcq = sum(q.qtype == "mcq" for q in questions)
        targets = iter(balanced_positions(n_mcq, rng))
        self.public: list[PublicQuestion] = []
        self._keys: dict[str, AnswerKey] = {}
        for n, q in enumerate(questions, 1):
            if q.qtype == "mcq":
                choices, ans = shuffle_question(q, next(targets), rng)
            else:
                choices, ans = [], -1
            qid = uuid.uuid4().hex[:12]
            self.public.append(PublicQuestion(qid, n, q.question, tuple(choices), q.difficulty, q.qtype))
            self._keys[qid] = AnswerKey(qid, ans, q.evidence, q.page, tuple(q.evidence_pages),
                                        q.source_document, q.chunk_id, q.qtype, q.answer_text,
                                        tuple(q.accepted), tuple(q.rubric))
        self.submitted = False

    def grade(self, answers: dict[str, int | None]) -> GradeResult:
        """answers: qid → 사용자가 고른 번호(0~3, 안 고르면 None). 제출 처리도 함께 한다."""
        results = []
        for pq in self.public:
            key = self._keys[pq.qid]
            ua = answers.get(pq.qid)
            results.append(QuestionResult(
                number=pq.number, question=pq.question, choices=pq.choices, difficulty=pq.difficulty,
                user_index=ua, answer_index=key.answer_index, correct=(ua == key.answer_index),
                evidence=key.evidence, page=key.page, source_document=key.source_document,
                chunk_id=key.chunk_id, evidence_pages=key.evidence_pages,
            ))
        self.submitted = True
        return GradeResult(total=len(results), correct=sum(r.correct for r in results), results=results)
