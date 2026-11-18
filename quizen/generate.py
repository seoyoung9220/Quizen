"""B-3: 청크별 문제 수 배분과 문제 생성.

검증(evidence 대조, 정답 숨김 풀이)은 B-4·B-5에서 이 결과 위에 얹는다.
"""
import json
import random
import re
import time
from dataclasses import dataclass, field

from pydantic import ValidationError

from . import config, prompts
from .llm import ContextOverflow, client, fit_context
from .schemas import BATCH_MODELS, GeneratedBatch, GeneratedQuestion, batch_schema

MAX_PER_CHUNK = 6          # 한 번에 너무 많이 시키면 품질이 떨어지고 같은 개념을 반복하기 쉽다
PARSE_RETRIES = 2          # JSON/구조 검증 실패 시 재시도 횟수

_KO = re.compile(r"[가-힣]{2,}")
_EN = re.compile(r"[A-Za-z]{3,}")


# ---------------------------------------------------------------- 배분
def info_score(chunk: dict, pages: list[dict]) -> int:
    """청크의 정보량 점수 = 이 청크에서 새로 시작하는 페이지들에 나오는 서로 다른 내용어 수.

    - overlap으로 겹친 앞부분은 앞 청크 몫이라 뺀다.
    - 단원 제목만 있는 페이지나 깨진 수식 기호는 단어로 잡히지 않아 점수가 거의 오르지 않는다.
    - 글자 수 대신 '서로 다른 단어 수'를 쓰는 이유: 같은 말을 반복하는 페이지보다
      새 개념이 많은 페이지에서 문제를 더 낼 수 있기 때문이다.
    """
    new_pages = [p for p in pages if chunk["new_page_start"] <= p["page"] <= chunk["page_end"]]
    words: set[str] = set()
    for p in new_pages:
        words.update(_KO.findall(p["text"]))
        words.update(w.lower() for w in _EN.findall(p["text"]))
    return len(words)


USAGE_DECAY = 0.3          # 이전에 1번 쓴 청크는 가중치 ×0.3, 2번이면 ×0.09 …


def chunk_weights(chunks: list[dict], pages: list[dict], usage: dict[str, int] | None = None) -> list[float]:
    """정보량 × (이전 사용 횟수만큼 줄어드는 계수). 많이 출제한 구간은 다음에 덜 뽑힌다."""
    from .history import chunk_key   # 구간 이름 규칙을 한곳(history)에서만 정한다
    usage = usage or {}
    return [info_score(c, _pages_of(c, pages)) * USAGE_DECAY ** usage.get(chunk_key(c), 0) for c in chunks]


def _pages_of(chunk: dict, pages: list[dict]) -> list[dict]:
    """여러 PDF를 섞으면 페이지 번호가 겹치므로, 청크와 같은 PDF의 페이지만 고른다."""
    if chunk.get("doc_id") is None:
        return pages
    return [p for p in pages if p.get("doc_id") == chunk["doc_id"]]


def allocate(chunks: list[dict], pages: list[dict], total: int,
             usage: dict[str, int] | None = None, rng: random.Random | None = None,
             balance_docs: bool = False) -> list[int]:
    """청크별 문제 수를 정한다. 가중치 = 정보량 × 사용 이력 감소 계수.

    balance_docs=True (여러 PDF를 섞을 때 'PDF별로 고르게'):
      먼저 PDF마다 문제 수를 똑같이 나누고(남는 것은 무작위로 1개씩), 각 PDF 안에서는 아래 규칙으로 나눈다.
      예) PDF 2개에 5문제 → 3/2 또는 2/3. 글자가 적은 PDF도 정해진 몫만큼 출제한다.

    - 문제 수가 청크 수 이하: 가중치에 비례하는 확률로 서로 다른 청크를 골라 1문제씩.
      (rng가 없으면 가중치가 큰 순서대로. 예전처럼 매번 같은 구간만 나오는 것을 막으려고
      앱에서는 rng를 넘겨 회차마다 다른 구간이 섞이게 한다.)
    - 문제 수가 청크 수보다 많음: 가중치 비례로 나누고 정수로 맞춘다 (최대 잔여법).
      예) 비율이 0.25/0.25/0.25/0.125/0.125 이고 20문제면 5/5/5/2.5/2.5
          → 정수 부분 5/5/5/2/2 = 19, 소수점이 큰 순서로 1개 더 → 5/5/5/3/2
    """
    weights = chunk_weights(chunks, pages, usage)
    docs = list(dict.fromkeys(c.get("doc_id") for c, w in zip(chunks, weights) if w > 0))
    if balance_docs and len(docs) > 1:
        share = [total // len(docs)] * len(docs)
        extra = list(range(len(docs)))
        (rng or random.Random(0)).shuffle(extra)
        for k in extra[: total - sum(share)]:
            share[k] += 1
        alloc = [0] * len(chunks)
        for d, n in zip(docs, share):
            idx = [i for i, c in enumerate(chunks) if c.get("doc_id") == d]
            sub = allocate([chunks[i] for i in idx], pages, n, usage, rng)
            for i, k in zip(idx, sub):
                alloc[i] = k
        return alloc

    usable = [i for i, w in enumerate(weights) if w > 0]
    if total <= len(usable):
        if rng is None:
            picked = sorted(usable, key=lambda i: weights[i], reverse=True)[:total]
        else:
            # 가중치 비례 비복원 추출 (Efraimidis–Spirakis): 키 = u^(1/w) 가 큰 순서
            picked = sorted(usable, key=lambda i: rng.random() ** (1 / weights[i]), reverse=True)[:total]
        return [1 if i in picked else 0 for i in range(len(chunks))]

    s = sum(weights) or 1
    raw = [total * x / s for x in weights]
    alloc = [min(int(r), MAX_PER_CHUNK) for r in raw]
    order = sorted(range(len(chunks)), key=lambda i: raw[i] - int(raw[i]), reverse=True)
    while sum(alloc) < total:
        progressed = False
        for i in order:
            if sum(alloc) >= total:
                break
            if alloc[i] < MAX_PER_CHUNK:
                alloc[i] += 1
                progressed = True
        if not progressed:      # 모든 청크가 상한에 걸림: 요청 수를 채울 수 없음
            break
    return alloc


# ---------------------------------------------------------------- 생성
@dataclass
class GenStats:
    calls: int = 0
    parse_failures: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    timings: list[dict] = field(default_factory=list)   # 호출별 읽기/쓰기 시간 (속도 진단용)


def call_timing(kind: str, r, wall: float) -> dict:
    """Ollama 응답의 시간 정보를 정리한다. 읽기(prompt) 시간이 길면 자료를 처음부터 다시 읽은 것이다."""
    ns = 1e9
    return {"kind": kind, "wall": round(wall, 2),
            "prompt_tokens": r.prompt_eval_count, "prompt_s": round((r.prompt_eval_duration or 0) / ns, 2),
            "out_tokens": r.eval_count, "out_s": round((r.eval_duration or 0) / ns, 2),
            "load_s": round((r.load_duration or 0) / ns, 2)}


EXISTING_MAX = 20         # 프롬프트에 넣는 '이미 출제된 문제' 최대 개수 (≈1,000토큰)


def _existing_block(existing: list[str]) -> str:
    if not existing:
        return ""
    items = "\n".join(f"- {q}" for q in existing[-EXISTING_MAX:])   # 너무 길어지지 않게 최근 것만
    return prompts.GEN_EXISTING.format(items=items)


# 유형별 최대 출력 토큰: 문제 1개 길이가 다르다 (서술형은 모범 답안·채점 기준 포함)
NUM_PREDICT = {"mcq": (400, 300), "short": (250, 200), "essay": (700, 300)}


# '상' 문제는 지문·보기가 길다. C-9 실측에서 '상' 출력이 한도에서 잘려 JSON이 4/16번 깨졌다.
HARD_FACTOR = 1.5


def num_predict_for(qtype: str, num: int, difficulty: str = "중") -> int:
    per, base = NUM_PREDICT[qtype]
    if difficulty == "상":
        per = int(per * HARD_FACTOR)
    return per * num + base


def build_fitting_messages(chunk_text: str, difficulty: str, num: int, existing: list[str],
                           conditions: str, num_predict: int, qtype: str = "mcq") -> list[dict]:
    """컨텍스트에 들어가도록 '이미 출제된 문제' 목록을 오래된 것부터 줄인다. 그래도 넘치면 ContextOverflow."""
    keep = existing[-EXISTING_MAX:]
    while True:
        messages = prompts.build_gen_messages(chunk_text, difficulty, num, _existing_block(keep), conditions, qtype)
        try:
            fit_context(messages, num_predict)
            return messages
        except ContextOverflow:
            if not keep:
                raise
            keep = keep[len(keep) // 2 + 1:] if len(keep) > 1 else []


def generate_for_chunk(chunk_text: str, difficulty: str, num: int,
                       existing: list[str], stats: GenStats,
                       model: str | None = None, conditions: str = "", qtype: str = "mcq") -> list:
    """청크 하나에서 num개 이하의 문제를 만든다 (qtype: mcq|short|essay). 구조 검증에 실패하면 최대 2회 다시 요청한다."""
    num_predict = num_predict_for(qtype, num, difficulty)
    messages = build_fitting_messages(chunk_text, difficulty, num, existing, conditions, num_predict, qtype)
    batch_model = BATCH_MODELS[qtype]
    for attempt in range(PARSE_RETRIES + 1):
        t = time.time()
        r = client().chat(
            model=model or config.MODEL,
            messages=messages,
            format=batch_schema(num, qtype),
            think=False,
            keep_alive=config.KEEP_ALIVE,
            options={"num_ctx": config.NUM_CTX, "temperature": 0.5, "num_predict": num_predict},
        )
        stats.calls += 1
        stats.seconds += time.time() - t
        stats.timings.append(call_timing("생성", r, time.time() - t))
        if r.done_reason == "length":            # 출력 한도에서 잘림 (진단용 기록)
            stats.errors.append(f"attempt {attempt + 1}: 출력 한도 {num_predict}토큰에서 잘림")
        try:
            batch = batch_model.model_validate_json(r.message.content)
        except ValidationError as e:
            # 문제 하나만 잘못돼도 전체가 실패하므로, 개별 문제 단위로 다시 살려 본다
            stats.parse_failures += 1
            stats.errors.append(f"attempt {attempt + 1}: " + "; ".join(
                f"{'.'.join(map(str, er['loc']))}: {er['msg']}" for er in e.errors()[:3]))
            good = _salvage(r.message.content, qtype)
            if good:
                return [q for q in good if q.difficulty == difficulty][:num]
            continue
        return [q for q in batch.questions if q.difficulty == difficulty][:num]
    return []


def _complete_items(content: str) -> list:
    """출력 한도에서 잘린 JSON에서 끝까지 쓰인 문제 객체만 꺼낸다.
    예) {"questions":[{...완성...},{...완성...},{"question":"잘린  → 앞의 2개"""
    start = content.find("[", content.find('"questions"'))
    if start < 0:
        return []
    dec, i, items = json.JSONDecoder(), start + 1, []
    while True:
        while i < len(content) and content[i] in " \t\r\n,":
            i += 1
        if i >= len(content) or content[i] != "{":
            return items
        try:
            obj, i = dec.raw_decode(content, i)
        except json.JSONDecodeError:
            return items
        items.append(obj)


def _salvage(content: str, qtype: str = "mcq") -> list:
    item_model = BATCH_MODELS[qtype].model_fields["questions"].annotation.__args__[0]
    try:
        items = json.loads(content).get("questions", [])
    except json.JSONDecodeError:
        items = _complete_items(content)       # 잘린 출력: 완성된 문제까지는 살린다
    except AttributeError:
        return []
    out = []
    for it in items:
        try:
            out.append(item_model.model_validate(it))
        except ValidationError:
            pass
    return out
