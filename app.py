"""Quizen 화면.

실행: Quizen 폴더에서  .\run.ps1  (또는 python -m streamlit run app.py)

화면 구성
- 사이드바: '새 문제 만들기' 버튼, 내역 트리(폴더 → PDF → 회차, 우클릭 메뉴), 서버 연결 상태
- 새 문제 만들기: PDF 올리기/고르기 → 문제 수·난이도·추가 조건 → 생성(진행 표시)
- 회차: 풀이 중이면 이어 풀기, 채점했으면 결과·근거·원본 페이지·오답 풀이
모든 기록은 data/quizen.db 에 저장되어 앱을 껐다 켜도 남는다.
정답은 DB에만 있고, 제출(채점) 전에는 화면에 그리지 않는다.
"""
import json
import random
import time

import httpx
import pymupdf
import streamlit as st

from quizen import config
from quizen.analyze import analyze_pdf
from quizen.db import Duplicate
from quizen.explain import explain_stream
from quizen.history import chunk_key
from quizen.llm import use_only
from quizen.grade_llm import grade_pending
from quizen.pipeline import build_mixed
from quizen.quiz import QuestionResult, QuizSession
from quizen.scope import parse_keywords, restrict
from quizen.store import UPLOAD_DIR, Store
from quizen.ui.tree import apply_action, tree_sidebar
from quizen.verify import verify_question

DIFFICULTY_HELP = {"하": "용어·정의·기본 개념", "중": "개념 비교·원인과 결과", "상": "복합 개념 적용·관계 추론"}
FEEDBACK_TAGS = ["같은 뜻이야", "오타야", "이 부분은 맞고 이 부분은 틀려", "채점이 틀렸어", "직접 입력"]

st.set_page_config(page_title="Quizen", page_icon="📝", layout="centered")

# Streamlit 기본 영어 문구를 한글로 바꾼다 (Streamlit에 한글 설정이 없어 CSS로 덮어씀).
# Deploy 버튼과 더보기 메뉴는 .streamlit/config.toml 의 toolbarMode = "minimal" 로 숨긴다.
st.markdown("""
<style>
[data-testid="stFileUploaderDropzone"] button [data-testid="stMarkdownContainer"] p { font-size: 0; }
[data-testid="stFileUploaderDropzone"] button [data-testid="stMarkdownContainer"] p::after {
  content: "파일 선택"; font-size: 0.875rem; }
[data-testid="stFileUploaderDropzoneInstructions"] span { font-size: 0; }
[data-testid="stFileUploaderDropzoneInstructions"] span::after {
  content: "PDF 파일 · 최대 200MB"; font-size: 0.875rem; }
[data-testid="stFileUploaderDropzone"] > div:not([data-testid]) > span { font-size: 0; }
[data-testid="stFileUploaderDropzone"] > div:not([data-testid]) > span::after {
  content: "여기에 PDF를 놓으세요"; font-size: 1rem; }
[data-testid="stStatusWidget"] { visibility: hidden; }
</style>
""", unsafe_allow_html=True)

ss = st.session_state
ss.setdefault("view", "new")          # new(새 문제 만들기) | quiz(회차 보기)
ss.setdefault("quiz_id", None)
ss.setdefault("new_doc_id", None)     # 트리에서 'PDF로 문제 만들기'를 누르면 미리 골라 둠
ss.setdefault("flash", None)          # 다음 화면에 한 번 보여 줄 안내 (종류, 문구)


# ------------------------------------------------------------------ 공통
@st.cache_resource
def get_store() -> Store:
    return Store()


store = get_store()
# 마지막으로 고른 모델을 이어서 쓴다 (목록에 없는 값이면 기본값)
_saved_model = store.get_setting("model")
if _saved_model in config.MODELS and not config.MODEL_FROM_ENV:
    config.MODEL = _saved_model


@st.cache_data(ttl=30, show_spinner=False)
def server_status() -> tuple[bool, str]:
    try:
        return True, httpx.get(f"{config.OLLAMA_HOST}/api/version", timeout=5).json()["version"]
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:120]


@st.cache_data(show_spinner=False)
def render_page(pdf_path: str, page: int) -> bytes:
    with pymupdf.open(pdf_path) as doc:
        return doc[page - 1].get_pixmap(dpi=110).tobytes("png")


def folder_paths() -> list[tuple[int | None, str]]:
    """폴더 선택 상자용: (폴더 id, '머신러닝 / 중간고사')."""
    folders = store.tree()["folders"]
    by_id = {f["id"]: f for f in folders}

    def path(f):
        parts = [f["name"]]
        while f["parent_id"] is not None and f["parent_id"] in by_id:
            f = by_id[f["parent_id"]]
            parts.append(f["name"])
        return " / ".join(reversed(parts))

    return [(None, "(폴더 없음)")] + sorted(((f["id"], path(f)) for f in folders), key=lambda x: x[1])


def go_quiz(quiz_id: int) -> None:
    ss.view, ss.quiz_id = "quiz", quiz_id


def go_new(doc_id: int | None = None) -> None:
    ss.view, ss.quiz_id, ss.new_doc_id = "new", None, doc_id


def stream_markdown(parts) -> str:
    """글자가 오는 대로 화면에 이어 붙인다. (st.write_stream은 pyarrow를 불러와 이 PC에서 막힘)"""
    box = st.empty()
    text = ""
    for p in parts:
        text += p
        box.markdown(text + "▌")
    box.markdown(text)
    return text


# ------------------------------------------------------------------ 사이드바
with st.sidebar:
    if st.button("＋ 새 문제 만들기", use_container_width=True, type="primary" if ss.view == "new" else "secondary"):
        go_new()
        st.rerun()
    if st.button("📚 자료 관리", use_container_width=True, type="primary" if ss.view == "library" else "secondary"):
        ss.view, ss.quiz_id = "library", None
        st.rerun()
    if st.button("📒 오답 노트", use_container_width=True, type="primary" if ss.view == "wrong" else "secondary"):
        ss.view, ss.quiz_id = "wrong", None
        st.rerun()
    selected = {"quiz_id": ss.quiz_id} if ss.view == "quiz" else {"doc_id": ss.new_doc_id}
    action = tree_sidebar(store, selected)
    if action:
        t = action.get("type")
        if t == "open_quiz":
            go_quiz(action["id"])
        elif t == "new_quiz":
            go_new(action["doc_id"])
        else:
            err = apply_action(store, action)
            if err:
                ss.flash = ("error", err)
            # 보고 있던 회차나 PDF가 지워졌으면 첫 화면으로
            if ss.view == "quiz" and ss.quiz_id and store.quiz(ss.quiz_id) is None:
                go_new()
            if ss.new_doc_id and store.document(ss.new_doc_id) is None:
                ss.new_doc_id = None
        st.rerun()

    st.divider()
    ok, info = server_status()
    if ok:
        st.caption(f"🟢 추론 서버 연결됨 · `{config.MODEL}`")
    else:
        st.caption("🔴 추론 서버에 연결할 수 없습니다")
        st.caption("확인: 서버 주소(quizen.local.json), 네트워크 연결, 서버 전원·잠자기, Ollama 실행 여부")

if ss.flash:
    kind, msg = ss.flash
    (st.error if kind == "error" else st.info)(msg)
    ss.flash = None


# ------------------------------------------------------------------ 새 문제 만들기
def view_new() -> None:
    st.title("📝 Quizen")
    st.caption("강의자료 문제 풀기")

    docs = store.tree()["documents"]
    up = st.file_uploader("새 강의자료 PDF 올리기", type=["pdf"])
    doc_ids: list[int] = []
    folder_id = None
    balance = False
    if up is not None:
        opts = folder_paths()
        idx = st.selectbox("저장할 폴더", range(len(opts)), format_func=lambda i: opts[i][1])
        folder_id = opts[idx][0]
    elif docs:
        # C-5: 여러 PDF를 골라 한 번에 출제할 수 있다 (예: 9주차 + 10주차)
        by_id = {d["id"]: d for d in docs}
        pre = [ss.new_doc_id] if ss.new_doc_id in by_id else []
        doc_ids = st.multiselect("또는 내 자료에서 고르기 (여러 개 가능)", list(by_id), default=pre,
                                 format_func=lambda i: f"{by_id[i]['title']} ({by_id[i]['page_count']}p)",
                                 placeholder="PDF를 고르세요")
        balance = False
        if len(doc_ids) > 1:
            balance = st.checkbox("PDF마다 비슷한 개수로 나누기",
                                  help="끄면 글자(정보)가 많은 PDF에서 더 많이 출제합니다. 켜면 PDF마다 똑같이 나누는데, "
                                       "그림 위주라 글자가 적은 PDF에서는 문제가 억지스러울 수 있습니다.")
            st.caption("문제마다 어느 PDF에서 나왔는지 표시합니다.")

    st.markdown("**문제 수**")
    c1, c2, c3 = st.columns(3)
    n_mcq = c1.number_input("객관식", min_value=0, max_value=30, value=5, step=1)
    n_short = c2.number_input("단답형", min_value=0, max_value=30, value=0, step=1)
    n_essay = c3.number_input("서술형", min_value=0, max_value=10, value=0, step=1)
    counts = {"mcq": int(n_mcq), "short": int(n_short), "essay": int(n_essay)}
    num = sum(counts.values())
    if num == 0 or num > 30:
        st.warning("문제 수 합계는 1~30개로 정해 주세요.")
    elif n_short or n_essay:
        st.caption("단답형·서술형은 제출할 때 모델이 채점합니다 (서술형 1문제에 약 10~20초). "
                   "채점이 틀렸다고 생각되면 결과 화면의 '판정 바꾸기'로 고칠 수 있습니다.")
    diff = st.radio("난이도", ["하", "중", "상"], index=1, horizontal=True)
    st.caption(f"{diff}: {DIFFICULTY_HELP[diff]}")
    names = list(config.MODELS)
    model = st.radio("모델", names, index=names.index(config.MODEL) if config.MODEL in names else 0,
                     horizontal=True, format_func=lambda m: m.split(":")[0])
    st.caption(config.MODELS[model] + ("" if model == config.MODEL else
                                       " · 바꾸면 추론 서버가 모델을 새로 불러와서 처음 몇 분은 느릴 수 있습니다."))

    page_count = 0
    if up is not None:
        try:
            with pymupdf.open(stream=up.getvalue(), filetype="pdf") as d:
                page_count = d.page_count
        except Exception:  # noqa: BLE001
            st.error("PDF를 열 수 없습니다. 손상된 파일인지 확인해 주세요.")
            return
    elif len(doc_ids) == 1:
        page_count = store.document(doc_ids[0])["page_count"]
    with st.expander("추가 조건 (선택)"):
        page_range = None
        if page_count > 1:
            page_range = st.slider("출제 페이지 범위", 1, page_count, (1, page_count))
        elif len(doc_ids) > 1:
            st.caption("페이지 범위는 PDF를 하나만 골랐을 때 지정할 수 있습니다 (PDF마다 페이지 번호가 겹침).")
        keywords = st.text_input("주제 키워드 (쉼표로 구분)", placeholder="예: 경사 하강법, 학습률",
                                 help="키워드가 들어 있는 부분에서만 출제합니다. 띄어쓰기·대소문자는 구분하지 않습니다.")
        conditions = st.text_area("문제 스타일·조건", max_chars=500,
                                  placeholder="예: 실제 상황 예시를 넣어서 / 보기를 짧게 / 용어 정의보다 개념 비교 위주로",
                                  help="근거 발췌, 정답 하나, 자료에 있는 내용만 출제하는 규칙과 충돌하는 조건은 적용되지 않습니다.")

    if not st.button("문제 생성", type="primary", disabled=(up is None and not doc_ids) or not 1 <= num <= 30):
        return
    if not server_status()[0]:
        st.error("추론 서버에 연결되지 않아 생성할 수 없습니다. 왼쪽 아래 안내를 확인하세요.")
        return

    st.info("생성하는 동안 다른 버튼을 누르면 작업이 중단됩니다. 화면을 그대로 두세요.")
    t0 = time.time()
    with st.status("PDF 분석 중", expanded=True) as status:
        bar = st.progress(0.0)
        line = st.empty()

        # 모델 선택 반영: 고른 모델을 기억하고, 다른 모델이 올라가 있으면 먼저 내린다 (Mac 메모리 보호)
        if model != config.MODEL:
            config.MODEL = model
            store.set_setting("model", model)
        try:
            unloaded = use_only(config.MODEL)
            if unloaded:
                line.write(f"모델 전환: {', '.join(unloaded)} 내림 → {config.MODEL} 사용")
        except Exception as e:  # noqa: BLE001 — 확인에 실패해도 생성은 시도한다
            line.write(f"모델 상태 확인 실패({e}), 그대로 진행합니다.")

        def on_analyze(msg: str, frac: float) -> None:
            bar.progress(min(frac, 1.0))
            line.write(msg)

        if up is not None:          # 새 PDF: 분석 → DB 등록 (같은 내용이 이미 있으면 그것을 씀)
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            tmp = UPLOAD_DIR / f"_tmp_{time.time_ns()}.pdf"
            tmp.write_bytes(up.getvalue())
            try:
                pages = analyze_pdf(tmp, on_analyze)["pages"]
                try:
                    doc_ids = [store.add_document(tmp, up.name, pages, folder_id=folder_id)]
                except Duplicate as dup:
                    doc_ids = [dup.doc_id]
                    ss.flash = ("info", f"이미 등록된 PDF('{dup.title}')라서 그 자료로 출제했습니다.")
            finally:
                tmp.unlink(missing_ok=True)

        extracted = store.extracted(doc_ids)
        kw = parse_keywords(keywords)
        full = page_range is None or tuple(page_range) == (1, page_count)
        if not full or kw:
            line.write("출제 범위 적용 중")
            extracted = restrict(extracted, *((None, None) if full else page_range), keywords=kw)
        if not extracted["chunks"]:
            status.update(label="출제 범위에 해당하는 내용이 없습니다", state="error")
            st.error("선택한 페이지 범위나 키워드에 맞는 내용을 찾지 못했습니다. 범위를 넓히거나 키워드를 바꿔 보세요.")
            return

        status.update(label="문제 생성·검증 중")
        stage_text = {"생성": "문제 생성 중", "검증": "정답 숨김 검증 중"}

        def on_build(stage: str, i: int, n: int, msg: str) -> None:
            bar.progress(max(0.0, (i - 1) / n))
            line.write(f"{stage_text.get(stage, stage)} · {msg}")

        history = store.history(doc_ids)
        qs, rep = build_mixed(extracted, counts, diff, verifier=verify_question, progress=on_build,
                              conditions=conditions, history=history, rng=random.Random(), balance_docs=balance)
        bar.progress(1.0)
        status.update(label=f"완료 · {rep.produced}문제 ({time.time() - t0:.0f}초)", state="complete",
                      expanded=False)

    if not qs:
        st.error("검증을 통과한 문제가 없습니다. 다른 난이도나 범위로 다시 시도해 보세요.")
        return
    keys = {c["chunk_id"]: chunk_key(c) for c in extracted["chunks"]}
    chunk_doc = {c["chunk_id"]: c["doc_id"] for c in extracted["chunks"]}
    # 대표 PDF(회차 이름 번호 기준) = 문제가 가장 많이 나온 PDF
    # 주의: counts(유형별 요청 수)와 이름이 겹치지 않게 한다 — 겹치면 회차 정보에 PDF 번호가 유형으로 저장됨
    per_doc = {d: sum(chunk_doc[q.chunk_id] == d for q in qs) for d in doc_ids}
    ordered = sorted(doc_ids, key=lambda d: -per_doc[d])
    quiz_id = store.create_quiz(
        QuizSession(qs), ordered, diff, int(num), chunk_keys=keys, conditions=conditions,
        doc_of_question=[chunk_doc[q.chunk_id] for q in qs],
        scope={"pages": None if full else list(page_range), "keywords": kw, "balance_docs": balance,
               "counts": counts},
        gen_seconds=time.time() - t0,
        report={"discarded": dict(rep.discarded), "samples": rep.discard_samples[:20],
                "fallback_chunks": rep.fallback_chunks, "short_to_mcq": rep.short_to_mcq})
    history.record(qs, rep.chunks_used)          # 다음 회차에서 구간 돌려쓰기에 사용
    notes = []
    if rep.short_to_mcq:
        notes.append(f"'상' 단답형으로 낼 만한 상황(현상 이름을 묻는 문제)이 부족해 {rep.short_to_mcq}문제는 객관식으로 냈습니다.")
    if rep.produced < rep.requested:
        why = (" 시간이 너무 오래 걸려 생성을 멈췄습니다. 범위를 넓히거나 난이도를 바꿔 보세요."
               if rep.timed_out else "")
        notes.append(f"요청 {rep.requested}문제 중 검증을 통과한 {rep.produced}문제만 출제되었습니다.{why}")
    if notes:
        before = [ss.flash[1]] if ss.flash else []      # 'PDF가 이미 등록됨' 안내가 있으면 함께 보여 줌
        ss.flash = ("info", " ".join(before + notes))
    go_quiz(quiz_id)
    st.rerun()


# ------------------------------------------------------------------ 회차: 풀이
def _answered(v: dict) -> bool:
    return v["user_index"] is not None if v["qtype"] == "mcq" else bool((v["user_text"] or "").strip())


def _save_text(question_id: int, key: str) -> None:
    """단답·서술 입력칸에서 다른 곳을 누르면(또는 Enter) 바로 저장한다."""
    store.save_answer(question_id, user_text=ss.get(key, ""))


QTYPE_KO = {"mcq": "객관식", "short": "단답형", "essay": "서술형"}


def view_solving(quiz) -> None:
    qid = quiz["id"]
    views = store.question_view(qid)
    n = len(views)
    cur = min(quiz["current"], n - 1)
    answered = sum(_answered(v) for v in views)
    st.progress(answered / n, text=f"{answered}/{n} 답함")

    cols = st.columns(min(n, 10))
    for i, v in enumerate(views):
        mark = "●" if _answered(v) else "○"
        if cols[i % len(cols)].button(f"{mark}{i + 1}", key=f"nav{qid}_{i}", use_container_width=True,
                                      type="primary" if i == cur else "secondary"):
            store.set_current(qid, i)
            st.rerun()

    v = views[cur]
    st.subheader(f"{v['number']}. {v['question']}")
    st.caption(f"{QTYPE_KO[v['qtype']]} · 난이도 {v['difficulty']}")
    if v["qtype"] == "mcq":
        picked = st.radio("보기", range(len(v["choices"])), index=v["user_index"], key=f"radio_{v['id']}",
                          format_func=lambda i: f"{i + 1}) {v['choices'][i]}", label_visibility="collapsed")
        if picked is not None and picked != v["user_index"]:
            store.save_answer(v["id"], picked)       # 고르는 즉시 저장 → 나갔다 와도 이어서 풀 수 있음
            st.rerun()
    elif v["qtype"] == "short":
        key = f"txt_{v['id']}"
        st.text_input("답 (용어 하나, 여러 개면 쉼표로 구분)", value=v["user_text"] or "", key=key,
                      on_change=_save_text, args=(v["id"], key), placeholder="예: 경사 하강법  /  에포크, 배치")
        st.caption("Enter를 누르거나 다른 곳을 누르면 저장됩니다.")
    else:
        key = f"txt_{v['id']}"
        st.text_area("답안 (2~4문장)", value=v["user_text"] or "", key=key, on_change=_save_text,
                     args=(v["id"], key), height=160)
        st.caption("다른 곳을 누르면 저장됩니다. 채점 기준에 맞춰 모델이 채점합니다.")

    c1, c2, c3 = st.columns([1, 1, 2])
    if c1.button("◀ 이전", disabled=cur == 0, use_container_width=True):
        store.set_current(qid, cur - 1)
        st.rerun()
    if c2.button("다음 ▶", disabled=cur == n - 1, use_container_width=True):
        store.set_current(qid, cur + 1)
        st.rerun()
    left = n - answered
    confirm = True
    if left:
        confirm = c3.checkbox(f"안 푼 {left}문제는 오답 처리하고 제출")
    if c3.button("제출", type="primary", disabled=not confirm, use_container_width=True):
        pending = store.grade(qid)               # 객관식·글자가 같은 단답은 코드로 바로 채점
        if pending:
            with st.status(f"단답형·서술형 {len(pending)}문제 채점 중", expanded=True) as status:
                bar = st.progress(0.0)
                line = st.empty()

                def on_grade(i: int, total: int, msg: str) -> None:
                    bar.progress((i - 1) / total)
                    line.write(msg)

                use_only(config.MODEL)
                grade_pending(store, pending, on_grade)
                status.update(label="채점 완료", state="complete")
        st.rerun()


# ------------------------------------------------------------------ 회차: 결과
def to_result(r: dict) -> QuestionResult:
    return QuestionResult(number=r["number"], question=r["question"], choices=tuple(r["choices"]),
                          difficulty=r["difficulty"], user_index=r["user_index"],
                          answer_index=r["answer_index"] if r["answer_index"] is not None else -1,
                          correct=r["correct"], evidence=r["evidence"], page=r["page"],
                          source_document=r["source_document"], chunk_id=0,
                          evidence_pages=tuple(r["evidence_pages"]), qtype=r["qtype"],
                          user_text=r["user_text"] or "", answer_text=r["answer_text"] or "")


def _show_mcq(r: dict) -> None:
    for i, c in enumerate(r["choices"]):
        tag = " ← 정답" if i == r["answer_index"] else ""
        if i == r["user_index"] and i != r["answer_index"]:
            tag = " ← 내 답"
        # "1)"을 마크다운이 번호 목록으로 바꾸지 않도록 괄호를 이스케이프한다
        text = f"{i + 1}\\) {c}{tag}"
        st.markdown(f"**{text}**" if i == r["answer_index"] else text)
    if r["user_index"] is None:
        st.caption("선택하지 않음")


def _show_short(r: dict) -> None:
    mine = (r["user_text"] or "").strip()
    st.markdown(f"**내 답:** {mine or '(쓰지 않음)'}")
    st.markdown(f"**정답:** {r['answer_text']}" + (f"  (인정: {', '.join(r['accepted'])})" if r["accepted"] else ""))
    d = r["auto_detail"] or {}
    how = "코드 비교" if d.get("method") == "code" else "모델 판단"
    if d.get("reason"):
        st.caption(f"채점({how}): {d['reason']}")


def _show_essay(r: dict) -> None:
    mine = (r["user_text"] or "").strip()
    st.markdown("**내 답안**")
    st.write(mine or "(쓰지 않음)")
    d = r["auto_detail"] or {}
    points = d.get("points") or []
    if points:
        met = sum(p["met"] for p in points)
        st.markdown(f"**채점 기준별 결과** — {met}/{len(points)} 충족 ({r['auto_score']:.0%}, 60% 이상이면 정답)")
        for p in points:
            st.markdown(f"{'✅' if p['met'] else '❌'} {p['point']}  \n　↳ {p['comment']}")
    else:
        st.markdown("**채점 기준**")
        for item in r["rubric"]:
            st.markdown(f"- {item['point']}")
    if d.get("feedback"):
        st.info(f"피드백: {d['feedback']}")
    if d.get("reason"):
        st.caption(d["reason"])
    with st.popover("모범 답안 보기"):
        st.write(r["answer_text"])
        st.caption("채점 기준 근거")
        for item in r["rubric"]:
            st.caption(f"· {item['point']} — {item['evidence'][:120]} (p.{item['page']})")


def show_evidence_block(r: dict, prefix: str):
    """출제 근거, 복습 위치, 원본 페이지 보기. 버튼 두 칸(오답 풀이, 판정 바꾸기 자리)을 돌려준다."""
    st.markdown("**출제 근거**")
    ev = r["evidence"] if len(r["evidence"]) <= 200 else r["evidence"][:200] + " …"
    st.info(ev)
    if len(r["evidence"]) > 200:
        with st.popover("근거 전체 보기"):
            st.write(r["evidence"])
    st.caption(f"복습 위치: {r['source_document']} · {r['page']}페이지")
    b1, b2, b3 = st.columns(3)
    if r["doc_id"] and b1.toggle("원본 페이지 보기", key=f"{prefix}page{r['id']}"):
        st.image(render_page(str(store.document_path(r["doc_id"])), r["page"]),
                 caption=f"{r['source_document']} p.{r['page']}")
    return b2, b3


def show_explanation(r: dict, col, prefix: str) -> None:
    """저장된 오답 풀이가 있으면 보여 주고, 없으면 버튼으로 만들어 저장한다."""
    if r.get("explanation"):
        st.markdown("**오답 풀이**")
        st.markdown(r["explanation"])
    elif col.button("오답 풀이 보기", key=f"{prefix}exp{r['id']}"):
        st.markdown("**오답 풀이**")
        try:
            pages = store.document_pages(r["doc_id"]) if r["doc_id"] else {}
            text = stream_markdown(explain_stream(to_result({**r, "correct": False}), pages))
            store.save_explanation(r["id"], text)       # 다음에 열 때는 다시 만들지 않음
        except Exception as e:  # noqa: BLE001
            st.error(f"오답 풀이를 가져오지 못했습니다: {e}")


def start_retake(question_ids: list[int]) -> None:
    quiz_id, skipped = store.create_retake(question_ids)
    if quiz_id is None:
        st.error("원본 PDF가 삭제되어 다시 풀 수 있는 문제가 없습니다.")
        return
    if skipped:
        ss.flash = ("info", f"원본 PDF가 삭제된 {skipped}문제는 빼고 재시험을 만들었습니다.")
    go_quiz(quiz_id)
    st.rerun()


def view_result(quiz) -> None:
    qid = quiz["id"]
    total, ok = store.score(qid)
    c1, c2, c3 = st.columns(3)
    c1.metric("총 문제", total)
    c2.metric("맞힌 문제", ok)
    c3.metric("정답률", f"{ok / total:.0%}" if total else "-")
    results = store.results(qid)
    wrong_ids = [r["id"] for r in results if not r["correct"]]
    if wrong_ids and st.button(f"🔁 이 회차에서 틀린 {len(wrong_ids)}문제만 다시 풀기", key=f"retake{qid}"):
        start_retake(wrong_ids)

    for r in results:
        icon = "✅" if r["correct"] else "❌"
        if r["qtype"] == "essay" and not r["correct"] and (r["auto_score"] or 0) > 0:
            icon = "🟡"                              # 서술형 부분 점수
        flag = " ✏️" if r["overridden"] else ""
        label = f"{icon}{flag} {r['number']}. [{QTYPE_KO[r['qtype']]}] {r['question']}"
        # 틀린 문제와, 내가 판정을 바꾼 문제는 펼쳐 둔다 (바꾼 직후 접혀서 메모가 안 보이는 것 방지)
        with st.expander(label, expanded=not r["correct"] or bool(r["overridden"])):
            {"mcq": _show_mcq, "short": _show_short, "essay": _show_essay}[r["qtype"]](r)

            b2, b3 = show_evidence_block(r, "res")

            with b3.popover("판정 바꾸기"):
                st.caption(f"자동 채점: {'정답' if r['auto_correct'] else '오답'}")
                new = st.radio("최종 판정", ["정답", "오답"], index=0 if r["correct"] else 1,
                               key=f"ov{r['id']}", horizontal=True)
                tag = st.selectbox("이유", FEEDBACK_TAGS, key=f"ovt{r['id']}")
                memo = st.text_area("피드백 (선택)", value=r["feedback"] or "", key=f"ovm{r['id']}",
                                    placeholder="예: 3번 기준은 '학습률을 줄인다'로 썼으니 맞음")
                s1, s2 = st.columns(2)
                if s1.button("저장", key=f"ovs{r['id']}", type="primary"):
                    store.override(r["id"], new == "정답", tag, memo)
                    st.rerun()
                if r["overridden"] and s2.button("자동 채점으로 되돌리기", key=f"ovr{r['id']}"):
                    store.clear_override(r["id"])
                    st.rerun()
            if r["overridden"]:
                st.caption(f"✏️ 판정 변경: {r['feedback_tag'] or ''} {('— ' + r['feedback']) if r['feedback'] else ''}")

            if not r["correct"]:
                show_explanation(r, b2, "res")


def quiz_info(quiz) -> None:
    """회차를 어떻게 만들었는지: 만든 시각, 범위·조건, 걸린 시간, 버린 문제와 이유."""
    scope = json.loads(quiz["scope"] or "{}")
    report = json.loads(quiz["report"] or "{}")
    with st.expander("회차 정보"):
        made = time.strftime("%Y-%m-%d %H:%M", time.localtime(quiz["created_at"]))
        lines = [f"- 만든 시각: {made}"]
        if quiz["graded_at"]:
            lines.append(f"- 채점 시각: {time.strftime('%Y-%m-%d %H:%M', time.localtime(quiz['graded_at']))}")
        counts = scope.get("counts") or {}
        kinds = " · ".join(f"{QTYPE_KO[k]} {v}" for k, v in counts.items() if v and k in QTYPE_KO)
        lines.append(f"- 요청 {quiz['requested']}문제{f' ({kinds})' if kinds else ''} → 출제 {quiz['produced']}문제")
        if quiz["gen_seconds"]:
            lines.append(f"- 생성 시간: {quiz['gen_seconds']:.0f}초 (문제당 {quiz['gen_seconds'] / max(quiz['produced'], 1):.0f}초)")
        pages = scope.get("pages")
        lines.append(f"- 출제 범위: {f'{pages[0]}~{pages[1]}페이지' if pages else '전체'}"
                     + (f" · 키워드 {', '.join(scope['keywords'])}" if scope.get("keywords") else ""))
        if quiz["conditions"]:
            lines.append(f"- 추가 조건: {quiz['conditions']}")
        lines.append(f"- 모델: `{quiz['model']}`")
        if report.get("short_to_mcq"):
            lines.append(f"- '상' 단답형 {report['short_to_mcq']}문제는 알맞은 상황이 없어 객관식으로 대신 출제")
        discarded = report.get("discarded") or {}
        if discarded:
            lines.append("- 검증에서 버린 문제: " + ", ".join(f"{k} {v}개" for k, v in discarded.items()))
        if report.get("fallback_chunks"):
            lines.append(f"- 부족분 보충에 쓴 구간: {report['fallback_chunks']}곳")
        st.markdown("\n".join(lines))


def view_quiz() -> None:
    quiz = store.quiz(ss.quiz_id) if ss.quiz_id else None
    if quiz is None:
        go_new()
        st.rerun()
    titles = [store.document(d)["title"] for d in store.quiz_doc_ids(quiz["id"])]
    st.title(quiz["name"])
    status = "채점 완료" if quiz["status"] == "graded" else "풀이 중"
    st.caption(f"{' + '.join(titles)} · 난이도 {quiz['difficulty']} · {quiz['produced']}문제 · {status}")
    quiz_info(quiz)
    if quiz["status"] == "graded":
        view_result(quiz)
    else:
        view_solving(quiz)


# ------------------------------------------------------------------ 오답 노트 (C-6)
def view_wrong() -> None:
    st.title("📒 오답 노트")
    st.caption("틀린 문제를 모아 봅니다. 재시험에서 맞히면 여기서 빠지고, 같은 문제를 여러 번 틀리면 가장 최근 답으로 한 번만 보입니다.")
    tree = store.tree()
    scope = st.radio("범위", ["전체", "폴더", "PDF"], horizontal=True)
    doc_ids: list[int] | None = None
    if scope == "폴더":
        opts = [o for o in folder_paths() if o[0] is not None]
        if not opts:
            st.info("아직 폴더가 없습니다.")
            return
        i = st.selectbox("폴더 (하위 폴더 포함)", range(len(opts)), format_func=lambda k: opts[k][1])
        doc_ids = store.folder_doc_ids(opts[i][0])
    elif scope == "PDF":
        by_id = {d["id"]: d for d in tree["documents"]}
        doc_ids = st.multiselect("PDF", list(by_id), format_func=lambda k: by_id[k]["title"], placeholder="PDF를 고르세요")
    if doc_ids is not None and not doc_ids:
        st.caption("범위를 골라 주세요." if scope == "PDF" else "이 폴더에는 PDF가 없습니다.")
        return
    wrong = store.wrong_questions(doc_ids)
    if not wrong:
        st.success("틀린 문제가 없습니다. 🎉")
        return

    kinds = {k: sum(w["qtype"] == k for w in wrong) for k in QTYPE_KO}
    st.markdown(f"**틀린 문제 {len(wrong)}개** · " + " · ".join(f"{QTYPE_KO[k]} {v}" for k, v in kinds.items() if v))
    c1, c2 = st.columns([2, 1])
    n = c1.slider("다시 풀 문제 수 (최근에 틀린 순서)", 1, len(wrong), len(wrong)) if len(wrong) > 1 else 1
    if c2.button(f"🔁 {n}문제 다시 풀기", type="primary", use_container_width=True):
        start_retake([w["id"] for w in wrong[:n]])

    for w in wrong:
        made = time.strftime("%m/%d", time.localtime(w["quiz_created"]))
        with st.expander(f"❌ [{QTYPE_KO[w['qtype']]}] {w['question']}  ·  {w['source_document']}"):
            st.caption(f"틀린 회차: {w['quiz_name']} ({made}) · 난이도 {w['difficulty']}")
            {"mcq": _show_mcq, "short": _show_short, "essay": _show_essay}[w["qtype"]](w)
            b2, b3 = show_evidence_block(w, "wn")
            if b3.button("원래 회차 열기", key=f"wnopen{w['id']}"):
                go_quiz(w["quiz_id"])
                st.rerun()
            show_explanation(w, b2, "wn")


# ------------------------------------------------------------------ 자료 관리 (C-4)
def register_pdf(name: str, data: bytes, folder_id: int | None) -> tuple[str, str]:
    """PDF 하나를 분석해 등록한다. (결과 종류, 설명) — 새로 등록 / 중복 / 실패.
    분석(글자 추출·토큰 계산)은 이 PC에서만 하므로 모델을 부르지 않는다."""
    try:
        with pymupdf.open(stream=data, filetype="pdf") as d:
            if d.page_count == 0:
                return "실패", "페이지가 없는 PDF입니다."
    except Exception:  # noqa: BLE001
        return "실패", "PDF를 열 수 없습니다 (손상되었거나 PDF가 아닌 파일)."
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tmp = UPLOAD_DIR / f"_tmp_{time.time_ns()}.pdf"
    tmp.write_bytes(data)
    try:
        pages = analyze_pdf(tmp)["pages"]
        doc_id = store.add_document(tmp, name, pages, folder_id=folder_id)
        low = sum(len("".join(p["text"].split())) < 50 for p in pages)
        note = f"{len(pages)}페이지" + (f" · 글자가 거의 없는 페이지 {low}개" if low else "")
        return "등록", note
    except Duplicate as dup:
        return "중복", f"이미 '{dup.title}'(으)로 등록되어 있습니다."
    finally:
        tmp.unlink(missing_ok=True)


def view_library() -> None:
    st.title("📚 자료 관리")
    st.caption("PDF를 여러 개 한꺼번에 등록해 두고, 문제는 나중에 만들 수 있습니다. "
               "이름 바꾸기·폴더 이동·삭제는 왼쪽 목록에서 우클릭으로 합니다.")

    with st.form("register", clear_on_submit=True):
        files = st.file_uploader("PDF 올리기 (여러 개 가능)", type=["pdf"], accept_multiple_files=True)
        opts = folder_paths()
        idx = st.selectbox("저장할 폴더", range(len(opts)), format_func=lambda i: opts[i][1])
        submitted = st.form_submit_button("등록", type="primary")
    if submitted and files:
        with st.status(f"PDF {len(files)}개 등록 중", expanded=True):
            results = []
            for i, f in enumerate(files, 1):
                st.write(f"({i}/{len(files)}) {f.name} 분석 중…")
                results.append((f.name, *register_pdf(f.name, f.getvalue(), opts[idx][0])))
        # 왼쪽 트리에 새 PDF가 바로 보이도록 화면을 새로 그리고, 결과는 그다음 화면에서 보여 준다
        ss.lib_results = results
        st.rerun()
    results = ss.pop("lib_results", None)
    if results:
        ok = sum(r[1] == "등록" for r in results)
        st.success(f"완료 · 새로 등록 {ok}개 / 전체 {len(results)}개")
        for name, kind, note in results:
            icon = {"등록": "✅", "중복": "ℹ️", "실패": "⚠️"}[kind]
            st.markdown(f"{icon} **{name}** — {kind}: {note}")

    tree = store.tree()
    docs = tree["documents"]
    st.subheader(f"등록된 PDF {len(docs)}개")
    if not docs:
        st.caption("아직 없습니다.")
        return
    paths = dict(folder_paths())
    counts: dict[int, int] = {}
    for q in tree["quizzes"]:
        counts[q["doc_id"]] = counts.get(q["doc_id"], 0) + 1
    rows = ["| 제목 | 폴더 | 페이지 | 회차 | 등록일 |", "|---|---|---|---|---|"]
    for d in docs:
        full = store.document(d["id"])
        made = time.strftime("%Y-%m-%d", time.localtime(full["created_at"]))
        title = d["title"].replace("|", "\\|")
        rows.append(f"| {title} | {paths.get(d['folder_id'], '(폴더 없음)')} | {d['page_count']} | "
                    f"{counts.get(d['id'], 0)} | {made} |")
    st.markdown("\n".join(rows))


{"new": view_new, "quiz": view_quiz, "library": view_library, "wrong": view_wrong}[ss.view]()
