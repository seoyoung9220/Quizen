"""B-5: 정답 숨김 검증.

같은 모델이 자기가 만든 문제를 "맞다"고 승인하는 편향을 피하려고, 정답을 숨긴 채 문제를 풀게 한다.
모델의 답과 실제 정답을 비교하는 건 코드가 한다.
- 객관식: 고른 번호 ≠ 정답 번호 → 폐기, ambiguous → 폐기
- 단답형: 모델이 직접 쓴 답이 정답·인정 답안과 같지 않으면 폐기 (공백·대소문자·문장부호 무시), ambiguous → 폐기
- 서술형: 모범 답안의 형태가 여러 가지라 '숨기고 풀기'로는 판단할 수 없다.
          대신 코드 검증(채점 기준마다 근거가 원문에 있음)을 통과한 것으로 충분하다고 본다.
문제마다 이전 대화 없이 독립된 요청으로 푼다.
"""
import time

from pydantic import BaseModel, ValidationError

from . import config, prompts
from .generate import call_timing
from .llm import client, fit_context
from .schemas import VerifyResult, VerifyShortResult
from .validate import ValidQuestion, answer_norm, split_items

VERIFY_RETRIES = 2
TIMINGS: list[dict] = []    # 검증 호출별 읽기/쓰기 시간 (속도 진단용, 최근 200개)


def format_choices(choices: list[str]) -> str:
    return "\n".join(f"{i}. {c}" for i, c in enumerate(choices))


def _ask(messages: list[dict], schema: type[BaseModel], model: str | None = None):
    """검증 요청을 보내고 응답 형식을 확인한다. 형식이 깨지면 최대 2회 다시 묻는다."""
    fit_context(messages, 300)        # 넘치면 ContextOverflow (자료가 잘린 채로 풀게 하지 않음)
    for _ in range(VERIFY_RETRIES + 1):
        t = time.time()
        r = client().chat(
            model=model or config.MODEL,
            messages=messages,
            format=schema.model_json_schema(),
            think=False,
            keep_alive=config.KEEP_ALIVE,
            options={"num_ctx": config.NUM_CTX, "temperature": 0, "num_predict": 300},
        )
        TIMINGS.append(call_timing("검증", r, time.time() - t))
        del TIMINGS[:-200]
        try:
            return schema.model_validate_json(r.message.content)
        except ValidationError:
            continue
    return None


def solve(question: str, choices: list[str], chunk_text: str, model: str | None = None) -> VerifyResult | None:
    """객관식: 정답 없이 문제와 보기만 주고 풀게 한다."""
    return _ask(prompts.build_verify_messages(chunk_text, question, format_choices(choices)), VerifyResult, model)


def solve_short(question: str, chunk_text: str, model: str | None = None) -> VerifyShortResult | None:
    """단답형: 정답 없이 문제만 주고 답을 쓰게 한다."""
    return _ask(prompts.build_verify_short_messages(chunk_text, question), VerifyShortResult, model)


LEFTOVER_MAX = 2   # 검증자 답에서 정답·인정 답안을 지우고 남아도 되는 글자 수 (예: '배', '개')


def _one_matches(u: str, forms: list[str], allow_contain: bool) -> bool:
    """정규화된 답 u가 한 항목의 표기들(forms) 중 하나와 같은지."""
    if not u:
        return False
    if u in forms:
        return True
    if allow_contain and any(n in u for n in forms):
        left = u
        for n in sorted(forms, key=len, reverse=True):
            left = left.replace(n, "")
        return len(left) <= LEFTOVER_MAX
    return False


def _item_forms(items: list[str], accepted: list[str], evidence: str) -> list[list[str]]:
    """정답 항목마다 인정 표기를 붙인다. 다른 표기는 근거 문장에서 바로 붙어 나온 항목에만 준다
    (근거로 알 수 없으면 모든 항목에 준다). 'A, B' 문제에서 A의 다른 표기로 B 자리를 채우지 못하게 한다."""
    norms = [answer_norm(x) for x in items]
    forms = [[n] for n in norms]
    ev = answer_norm(evidence)
    for a in accepted:
        na = answer_norm(a)
        if not na:
            continue
        gaps = [_gap(ev, n, na) for n in norms]
        best = min(gaps)
        # 가장 가까이 붙어 나온 항목에만 준다 ('에포크 (epoch)는 훈련셋' → epoch는 에포크의 표기)
        owners = [i for i, g in enumerate(gaps) if g == best] if best <= 2 else []
        for i in owners or range(len(norms)):
            forms[i].append(na)
    return forms


def _gap(text: str, a: str, b: str) -> int:
    """text에서 a와 b가 가장 가까이 나온 곳의 사이 글자 수. 둘 중 하나라도 없으면 아주 큰 수."""
    if not text or not a or not b:
        return 10**9
    best = 10**9
    i = text.find(a)
    while i >= 0:
        j = text.find(b)
        while j >= 0:
            gap = j - (i + len(a)) if j >= i + len(a) else i - (j + len(b)) if i >= j + len(b) else 0
            best = min(best, gap)
            j = text.find(b, j + 1)
        i = text.find(a, i + 1)
    return best


def short_matches(user: str, answer: str, accepted: list[str], allow_contain: bool = False,
                  evidence: str = "") -> bool:
    """단답 비교 (코드). 공백·대소문자·문장부호는 무시한다.
    정답이 'A, B'처럼 쉼표로 나뉜 여러 용어면, 학생 답도 쉼표로 나눠 순서와 관계없이 하나씩 짝을 맞춘다.
    allow_contain=True (검증용): 정답·인정 답안 표기를 지우고 남는 글자가 LEFTOVER_MAX 이하면 같다고 본다.
      '경사 하강법 (Gradient Descent)' → 둘 다 지우면 남는 것 없음 → 같음
      '5.3배' → '배' 1자 → 같음 / '5.3에서 8배' → '에서8배' 4자 → 다름 (정답이 범위의 일부뿐인 문제를 걸러 냄)
    채점에서는 포함을 허용하지 않는다 ('경사 하강법이 아니다'도 정답을 포함하기 때문)."""
    items = split_items(answer) or [answer]
    forms = _item_forms(items, accepted, evidence)
    if len(items) == 1:
        return _one_matches(answer_norm(user), forms[0], allow_contain)
    users = [answer_norm(x) for x in split_items(user)]
    if len(users) == len(items):
        left = list(range(len(items)))
        for u in users:
            hit = next((i for i in left if _one_matches(u, forms[i], allow_contain)), None)
            if hit is None:
                break
            left.remove(hit)
        else:
            return True
    if allow_contain:
        # 검증자가 '와', '및'으로 이어 쓴 경우: 모든 항목이 들어 있고 남는 글자가 항목당 LEFTOVER_MAX 이하
        u = answer_norm(user)
        if all(any(f in u for f in fs) for fs in forms):
            for f in sorted({f for fs in forms for f in fs}, key=len, reverse=True):
                u = u.replace(f, "")
            return len(u) <= LEFTOVER_MAX * len(items)
    return False


def verify_question(q: ValidQuestion, chunk_text: str) -> tuple[bool, str]:
    if q.qtype == "essay":
        return True, "서술형: 채점 기준 근거 원문 대조로 검증"
    if q.qtype == "short":
        res = solve_short(q.question, chunk_text)
        if res is None:
            return False, "검증 응답 형식 오류"
        if res.ambiguous:
            return False, f"모호함: {res.reason}"
        if not short_matches(res.answer, q.answer_text, q.accepted, allow_contain=True, evidence=q.evidence):
            return False, f"검증자 답 '{res.answer}' ≠ 정답 '{q.answer_text}': {res.reason}"
        return True, res.reason
    res = solve(q.question, q.choices, chunk_text)
    if res is None:
        return False, "검증 응답 형식 오류"
    if res.ambiguous:
        return False, f"모호함: {res.reason}"
    if res.selected_index != q.answer_index:
        return False, f"검증자 {res.selected_index}번 선택 ≠ 정답 {q.answer_index}번: {res.reason}"
    return True, res.reason
