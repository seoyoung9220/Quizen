"""오답 풀이 (5-5, 5-6 프롬프트). 화면에 글자가 바로바로 나오도록 스트리밍으로 받는다."""
from typing import Iterator

from . import config, prompts
from .llm import TokenCounter, client, fit_context
from .quiz import QuestionResult

CONTEXT_MAX_TOKENS = 3000   # 관련 페이지 원문 상한 (근거 페이지가 여러 장이어도 이 안에서 자름)


def _label(i: int | None, choices) -> str:
    if i is None:
        return "(선택하지 않음)"
    return f"{i + 1}) {choices[i]}"


def page_context(page_texts: dict[int, str], r: QuestionResult) -> str:
    """근거가 나온 페이지들의 원문. 토큰 상한을 넘으면 뒤쪽 페이지부터 뺀다."""
    pages = list(dict.fromkeys([r.page, *r.evidence_pages]))      # 순서 유지, 중복 제거
    count = TokenCounter()
    parts: list[str] = []
    for p in pages:
        text = page_texts.get(p, "")
        if not text:
            continue
        cand = "\n\n".join(parts + [f"[{p}페이지]\n{text}"])
        if parts and count(cand) > CONTEXT_MAX_TOKENS:
            break
        parts.append(f"[{p}페이지]\n{text}")
    return "\n\n".join(parts) or "(페이지 원문 없음)"


def build_explain_messages(r: QuestionResult, context: str) -> list[dict]:
    if r.qtype == "mcq":
        choices = "\n".join(f"{i + 1}) {c}" for i, c in enumerate(r.choices))
        correct, mine = _label(r.answer_index, r.choices), _label(r.user_index, r.choices)
    else:
        # 단답·서술: 보기 대신 정답(모범 답안)과 내가 쓴 답을 그대로 넘긴다
        choices = "(주관식 — 보기 없음)"
        correct = r.answer_text if r.qtype == "short" else f"(모범 답안) {r.answer_text}"
        mine = r.user_text.strip() or "(답을 쓰지 않음)"
    user = prompts.EXPLAIN_USER.format(
        context=context,
        evidence=r.evidence,
        question=r.question,
        choices=choices,
        correct_choice=correct,
        user_choice=mine,
        difficulty=r.difficulty,
        source_document=r.source_document,
        page=r.page,
    )
    return [{"role": "system", "content": prompts.EXPLAIN_SYSTEM},
            {"role": "user", "content": user}]


def explain_stream(r: QuestionResult, page_texts: dict[int, str] | None = None) -> Iterator[str]:
    context = page_context(page_texts or {}, r)
    messages = build_explain_messages(r, context)
    fit_context(messages, 800)        # 넘치면 ContextOverflow
    stream = client().chat(
        model=config.MODEL,
        messages=messages,
        think=False,
        stream=True,
        keep_alive=config.KEEP_ALIVE,
        # temperature 0: 자료에 없는 말을 지어낼 여지를 줄인다 (0.3에서 약어 뜻을 지어낸 사례가 있었음)
        options={"num_ctx": config.NUM_CTX, "temperature": 0, "num_predict": 800},
    )
    for part in stream:
        if part.message.content:
            yield part.message.content
