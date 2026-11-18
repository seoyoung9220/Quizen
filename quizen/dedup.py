"""문제 중복 검사 (문자열 유사도, 모델 사용 안 함).

다음 중 하나라도 해당하면 중복으로 본다.
1. 질문이 비슷함: 상투 문구("~에 대한 설명으로 옳은 것은")·조사·괄호 병기를 뺀 핵심 단어 묶음이
   60% 이상 겹침 (단어 단위라서 '배치'와 '미니배치'는 다른 단어로 본다)
2. 같은 근거: 근거 문장이 거의 같거나(유사도 ≥ 0.85) 한쪽이 다른 쪽을 포함 (짧은 근거는 제외)
3. 보기 구성이 비슷함: 보기 4개를 정렬해 이어 붙인 문자열의 유사도 ≥ 0.80
5. [추가] 질문·근거가 함께 겹침: 질문 단어 겹침 ≥ 0.30 이면서 근거 문장의 30% 이상(10자 이상 덩어리)이 겹침
   (실측 2026-11-18: 같은 슬라이드로 '특성 스케일 차이 → 스케일링'을 두 번 냄. 근거 범위가 조금 달라 2번에 안 걸림.
    오늘 측정 문제 쌍 전체에 대 보니 이 조건에 걸리는 쌍은 진짜 중복 2쌍뿐이었다)
예) "에포크의 정의는?" vs "에포크(epoch)의 정의로 옳은 것은?" → 1번으로 중복
"""
import re
from difflib import SequenceMatcher

Q_SIM = 0.60
EV_SIM = 0.85
CH_SIM = 0.80
EV_MIN_CONTAIN = 20   # 이보다 짧은 근거는 '포함' 판정에 쓰지 않는다
PAIR_Q_SIM = 0.30     # 5번 규칙: 질문 단어 겹침
PAIR_EV_SHARE = 0.30  # 5번 규칙: 짧은 쪽 근거 중 겹치는 비율
SHARE_BLOCK = 10

_DROP = re.compile(r"[\s\W_]+", re.UNICODE)
_PAREN = re.compile(r"\([^)]*\)")
_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")
# 단어 끝 조사 (한 번만 떼고, 떼고 나서 1글자 이하가 되면 떼지 않음)
_JOSA = re.compile(r"(으로|에서|에게|에는|에|의|을|를|은|는|이|가|와|과|로|도)$")
# 질문에 거의 항상 붙는 상투 단어. 이것 때문에 다른 문제도 비슷해 보이고, 같은 문제는 덜 비슷해 보인다.
_BOILER = {"다음", "중", "제시된", "자료", "바탕", "근거하여", "근거한", "따르면", "대한", "관한", "설명",
           "가장", "옳은", "올바른", "적절한", "적절하게", "알맞은", "해당하는", "것", "것은", "것을",
           "고르면", "무엇인가", "무엇인", "무엇", "고른", "모두"}


def _n(s: str) -> str:
    return _DROP.sub("", s).lower()


def _nq(question: str) -> set[str]:
    """질문 비교용: 괄호 속 영어 병기, 단어 끝 조사, 상투 단어를 빼고 핵심 단어 묶음만 남긴다.
    예) "에포크(epoch)의 정의에 해당하는 것은?" → {"에포크", "정의"}
        "에포크의 정의로 옳은 것은?"           → {"에포크", "정의"}
    """
    words = []
    for w in _TOKEN.findall(_PAREN.sub(" ", question).lower()):
        if w in _BOILER or _JOSA.fullmatch(w):          # 상투 단어, 또는 홀로 남은 조사("의")
            continue
        stem = _JOSA.sub("", w)
        if len(stem) >= 2:
            w = stem
        if w not in _BOILER and w not in ("해당하",):
            words.append(w)
    return set(words)


def _jaccard(a: set[str], b: set[str]) -> float:
    """두 단어 묶음에서 겹치는 단어 비율. 글자 단위 비교와 달리 '배치'와 '미니배치'를 다른 단어로 본다."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    m = SequenceMatcher(None, a, b, autojunk=False)
    if m.real_quick_ratio() < 0.5:        # 길이 차이만으로도 확실히 다르면 빨리 넘어간다
        return 0.0
    return m.ratio()


def _shared(a: str, b: str) -> float:
    """짧은 쪽 문자열 가운데 SHARE_BLOCK자 이상 덩어리로 상대와 겹치는 글자의 비율."""
    if not a or not b:
        return 0.0
    blocks = SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
    return sum(m.size for m in blocks if m.size >= SHARE_BLOCK) / min(len(a), len(b))


def duplicate_reason(question: str, choices, evidence: str, previous: list[dict], answer: str = "") -> str:
    """previous: [{"question", "choices", "evidence", "answer"(단답 정답, 없을 수 있음)}].
    중복이면 사유, 아니면 빈 문자열.
    4. (단답형) 정답이 같음: 질문이 달라도 답이 같으면 같은 개념을 다시 묻는 것으로 본다
       (실측: 선형 회귀·로지스틱 회귀 문제의 답이 둘 다 '볼록 함수')."""
    q, ev = _nq(question), _n(evidence)
    ch = _n("".join(sorted(choices)))
    ans = _n(answer)
    for p in previous:
        if ans and _n(p.get("answer") or "") == ans:
            return f"같은 정답({answer}): {p['question'][:30]}"
        jq = _jaccard(q, _nq(p["question"]))
        if jq >= Q_SIM:
            return f"질문 유사: {p['question'][:30]}"
        pev = _n(p["evidence"])
        if jq >= PAIR_Q_SIM and _shared(ev, pev) >= PAIR_EV_SHARE:
            return f"질문·근거 겹침: {p['question'][:30]}"
        if _ratio(ev, pev) >= EV_SIM or (min(len(ev), len(pev)) >= EV_MIN_CONTAIN and (ev in pev or pev in ev)):
            return f"같은 근거: {p['question'][:30]}"
        if _ratio(ch, _n("".join(sorted(p["choices"])))) >= CH_SIM:
            return f"보기 구성 유사: {p['question'][:30]}"
    return ""
