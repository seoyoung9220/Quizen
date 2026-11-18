"""B-4: 코드 검증. LLM에게 맡기지 않는다.

evidence가 청크 원문에 실제로 있는지 확인하고, 있으면 그 위치의 [[PAGE n]]으로 페이지를 정한다.
LLM이 낸 페이지나 파일명은 받지도 않는다. 페이지와 source_document는 여기서 코드가 채운다.

비교 규칙
- 공백, 줄바꿈, 글머리 기호(•, ● 등), 따옴표 종류는 서식일 뿐이라 양쪽에서 지우고 비교한다.
  예) 원문 "● 한편으로는 지역 최소값에서" ↔ evidence "한편으로는 지역최소값에서" → 일치
- evidence 전체가 원문의 한 구간과 일치하면 통과.
- 아니면 글머리 기호·문장 끝·줄바꿈 기준으로 나눠, 나뉜 구간이 '모두' 원문에 있으면 통과.
  (모델이 떨어진 두 문장을 이어 붙인 경우. 각 구간은 여전히 글자 그대로 원문에 있어야 한다.)
- 하나라도 원문에 없으면 환각으로 보고 폐기한다.
"""
import re
from dataclasses import asdict, dataclass, field

from .schemas import GeneratedEssay, GeneratedQuestion, GeneratedShort

_MARKER = re.compile(r"\[\[PAGE (\d+)\]\]")
# 비교할 때 무시하는 문자: 공백류, 글머리 기호, 따옴표
_IGNORE = set(" \t\r\n 　•●○◦▪■□◆◇▶►➢✓※·∙'\"‘’“”`")
# evidence를 구간으로 나누는 경계: 글머리 기호, 줄바꿈, 문장 끝(. ? ! 다음 공백)
# [추가] 말줄임('...', '…')도 경계로 본다: 모델이 근거 중간·끝을 '...'로 줄여 쓰는 경우 (C-9 실측)
_SPLIT = re.compile(r"[•●○◦▪■□◆◇▶►➢✓※\n]+|\.{2,}|…|(?<=[.?!])\s+")
MIN_SEGMENT = 4        # 이보다 짧은 조각(정규화 후 글자 수)은 구간으로 보지 않는다
MIN_EVIDENCE = 8       # evidence 전체가 이보다 짧으면 근거로 인정하지 않는다 (예: "학습률")
# 수식 의존 판정: PDF 수식은 추출하며 위·아래첨자가 깨진다 (예: a²b → "𝑎:𝑏").
# 수학용 문자(U+1D400~1D7FF, 𝑎 𝜃 𝑿 등)가 evidence의 이 비율 이상이면 깨진 수식을 근거로 삼은 것으로 보고 폐기한다.
MATH_RATIO = 0.15


def math_ratio(s: str) -> float:
    body = normalize(s)
    if not body:
        return 0.0
    return sum(0x1D400 <= ord(ch) <= 0x1D7FF for ch in body) / len(body)


def normalize(s: str) -> str:
    return "".join(ch for ch in s if ch not in _IGNORE)


class ChunkIndex:
    """청크 원문을 정규화하면서, 정규화된 글자마다 원래 페이지 번호를 기억해 둔다."""

    def __init__(self, chunk_text: str):
        chars: list[str] = []
        pages: list[int] = []
        page = 0
        pos = 0
        for m in _MARKER.finditer(chunk_text):
            self._add(chunk_text[pos:m.start()], page, chars, pages)
            page = int(m.group(1))
            pos = m.end()
        self._add(chunk_text[pos:], page, chars, pages)
        self.text = "".join(chars)
        self.pages = pages

    @staticmethod
    def _add(seg: str, page: int, chars: list[str], pages: list[int]) -> None:
        for ch in seg:
            if ch not in _IGNORE:
                chars.append(ch)
                pages.append(page)

    def find_pages(self, needle_norm: str) -> list[int] | None:
        """정규화된 문자열이 있으면 그 구간이 걸친 페이지들(시작 페이지가 맨 앞), 없으면 None."""
        i = self.text.find(needle_norm)
        if i < 0:
            return None
        span = self.pages[i:i + len(needle_norm)]
        return [span[0]] + sorted(set(span) - {span[0]})


@dataclass
class EvidenceCheck:
    ok: bool
    page: int | None
    pages: list[int]
    mode: str          # "full" | "segments" | "fail"
    reason: str = ""


def check_evidence(evidence: str, index: ChunkIndex) -> EvidenceCheck:
    whole = normalize(evidence)
    if len(whole) < MIN_EVIDENCE:
        return EvidenceCheck(False, None, [], "fail", f"evidence가 너무 짧음({len(whole)}자)")
    mr = math_ratio(evidence)
    if mr >= MATH_RATIO:
        return EvidenceCheck(False, None, [], "fail", f"깨진 수식 의존 (수학 기호 {mr:.0%})")

    span = index.find_pages(whole)
    if span is not None:
        return EvidenceCheck(True, span[0], sorted(set(span)), "full")

    segments = [normalize(s) for s in _SPLIT.split(evidence) if s]
    segments = [s for s in segments if len(s) >= MIN_SEGMENT]
    if len(segments) == 1 and re.search(r"\.{2,}|…", evidence):   # '앞부분...'처럼 한 조각 + 말줄임
        sp = index.find_pages(segments[0])
        if sp is not None and len(segments[0]) >= MIN_EVIDENCE:
            return EvidenceCheck(True, sp[0], sorted(set(sp)), "segments")
    if len(segments) < 2:
        return _pieces(whole, index) or EvidenceCheck(False, None, [], "fail", "원문에 없음")
    found: list[int] = []
    for s in segments:
        sp = index.find_pages(s)
        if sp is None:
            pc = _pieces(whole, index)
            if pc:
                return pc
            return EvidenceCheck(False, None, [], "fail", f"원문에 없는 구간: {s[:30]}")
        found.extend(sp)
    return EvidenceCheck(True, found[0], sorted(set(found)), "segments")


# [추가] 조각 모드: 모델이 원문을 그대로 옮기면서 중간의 짧은 부분만 '빼먹은' 경우를 받아 준다.
# (C-9 실측: '모델 파라미터 𝜽를' → '모델 파라미터를' 처럼 수학 기호를 빼거나, 두 문장 사이의 그림 라벨
#  '전역 최소값 / 비용 / 평지 …'를 건너뛰고 이어 쓴 근거가 환각으로 폐기됨)
# evidence의 모든 글자는 원문에 순서대로 있어야 한다. 원문 쪽에서만 PIECE_GAP자 이하를 건너뛸 수 있고,
# 조각은 MAX_PIECES개 이하, 각 조각은 MIN_PIECE자 이상이어야 한다. 모델이 덧붙이거나 바꾼 글자는 여전히 폐기된다.
PIECE_GAP = 60
MIN_PIECE = 8
MAX_PIECES = 4
# 건너뛴 원문에 부정어가 있으면 받지 않는다 (예: '변하지 않음'에서 '않'을 빼 뜻이 뒤집히는 경우)
_NEGATION = ("않", "없", "못", "아니", "말고", "not", "n't", "no")


def _is_math(ch: str) -> bool:
    return 0x1D400 <= ord(ch) <= 0x1D7FF


def _longest_prefix(ev: str, hay: str, start: int, end: int) -> tuple[int, int]:
    """ev의 가장 긴 앞부분이 hay[start:end]에서 처음 나오는 (위치, 길이). 없으면 (-1, 0)."""
    lo, hi, best = 1, len(ev), (-1, 0)
    while lo <= hi:                       # 앞부분이 있으면 더 짧은 앞부분도 있다 → 이분 탐색
        mid = (lo + hi) // 2
        i = hay.find(ev[:mid], start, end)
        if i >= 0:
            best = (i, mid)
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _pieces(whole: str, index: ChunkIndex) -> EvidenceCheck | None:
    keep = [i for i, ch in enumerate(index.text) if not _is_math(ch)]
    hay = "".join(index.text[i] for i in keep)
    ev = re.sub(r"\.{2,}|…", "", "".join(ch for ch in whole if not _is_math(ch)))
    pos, cur, spans = 0, 0, []
    while pos < len(ev):
        if not spans:
            i, n = _longest_prefix(ev[pos:], hay, 0, len(hay))
        else:
            i, n = _longest_prefix(ev[pos:], hay, cur, min(len(hay), cur + PIECE_GAP + len(ev) - pos))
            if i - cur > PIECE_GAP:
                return None
        if n < MIN_PIECE or len(spans) >= MAX_PIECES:
            return None
        if spans:
            skipped = "".join(index.text[k] for k in range(keep[cur - 1] + 1, keep[i]))
            if any(w in skipped.lower() for w in _NEGATION):
                return None
        spans.append((i, n))
        pos, cur = pos + n, i + n
    pages = [index.pages[keep[j]] for i, n in spans for j in range(i, i + n)]
    return EvidenceCheck(True, pages[0], sorted(set(pages)), "pieces")


@dataclass
class ValidQuestion:
    """코드 검증을 통과한 문제. page·source_document는 코드가 채운 값이다."""
    question: str
    choices: list[str]
    answer_index: int
    difficulty: str
    evidence: str
    page: int
    evidence_pages: list[int]
    evidence_mode: str
    source_document: str
    chunk_id: int
    qtype: str = "mcq"                                    # mcq | short | essay
    answer_text: str = ""                                 # 단답 정답 / 서술 모범 답안
    accepted: list = field(default_factory=list)          # 단답 인정 답안 (자료에 나온 표기만)
    rubric: list = field(default_factory=list)            # 서술 채점 기준 [{point, evidence, page}]

    def to_dict(self) -> dict:
        return asdict(self)


def answer_norm(s: str) -> str:
    """단답 비교용: 공백·문장부호·대소문자를 무시한다. 예) 'Gradient Descent' == 'gradientdescent'."""
    return re.sub(r"[\W_]+", "", s, flags=re.UNICODE).lower()


ADJ_GAP = 2   # 정답과 다른 표기 사이에 허용하는 글자 수 (공백·괄호는 이미 빠진 상태라 '즉', '=' 정도)


def _adjacent(text: str, ans: str, alt: str) -> bool:
    """정규화된 자료에서 alt가 ans 바로 뒤에서 시작하거나 바로 앞에서 끝나는 곳이 있는지."""
    start = text.find(ans)
    while start >= 0:
        end = start + len(ans)
        after = text.find(alt, end, end + ADJ_GAP + len(alt))
        if after != -1 and after - end <= ADJ_GAP:
            return True
        lo = max(0, start - ADJ_GAP - len(alt))
        before = text.rfind(alt, lo, start)
        if before != -1 and start - (before + len(alt)) <= ADJ_GAP:
            return True
        start = text.find(ans, start + 1)
    return False


# 단답 정답은 용어 하나다. 답할 것이 여러 개면 용어를 쉼표로 구분한다 (사용자 결정 2026-11-18:
# "단답형은 한 단어, 여러 개면 콤마로 구분. 문장은 서술형").
# '경사 하강법'처럼 띄어 쓴 복합 용어는 한 단어로 본다 → 항목당 3어절·20자까지. 괄호 속 다른 표기는 빼고 센다.
# (실측: '선형 회귀가 아닌 지역 최소값이 존재하는 비용 함수의 경우', '지역 최소값에서 빠져나오기가 더 힘들 수 있음'
#  같은 설명구·문장이 정답으로 나왔다)
SHORT_MAX_WORDS = 3
SHORT_MAX_CHARS = 20
SHORT_MAX_ITEMS = 4
_ITEM_SPLIT = re.compile(r"\s*[,，、]\s*")
# 문장·설명구의 흔적: 서술어로 끝남, 또는 중간 어절이 목적격·부사격 조사로 끝남 ('훈련셋을 반복')
_SENTENCE_END = re.compile(r"(있음|없음|않음|한다|된다|이다|하다|되다|것|경우|때)$")
_CLAUSE_PARTICLE = re.compile(r"(을|를|에서|으로)$")


def split_items(answer: str) -> list[str]:
    """단답 정답·학생 답을 쉼표로 나눈다. 빈 항목은 버린다."""
    return [x.strip() for x in _ITEM_SPLIT.split(answer or "") if x.strip()]


def _core(item: str) -> str:
    return re.sub(r"\([^)]*\)", " ", item).strip()


def short_too_long(answer: str) -> bool:
    """항목(쉼표로 나눈 각 용어) 하나라도 용어로 보기에 너무 길면 True."""
    return any(len(_core(x).split()) > SHORT_MAX_WORDS or len(_core(x)) > SHORT_MAX_CHARS
               for x in split_items(answer) or [answer])


def short_is_sentence(answer: str) -> bool:
    """항목 하나라도 문장·설명구로 보이면 True ('빠져나오기 힘들 수 있음', '훈련셋을 반복하는 것')."""
    for x in split_items(answer):
        words = _core(x).split()
        if not words:
            continue
        if _SENTENCE_END.search(words[-1]) and len(words) > 1:
            return True
        if any(_CLAUSE_PARTICLE.search(w) for w in words[:-1]):
            return True
    return False


# 표현만 봐도 틀린 티가 나는 오답 보기 (C-9 이후 측정: '학습률이 자동으로 조정되어', '항상 빠르다')
# 자료에 그 표현이 실제로 나오면(예: '반드시 같은 스케일') 자료를 잘못 읽은 그럴듯한 오답일 수 있으므로 허용한다.
TELLTALE = ("자동으로", "즉시", "항상", "무조건", "절대로", "전혀", "완전히", "반드시")


# 근거에 깨진 수식 조각이 있는데 문제가 수식·행렬 계산을 묻는 경우 (실측: 근거 ';𝜃 = 𝑿& - 𝑿 *'로
# 정규 방정식의 행렬 곱 조합을 묻는 문제 — 자료 글자만으로는 풀 수 없고 바깥 지식이 필요했다).
# 수학 문자 옆에 연산 기호가 붙어 있으면 깨진 수식 조각으로 본다. 문장 속 기호 하나('파라미터 𝜽를 찾는')는 해당하지 않는다.
_M = "\U0001D400-\U0001D7FF"
_BROKEN_FORMULA = re.compile(rf"[{_M}]\s*[=&*^+/]|[=&*^/]\s*[{_M}]")
FORMULA_WORDS = ("행렬", "전치", "수식", "공식", "계산식")   # '식은'은 '방정식은'에도 걸려서 넣지 않음


def leans_on_broken_formula(evidence: str, asked: str) -> bool:
    return bool(_BROKEN_FORMULA.search(evidence)) and any(w in asked for w in FORMULA_WORDS)


def telltale_choice(choices: list[str], answer_index: int, chunk_text: str) -> str:
    """오답 보기에 자료에 없는 단정 표현이 있으면 그 표현을 돌려준다. 없으면 ''."""
    src = answer_norm(chunk_text)
    for i, c in enumerate(choices):
        if i == answer_index:
            continue
        for w in TELLTALE:
            if w in c and answer_norm(w) not in src:
                return w
    return ""


# 숫자 정답이 자료에서 범위의 한쪽 끝인 경우 (실측 2번: 자료 '약 5.3에서 8배'인데 정답 '5.3')
# [추가] 숫자 뒤에 괄호 설명이 끼어 있어도 범위로 본다 (실측: '5.3 (=22.4) 에서 8(=23) 배'에서 정답 '5.3' 통과)
_PAREN = r"(\s*\([^)]*\))?"
_RANGE_AFTER = _PAREN + r"\s*(에서|부터|~|∼|-|–)\s*\d"
_RANGE_BEFORE = r"\d" + _PAREN + r"\s*(에서|부터|~|∼|-|–)\s*"


def is_partial_range(answer: str, evidence: str) -> bool:
    a = answer.strip()
    if not re.fullmatch(r"[\d.,]+\s*\S{0,2}", a):        # 숫자(+단위 2글자 이하)인 정답만 본다
        return False
    num = re.match(r"[\d.,]+", a).group()
    for m in re.finditer(r"(?<![\d.])" + re.escape(num) + r"(?![\d])", evidence):
        if re.match(_RANGE_AFTER, evidence[m.end():]) or re.search(_RANGE_BEFORE + r"$", evidence[:m.start()]):
            return True
    return False


def validate(q, index: ChunkIndex, source_document: str,
             chunk_id: int) -> tuple[ValidQuestion | None, str]:
    """모델이 만든 문제를 코드로 검증한다. 유형별 추가 검사:
    - 단답형: 정답이 근거 문장 안에 글자 그대로 있어야 한다. 인정 답안은 자료에 나오는 표기만 남긴다.
    - 서술형: 채점 기준마다 근거가 원문에 있어야 한다 (하나라도 없으면 폐기)."""
    chk = check_evidence(q.evidence, index)
    if not chk.ok:
        return None, chk.reason
    base = dict(question=q.question, difficulty=q.difficulty, evidence=q.evidence, page=chk.page,
                evidence_pages=chk.pages, evidence_mode=chk.mode, source_document=source_document,
                chunk_id=chunk_id)

    asked = " ".join([q.question, *getattr(q, "choices", []), getattr(q, "answer", ""), getattr(q, "model_answer", "")])
    if leans_on_broken_formula(q.evidence, asked):
        return None, "깨진 수식에 기대는 문제 (수식·행렬을 묻는데 근거의 수식이 깨져 있음)"

    if isinstance(q, GeneratedQuestion):
        w = telltale_choice(q.choices, q.answer_index, index.text)
        if w:
            return None, f"티 나는 오답 보기('{w}')"
        return ValidQuestion(choices=q.choices, answer_index=q.answer_index, **base), ""

    if isinstance(q, GeneratedShort):
        items = split_items(q.answer)
        if not 1 <= len(items) <= SHORT_MAX_ITEMS:
            return None, f"단답 항목 수가 1~{SHORT_MAX_ITEMS}개가 아님: {q.answer}"
        if short_too_long(q.answer):
            return None, f"단답 정답이 너무 김: {q.answer}"
        if short_is_sentence(q.answer):
            return None, f"단답 정답이 용어가 아니라 문장: {q.answer}"
        norms = [answer_norm(x) for x in items]
        if len(set(norms)) != len(norms):
            return None, f"단답 항목이 겹침: {q.answer}"
        ev_norm = answer_norm(q.evidence)
        for x, n in zip(items, norms):
            if len(n) < 1 or n not in ev_norm:
                return None, f"단답 정답이 근거 문장에 없음: {x}"
            if is_partial_range(x, q.evidence):
                return None, f"단답 정답이 범위의 한쪽 끝만 씀: {x}"
        text_norm = answer_norm(index.text)
        accepted, seen = [], set(norms)
        for a in q.accepted:
            na = answer_norm(a)
            # 인정 답안 = 같은 말의 다른 표기. 강의자료에서는 "에포크 (epoch)", "확률적 경사 하강법(SGD)"처럼
            # 정답 바로 앞뒤에 붙어 나온다. 자료의 다른 곳에 나오는 말(예: 반대 개념)은 받지 않는다.
            # (실측 버그: 정답 '전체 훈련 세트'의 인정 답안으로 '하나의 샘플'이 들어가 오답이 정답 처리됨)
            if na and na not in seen and any(_adjacent(text_norm, n, na) for n in norms):
                accepted.append(a)
                seen.add(na)
        return ValidQuestion(choices=[], answer_index=0, qtype="short", answer_text=q.answer,
                             accepted=accepted, **base), ""

    if isinstance(q, GeneratedEssay):
        rubric = []
        pages = set(chk.pages)
        for i, item in enumerate(q.rubric):
            rc = check_evidence(item.evidence, index)
            if not rc.ok:
                return None, f"채점 기준 {i + 1}의 근거가 원문에 없음: {rc.reason}"
            rubric.append({"point": item.point.strip(), "evidence": item.evidence, "page": rc.page})
            pages.update(rc.pages)
        base["evidence_pages"] = sorted(pages)
        return ValidQuestion(choices=[], answer_index=0, qtype="essay", answer_text=q.model_answer,
                             rubric=rubric, **base), ""

    return None, f"알 수 없는 문제 형식: {type(q).__name__}"
