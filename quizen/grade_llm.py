"""단답형·서술형 모델 채점. 객관식과 '글자가 정확히 같은 단답'은 store.grade()가 코드로 끝낸다.

- 단답형: 학생 답이 정답과 같은 뜻인지(다른 표기·사소한 오타 인정) → 맞으면 1점, 아니면 0점
- 서술형: 채점 기준마다 충족 여부와 한 줄 평 → 점수 = 충족한 기준 수 / 전체 기준 수 (코드 계산)
          ESSAY_PASS(60%) 이상이면 정답으로 본다. 부분 점수는 결과 화면에 그대로 보여 준다.
모델 판정은 틀릴 수 있으므로, 결과 화면에서 언제든 '판정 바꾸기'로 고칠 수 있다.
"""
from pydantic import BaseModel, ValidationError

from . import config, prompts
from .llm import client, fit_context
from .schemas import GradeEssayResult, GradeShortResult

ESSAY_PASS = 0.6
RETRIES = 2


def _ask(messages: list[dict], schema: type[BaseModel], num_predict: int):
    fit_context(messages, num_predict)
    for _ in range(RETRIES + 1):
        r = client().chat(model=config.MODEL, messages=messages, format=schema.model_json_schema(), think=False,
                          keep_alive=config.KEEP_ALIVE,
                          options={"num_ctx": config.NUM_CTX, "temperature": 0, "num_predict": num_predict})
        try:
            return schema.model_validate_json(r.message.content)
        except ValidationError:
            continue
    return None


def grade_short(q: dict) -> tuple[bool, float, dict]:
    user = prompts.GRADE_SHORT_USER.format(evidence=q["evidence"], question=q["question"], answer=q["answer_text"],
                                           accepted=", ".join(q["accepted"]) or "(없음)", user=q["user_text"])
    res = _ask([{"role": "system", "content": prompts.GRADE_SHORT_SYSTEM}, {"role": "user", "content": user}],
               GradeShortResult, 200)
    if res is None:
        return False, 0.0, {"method": "model", "error": "채점 응답 형식 오류", "reason": "채점하지 못했습니다. 판정 바꾸기로 직접 채점해 주세요."}
    return res.equivalent, 1.0 if res.equivalent else 0.0, {"method": "model", "reason": res.reason}


def grade_essay(q: dict) -> tuple[bool, float, dict]:
    rubric = q["rubric"]
    lines = "\n".join(f"{i}. {r['point']} (근거: {r['evidence']})" for i, r in enumerate(rubric))
    user = prompts.GRADE_ESSAY_USER.format(question=q["question"], model_answer=q["answer_text"],
                                           rubric=lines, user=q["user_text"])
    res = _ask([{"role": "system", "content": prompts.GRADE_ESSAY_SYSTEM}, {"role": "user", "content": user}],
               GradeEssayResult, 600)
    if res is None:
        return False, 0.0, {"method": "model", "error": "채점 응답 형식 오류", "points": [],
                            "feedback": "채점하지 못했습니다. 판정 바꾸기로 직접 채점해 주세요."}
    # 기준 번호별로 정리 (모델이 빠뜨린 기준은 미충족으로 본다)
    by_idx = {p.index: p for p in res.points if p.index < len(rubric)}
    points = [{"point": r["point"], "met": bool(by_idx[i].met) if i in by_idx else False,
               "comment": by_idx[i].comment if i in by_idx else "채점 결과 없음"} for i, r in enumerate(rubric)]
    score = sum(p["met"] for p in points) / len(points)
    return score >= ESSAY_PASS, round(score, 3), {"method": "model", "points": points, "feedback": res.feedback}


def grade_pending(store, question_ids: list[int], progress=lambda i, n, msg: None) -> None:
    """store.grade()가 돌려준 '모델 채점 필요' 문제들을 채점해 저장한다."""
    for i, qid in enumerate(question_ids, 1):
        q = store.question(qid)
        progress(i, len(question_ids), f"{q['number']}번 ({'단답형' if q['qtype'] == 'short' else '서술형'}) 채점 중")
        try:
            ok, score, detail = (grade_short if q["qtype"] == "short" else grade_essay)(q)
        except Exception as e:  # noqa: BLE001 — 한 문제 채점 실패가 회차 전체를 막지 않게
            ok, score, detail = False, 0.0, {"method": "model", "error": str(e)[:200],
                                             "reason": "채점 중 오류가 났습니다. 판정 바꾸기로 직접 채점해 주세요."}
        store.set_auto_grade(qid, ok, score, detail)
