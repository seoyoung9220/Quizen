"""문제 세트 만들기:
배분(정보량 × 사용 이력) → 청크별 생성 → 구조 검증 → evidence 검증 → 중복 검사 → 정답 숨김 검증 → 부족분 재생성.

재생성 규칙: 청크마다 처음 1회 + 부족분 재생성 최대 2회. 한 구간에서 하나도 못 만들면
다른 구간 최대 2곳에서 부족분을 보충한다. 그래도 못 채우면 실제 생성 수를 그대로 알린다.
출제 이력(History)은 읽기만 한다. 결과를 이력에 남기는 것은 호출하는 쪽(앱)이 한다.
"""
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from .dedup import duplicate_reason
from .generate import MAX_PER_CHUNK, GenStats, allocate, chunk_weights, generate_for_chunk
from .history import History
from .llm import ContextOverflow
from .validate import ChunkIndex, ValidQuestion, normalize, validate

EXISTING_FROM_HISTORY = 15   # 프롬프트에 '이미 출제된 문제'로 넣을 이전 회차 문제 수 (이 청크 근거만)
FALLBACK_CHUNKS = 2          # 부족분을 채우려고 추가로 시도할 구간 수

REGEN_ROUNDS = 2

# 시간 상한: 요청 1문제당 이만큼까지만 쓴다 (최소 MIN_BUDGET초). 넘으면 새 생성 요청을 보내지 않고
# 그때까지 통과한 문제로 회차를 만든다. (C-9 실측: 수식 위주 구간 '상' 5문제가 657초 걸리고 2문제만 나옴)
SECONDS_PER_QUESTION = 120
MIN_BUDGET = 180

# 진행 상황 콜백: (단계 이름, 현재, 전체, 설명)
Progress = Callable[[str, int, int, str], None]
# B-5에서 끼워 넣을 검증 함수: 통과하면 (True, ""), 아니면 (False, 사유)
Verifier = Callable[[ValidQuestion, str], tuple[bool, str]]


@dataclass
class BuildReport:
    requested: int
    produced: int = 0
    generated: int = 0                       # 구조 검증을 통과해 모델에서 받은 문제 수
    discarded: Counter = field(default_factory=Counter)
    discard_samples: list[str] = field(default_factory=list)
    regen_rounds_used: int = 0
    gen: GenStats = field(default_factory=GenStats)
    verify_seconds: float = 0.0
    seconds: float = 0.0
    per_chunk: list[dict] = field(default_factory=list)
    chunks_used: list[dict] = field(default_factory=list)   # 문제가 1개 이상 나온 청크 (이력 기록용)
    fallback_chunks: int = 0                                 # 부족분 보충에 쓴 구간 수
    timed_out: bool = False                                  # 시간 상한에 걸려 생성을 멈췄는지
    short_to_mcq: int = 0                                    # '상' 단답이 모자라 객관식으로 대신 낸 수


def _noop(*_a) -> None:
    pass


def build_quiz(extracted: dict, total: int, difficulty: str,
               verifier: Verifier | None = None,
               progress: Progress = _noop,
               conditions: str = "",
               history: History | None = None,
               rng: random.Random | None = None,
               balance_docs: bool = False,
               qtype: str = "mcq",
               extra_prev: list[dict] | None = None,
               time_budget: float | None = None) -> tuple[list[ValidQuestion], BuildReport]:
    """extracted는 analyze_pdf 결과 또는 scope.restrict()로 범위를 좁힌 결과.

    history: 같은 PDF의 출제 이력. 많이 쓴 구간을 덜 뽑고, 이전 문제와 중복을 검사한다.
    rng: 구간 추첨용. None이면 정보량이 큰 순서대로 고른다 (테스트용으로 결과 고정).
    qtype: mcq(객관식) | short(단답형) | essay(서술형)
    extra_prev: 같은 회차에서 먼저 만든 다른 유형의 문제 (중복 검사·'이미 출제된 문제'에 포함)
    time_budget: 이 호출에 쓸 최대 초. None이면 문제당 SECONDS_PER_QUESTION초 (최소 MIN_BUDGET초)
    """
    t0 = time.time()
    if time_budget is None:
        time_budget = max(MIN_BUDGET, SECONDS_PER_QUESTION * total)
    deadline = t0 + time_budget
    chunks, pages = extracted["chunks"], extracted["pages"]
    source = extracted["source_document"]
    prev = (history.questions if history else []) + (extra_prev or [])
    alloc = allocate(chunks, pages, total, usage=history.chunk_usage if history else None, rng=rng,
                     balance_docs=balance_docs)
    rep = BuildReport(requested=total)
    kept: list[ValidQuestion] = []

    def as_dict(q: ValidQuestion) -> dict:
        return {"question": q.question, "choices": q.choices, "evidence": q.evidence,
                "answer": q.answer_text if q.qtype == "short" else ""}

    targets = [(c, n) for c, n in zip(chunks, alloc) if n > 0]
    n_steps = len(targets)

    def fill_chunk(chunk: dict, need_total: int, step: int, tag: str = "") -> list[ValidQuestion]:
        """청크 하나에서 need_total개를 채운다: 처음 1회 + 재생성 최대 REGEN_ROUNDS회."""
        index = ChunkIndex(chunk["text"])
        # 이전 회차 문제 중 근거가 이 청크에 있는 것 → 프롬프트의 '이미 출제된 문제'에 넣는다
        prev_here = [p["question"] for p in prev if normalize(p["evidence"])[:40] in index.text]
        prev_here = prev_here[-EXISTING_FROM_HISTORY:]
        got: list[ValidQuestion] = []
        # 이 구간에서 버린 문제: 다음 요청의 '이미 출제된 문제'에 넣어 같은 문제를 또 만들지 않게 한다
        # (C-9 실측: 같은 개념 문제를 재생성마다 다시 만들어 중복으로 7번 버림)
        rejected: list[str] = []
        empty_rounds = 0
        kind = {"mcq": "", "short": "[단답] ", "essay": "[서술] "}[qtype]
        label = f"{kind}{tag}청크 {step}/{n_steps} (p{chunk['page_start']}-{chunk['page_end']})"
        for rnd in range(REGEN_ROUNDS + 1):
            need = need_total - len(got)
            if need <= 0:
                break
            if time.time() > deadline:
                rep.timed_out = True
                break
            if rnd > 0:
                rep.regen_rounds_used += 1
            progress("생성", min(step, n_steps), n_steps,
                     f"{label} {need}문제" + (f" · 재생성 {rnd}회차" if rnd else ""))

            existing = prev_here + rejected + [q.question for q in kept + got]
            n_before = len(got)
            before_fail = rep.gen.parse_failures
            try:
                raw = generate_for_chunk(chunk["text"], difficulty, need, existing, rep.gen,
                                         conditions=conditions, qtype=qtype)
            except ContextOverflow as e:        # 이 청크는 컨텍스트에 안 들어감 → 다른 구간에서 보충
                rep.discarded["컨텍스트 초과"] += need
                rep.discard_samples.append(f"[컨텍스트] {e}")
                break
            rep.generated += len(raw)
            if not raw and rep.gen.parse_failures > before_fail:
                rep.discarded["구조 검증 실패"] += need

            for q in raw:
                # 여러 PDF를 섞으면 청크마다 원본 파일 이름이 다르다
                vq, why = validate(q, index, chunk.get("source_document", source), chunk["chunk_id"],
                                   difficulty=difficulty)
                if vq is None:
                    rep.discarded["evidence 원문 불일치"] += 1
                    rep.discard_samples.append(f"[evidence] {why} | {q.question[:40]}")
                    rejected.append(q.question)
                    continue
                # 중복 검사는 모델 검증보다 먼저 한다 (중복이면 검증 시간을 쓸 필요가 없음)
                dup = duplicate_reason(vq.question, vq.choices, vq.evidence,
                                       prev + [as_dict(x) for x in kept + got],
                                       answer=vq.answer_text if vq.qtype == "short" else "")
                if dup:
                    rep.discarded["중복"] += 1
                    rep.discard_samples.append(f"[중복] {dup} | {q.question[:40]}")
                    rejected.append(q.question)
                    continue
                if verifier is not None:
                    progress("검증", min(step, n_steps), n_steps, f"{label} 정답 숨김 풀이")
                    tv = time.time()
                    try:
                        ok, why = verifier(vq, chunk["text"])
                    except ContextOverflow as e:
                        ok, why = False, f"컨텍스트 초과: {e}"
                    rep.verify_seconds += time.time() - tv
                    if not ok:
                        rep.discarded["LLM 검증 탈락"] += 1
                        rep.discard_samples.append(f"[verify] {why} | {q.question[:40]}")
                        rejected.append(q.question)
                        continue
                got.append(vq)

            # 두 번 연속 한 문제도 못 건지면 이 구간은 포기하고 다른 구간에서 보충한다 (3번째 시도 시간 절약)
            empty_rounds = empty_rounds + 1 if len(got) == n_before else 0
            if empty_rounds >= 2:
                break

        rep.per_chunk.append({"chunk_id": chunk["chunk_id"], "pages": f"{chunk['page_start']}-{chunk['page_end']}",
                              "requested": need_total, "produced": len(got), "fallback": bool(tag)})
        if got and chunk not in rep.chunks_used:
            rep.chunks_used.append(chunk)
        return got

    failed: list[dict] = []
    for step, (chunk, need_total) in enumerate(targets, 1):
        got = fill_chunk(chunk, need_total, step)
        if not got:
            failed.append(chunk)
        kept.extend(got)

    # 부족분 보충: 재생성까지 실패한 구간이 있으면 다른 구간에서 대신 만든다 (최대 FALLBACK_CHUNKS개 구간).
    # 예) 수식 위주 구간에서 '상' 문제가 계속 깨진 수식을 근거로 삼아 폐기될 때.
    shortage = total - len(kept)
    if shortage > 0:
        weights = chunk_weights(chunks, pages, history.chunk_usage if history else None)
        tried = {id(c) for c in failed}
        cands = [i for i in range(len(chunks)) if weights[i] > 0 and id(chunks[i]) not in tried]
        if rng is None:
            cands.sort(key=lambda i: weights[i], reverse=True)
        else:
            cands.sort(key=lambda i: rng.random() ** (1 / weights[i]), reverse=True)
        for i in cands[:FALLBACK_CHUNKS]:
            shortage = total - len(kept)
            if shortage <= 0:
                break
            if time.time() > deadline:
                rep.timed_out = True
                break
            rep.fallback_chunks += 1
            kept.extend(fill_chunk(chunks[i], min(shortage, MAX_PER_CHUNK), n_steps, tag="보충 · "))

    rep.produced = len(kept)
    rep.seconds = time.time() - t0
    return kept, rep


def build_mixed(extracted: dict, counts: dict[str, int], difficulty: str, **kw) -> tuple[list[ValidQuestion], BuildReport]:
    """여러 유형을 한 회차로: counts = {"mcq": 3, "short": 2, "essay": 1}.
    객관식 → 단답형 → 서술형 순서로 만들고, 앞에서 만든 문제는 뒤 유형의 중복 검사에 넣는다.
    보고서는 하나로 합친다 (요청 수·생성 수·폐기 사유 등).
    [추가] '상' 단답형은 '상황 → 현상 이름' 문제만 내므로 재료가 모자랄 수 있다. 모자란 수만큼 객관식으로 채운다
    (사용자 결정 2026-11-18). 시간 상한에 걸렸으면 채우지 않는다."""
    all_qs: list[ValidQuestion] = []
    total = BuildReport(requested=sum(counts.values()))
    t0 = time.time()
    plan = [(t, counts.get(t, 0)) for t in ("mcq", "short", "essay")]
    while plan:
        qtype, n = plan.pop(0)
        if n <= 0:
            continue
        prev = [{"question": q.question, "choices": q.choices, "evidence": q.evidence,
                 "answer": q.answer_text if q.qtype == "short" else ""} for q in all_qs]
        qs, rep = build_quiz(extracted, n, difficulty, qtype=qtype, extra_prev=prev, **kw)
        if qtype == "short" and difficulty == "상" and len(qs) < n and not rep.timed_out:
            total.short_to_mcq = n - len(qs)
            plan.append(("mcq", n - len(qs)))
        all_qs += qs
        total.generated += rep.generated
        total.discarded.update(rep.discarded)
        total.discard_samples += rep.discard_samples
        total.regen_rounds_used += rep.regen_rounds_used
        total.fallback_chunks += rep.fallback_chunks
        total.timed_out |= rep.timed_out
        total.verify_seconds += rep.verify_seconds
        total.gen.calls += rep.gen.calls
        total.gen.seconds += rep.gen.seconds
        total.gen.parse_failures += rep.gen.parse_failures
        total.gen.errors += rep.gen.errors
        total.gen.timings += rep.gen.timings
        total.per_chunk += [{**c, "qtype": qtype} for c in rep.per_chunk]
        for c in rep.chunks_used:
            if c not in total.chunks_used:
                total.chunks_used.append(c)
    # 대신 낸 객관식도 객관식 자리에 오게 유형 순서로 정렬 (같은 유형 안의 순서는 유지)
    order = {"mcq": 0, "short": 1, "essay": 2}
    all_qs.sort(key=lambda q: order.get(q.qtype, 0))
    total.produced = len(all_qs)
    total.seconds = time.time() - t0
    return all_qs, total
