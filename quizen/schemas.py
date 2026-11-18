"""모델 출력 구조. Ollama의 JSON Schema 강제(format)와 Pydantic 검증에 같은 정의를 쓴다."""
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Difficulty = Literal["하", "중", "상"]
BANNED_CHOICES = ("모두 맞다", "모두 틀리다", "모두 정답", "정답 없음", "위의 모든")


class GeneratedQuestion(BaseModel):
    question: str = Field(min_length=1)
    choices: list[str] = Field(min_length=4, max_length=4)
    answer_index: int = Field(ge=0, le=3)
    difficulty: Difficulty
    evidence: str = Field(min_length=1)

    @field_validator("question", "evidence")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("빈 문자열")
        return v

    @field_validator("choices")
    @classmethod
    def _choices_ok(cls, v: list[str]) -> list[str]:
        v = [c.strip() for c in v]
        if any(not c for c in v):
            raise ValueError("빈 보기")
        if len(set(v)) != 4:
            raise ValueError("중복 보기")
        if any(b in c for c in v for b in BANNED_CHOICES):
            raise ValueError("금지된 보기 유형")
        return v


class GeneratedBatch(BaseModel):
    questions: list[GeneratedQuestion]


# ---------------------------------------------------------------- 단답형
class GeneratedShort(BaseModel):
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1, max_length=100)             # 용어 하나, 여러 개면 쉼표로 구분
    accepted: list[str] = Field(default_factory=list, max_length=5)   # 같은 뜻의 다른 표기 (예: 영문명)
    difficulty: Difficulty
    evidence: str = Field(min_length=1)

    @field_validator("question", "answer", "evidence")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("빈 문자열")
        return v

    @field_validator("accepted")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return [a.strip() for a in v if a.strip()]


class ShortBatch(BaseModel):
    questions: list[GeneratedShort]


# ---------------------------------------------------------------- 서술형
class RubricItem(BaseModel):
    point: str = Field(min_length=1)          # 답안에 들어가야 할 핵심 내용 한 가지
    evidence: str = Field(min_length=1)       # 그 내용의 근거 원문 (글자 그대로)


class GeneratedEssay(BaseModel):
    question: str = Field(min_length=1)
    model_answer: str = Field(min_length=1)
    rubric: list[RubricItem] = Field(min_length=2, max_length=4)
    difficulty: Difficulty
    evidence: str = Field(min_length=1)

    @field_validator("question", "model_answer", "evidence")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("빈 문자열")
        return v


class EssayBatch(BaseModel):
    questions: list[GeneratedEssay]


BATCH_MODELS = {"mcq": GeneratedBatch, "short": ShortBatch, "essay": EssayBatch}
QTYPE_LABEL = {"mcq": "객관식", "short": "단답형", "essay": "서술형"}


def batch_schema(n: int, qtype: str = "mcq") -> dict:
    """요청한 문제 수를 넘지 않도록 maxItems를 건 JSON Schema."""
    schema = BATCH_MODELS[qtype].model_json_schema()
    schema["properties"]["questions"]["maxItems"] = n
    schema["properties"]["questions"]["minItems"] = 1
    return schema


# ---------------------------------------------------------------- 검증·채점 응답
class VerifyResult(BaseModel):
    selected_index: int = Field(ge=0, le=3)
    ambiguous: bool
    reason: str


class VerifyShortResult(BaseModel):
    """단답형 정답 숨김 풀이: 모델이 직접 쓴 답."""
    answer: str
    ambiguous: bool
    reason: str


class GradeShortResult(BaseModel):
    """단답형 채점: 학생 답이 정답과 같은 뜻인지."""
    equivalent: bool
    reason: str


class EssayPointResult(BaseModel):
    index: int = Field(ge=0, le=3)
    met: bool
    comment: str


class GradeEssayResult(BaseModel):
    """서술형 채점: 채점 기준별 충족 여부와 전체 피드백."""
    points: list[EssayPointResult] = Field(min_length=1, max_length=4)
    feedback: str
