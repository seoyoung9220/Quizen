"""C-1 저장소 테스트. 임시 폴더에 DB와 업로드 파일을 만들어 실제 데이터에 영향을 주지 않는다.
실행: python tests/test_store.py
"""
import os
os.environ["QUIZEN_OFFLINE"] = "1"   # 테스트는 절대 모델(Mac Studio)을 부르지 않는다
import random
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import quizen.pipeline as pl  # noqa: E402
from quizen import config, store as store_mod  # noqa: E402
from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.db import Duplicate  # noqa: E402
from quizen.quiz import QuizSession  # noqa: E402
from quizen.schemas import GeneratedQuestion  # noqa: E402
from quizen.scope import restrict  # noqa: E402
from quizen.store import Store  # noqa: E402
from quizen.validate import ValidQuestion  # noqa: E402

PDF = ROOT / "samples" / "sample.pdf"
PAGES = analyze_pdf(PDF)["pages"]


class Sandbox:
    """테스트마다 임시 프로젝트 폴더(DB, uploads)를 쓴다. 분석 캐시는 원래 data를 그대로 읽는다."""

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quizen-test-"))
        self.old = (config.PROJECT_DIR, config.DATA_DIR, store_mod.UPLOAD_DIR)
        config.PROJECT_DIR = self.tmp
        config.DATA_DIR = self.tmp / "data"
        store_mod.UPLOAD_DIR = config.DATA_DIR / "uploads"
        return Store(config.DATA_DIR / "quizen.db")

    def __exit__(self, *exc):
        config.PROJECT_DIR, config.DATA_DIR, store_mod.UPLOAD_DIR = self.old
        for s in Store.instances:               # DB 연결을 닫아야 Windows에서 임시 폴더를 지울 수 있다
            s.conn.close()
        Store.instances.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)


def vq(i: int, page: int = 13, doc: str = "9주차") -> ValidQuestion:
    return ValidQuestion(question=f"Q{i}", choices=[f"{i}정답", f"{i}오답1", f"{i}오답2", f"{i}오답3"],
                         answer_index=0, difficulty="중", evidence=f"근거 문장 {i}번입니다", page=page,
                         evidence_pages=[page], evidence_mode="full", source_document=doc, chunk_id=1)


def make_quiz(s: Store, doc_id: int, n: int = 3, seed: int = 1, **kw) -> int:
    return s.create_quiz(QuizSession([vq(i) for i in range(n)], seed=seed), [doc_id], "중", n, **kw)


# ------------------------------------------------------------------ PDF
def test_add_document_and_duplicate():
    with Sandbox() as s:
        d = s.add_document(PDF, PDF.name, PAGES)
        doc = s.document(d)
        assert doc["title"] == PDF.stem and doc["page_count"] == 48
        assert s.document_path(d).exists() and len(s.document_pages(d)) == 48
        # 같은 내용, 다른 이름 → 중복으로 막힘
        copy = config.PROJECT_DIR / "다른이름.pdf"
        shutil.copyfile(PDF, copy)
        try:
            s.add_document(copy, "다른이름.pdf", PAGES)
            raise AssertionError("중복 등록이 막히지 않음")
        except Duplicate as e:
            assert e.doc_id == d


def test_persistence_after_reopen():
    """앱을 껐다 켠 것처럼 DB를 다시 열어도 데이터가 남아 있다."""
    with Sandbox() as s:
        d = s.add_document(PDF, PDF.name, PAGES)
        q = make_quiz(s, d)
        path = s.conn.execute("PRAGMA database_list").fetchone()["file"]
        s.conn.close()
        s2 = Store(Path(path))
        assert s2.document(d)["title"] == PDF.stem and s2.quiz(q)["name"].endswith("(1)")
        assert len(s2.question_view(q)) == 3


# ------------------------------------------------------------------ 회차
def test_quiz_names_per_file_per_day():
    with Sandbox() as s:
        d = s.add_document(PDF, PDF.name, PAGES)
        names = [s.quiz(make_quiz(s, d))["name"] for _ in range(3)]
        day = names[0][:6]
        assert names == [f"{day}(1)", f"{day}(2)", f"{day}(3)"], names


def test_question_view_hides_answer():
    with Sandbox() as s:
        d = s.add_document(PDF, PDF.name, PAGES)
        q = make_quiz(s, d)
        for v in s.question_view(q):
            assert set(v) == {"id", "number", "qtype", "question", "choices", "difficulty", "user_index", "user_text"}


def test_saved_order_and_answer_match_session():
    """DB에 저장된 보기 순서·정답 번호가 섞은 결과와 같다 (정답 번호의 보기 = 원래 정답)."""
    with Sandbox() as s:
        d = s.add_document(PDF, PDF.name, PAGES)
        q = make_quiz(s, d, n=8, seed=3)
        for r in s.results(q):
            assert r["choices"][r["answer_index"]].endswith("정답"), r


def test_answer_grade_resume_and_override():
    with Sandbox() as s:
        d = s.add_document(PDF, PDF.name, PAGES)
        q = make_quiz(s, d, n=3)
        rows = s.results(q)
        s.save_answer(rows[0]["id"], rows[0]["answer_index"])               # 맞힘
        s.save_answer(rows[1]["id"], (rows[1]["answer_index"] + 1) % 4)     # 틀림, 3번은 안 품
        s.set_current(q, 1)
        assert s.quiz(q)["status"] == "solving" and s.quiz(q)["current"] == 1   # 이어 풀기 정보
        assert [v["user_index"] for v in s.question_view(q)][:2] == [rows[0]["answer_index"],
                                                                     (rows[1]["answer_index"] + 1) % 4]
        s.grade(q)
        assert s.score(q) == (3, 1) and s.quiz(q)["status"] == "graded"
        # 2번 판정을 '같은 뜻이야'로 정답 처리 → 점수 반영, 다시 채점해도 유지
        s.override(rows[1]["id"], True, "같은 뜻", "표현만 다름")
        assert s.score(q) == (3, 2)
        s.grade(q)
        assert s.score(q) == (3, 2)
        r = s.results(q)[1]
        assert r["overridden"] == 1 and r["feedback_tag"] == "같은 뜻" and r["auto_correct"] == 0
        s.clear_override(rows[1]["id"])
        assert s.score(q) == (3, 1)


def test_explanation_saved():
    with Sandbox() as s:
        d = s.add_document(PDF, PDF.name, PAGES)
        q = make_quiz(s, d)
        qid = s.results(q)[0]["id"]
        s.save_explanation(qid, "풀이 내용")
        assert s.results(q)[0]["explanation"] == "풀이 내용"


def test_delete_quiz_cascades():
    with Sandbox() as s:
        d = s.add_document(PDF, PDF.name, PAGES)
        q = make_quiz(s, d)
        qid = s.results(q)[0]["id"]
        s.save_answer(qid, 0)
        s.save_explanation(qid, "x")
        s.delete_quiz(q)
        for t in ("questions", "answers", "explanations"):
            assert s.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] == 0, t
        assert s.document(d) is not None


# ------------------------------------------------------------------ 폴더
def test_folder_tree_move_and_cycle_guard():
    with Sandbox() as s:
        ml = s.create_folder("머신러닝")
        mid = s.create_folder("중간고사", ml)
        sub = s.create_folder("9주차 묶음", mid)
        d = s.add_document(PDF, PDF.name, PAGES, folder_id=sub)
        t = s.tree()
        assert {f["name"] for f in t["folders"]} == {"머신러닝", "중간고사", "9주차 묶음"}
        assert t["documents"][0]["folder_id"] == sub
        try:
            s.move_folder(ml, sub)       # 상위 폴더를 자기 하위로 → 막힘
            raise AssertionError("순환 이동이 막히지 않음")
        except ValueError:
            pass
        s.move_folder(sub, None)         # 맨 위로 이동
        assert s.conn.execute("SELECT parent_id FROM folders WHERE id=?", (sub,)).fetchone()["parent_id"] is None
        s.rename_folder(ml, "ML")
        s.rename_document(d, "9주차")
        assert s.document(d)["title"] == "9주차"


def test_delete_folder_keep_contents():
    with Sandbox() as s:
        ml = s.create_folder("머신러닝")
        mid = s.create_folder("중간고사", ml)
        inner = s.create_folder("안쪽", mid)
        d = s.add_document(PDF, PDF.name, PAGES, folder_id=mid)
        s.delete_folder(mid, keep_contents=True)
        assert s.document(d)["folder_id"] == ml
        assert s.conn.execute("SELECT parent_id FROM folders WHERE id=?", (inner,)).fetchone()["parent_id"] == ml


def test_delete_folder_with_contents():
    with Sandbox() as s:
        ml = s.create_folder("머신러닝")
        mid = s.create_folder("중간고사", ml)
        d = s.add_document(PDF, PDF.name, PAGES, folder_id=mid)
        q = make_quiz(s, d)
        path = s.document_path(d)
        s.delete_folder(ml, keep_contents=False)
        assert s.conn.execute("SELECT COUNT(*) AS n FROM folders").fetchone()["n"] == 0
        assert s.document(d) is None and s.quiz(q) is None and not path.exists()


def test_delete_document_keeps_mixed_quiz():
    """여러 PDF를 섞은 회차는 PDF 하나를 지워도 남고, 그 PDF만 쓴 회차는 지워진다."""
    with Sandbox() as s:
        d1 = s.add_document(PDF, PDF.name, PAGES)
        other = config.PROJECT_DIR / "other.pdf"
        other.write_bytes(PDF.read_bytes() + b"\n% different")
        d2 = s.add_document(other, "other.pdf", PAGES)
        only1 = make_quiz(s, d1)
        mixed = s.create_quiz(QuizSession([vq(0), vq(1)], seed=1), [d1, d2], "중", 2, doc_of_question=[d1, d2])
        s.delete_document(d1)
        assert s.quiz(only1) is None and s.quiz(mixed) is not None
        rs = s.results(mixed)
        assert [r["doc_id"] for r in rs].count(None) == 1          # d1에서 나온 문제는 연결만 끊김
        assert all(r["source_document"] for r in rs)              # 파일 이름은 남음


# ------------------------------------------------------------------ 오답 노트·재시험
def test_wrong_notes_and_retake():
    with Sandbox() as s:
        d = s.add_document(PDF, PDF.name, PAGES)
        q = make_quiz(s, d, n=3)
        rows = s.results(q)
        for r in rows:
            s.save_answer(r["id"], (r["answer_index"] + 1) % 4)   # 모두 틀림
        s.grade(q)
        wrong = s.wrong_questions([d])
        assert len(wrong) == 3
        # 틀린 문제만 다시 풀기: 같은 문제를 새로 섞어 새 회차로
        sess = QuizSession([ValidQuestion(question=w["question"], choices=[w["choices"][w["answer_index"]]] +
                                          [c for i, c in enumerate(w["choices"]) if i != w["answer_index"]],
                                          answer_index=0, difficulty=w["difficulty"], evidence=w["evidence"],
                                          page=w["page"], evidence_pages=w["evidence_pages"],
                                          evidence_mode="full", source_document=w["source_document"], chunk_id=1)
                            for w in wrong], seed=5)
        r2 = s.create_quiz(sess, [d], "중", 3, parent_quiz_id=q, origin_ids=[w["id"] for w in wrong])
        assert s.quiz(r2)["parent_quiz_id"] == q
        rows2 = s.results(r2)
        s.save_answer(rows2[0]["id"], rows2[0]["answer_index"])     # 하나는 이번에 맞힘
        s.grade(r2)
        left = s.wrong_questions([d])
        assert len(left) == 2, [w["question"] for w in left]       # 맞힌 문제는 오답 노트에서 빠짐


# ------------------------------------------------------------------ 출제 이력 (DB) + 파이프라인
def test_db_history_with_pipeline():
    with Sandbox() as s:
        d = s.add_document(PDF, PDF.name, PAGES)
        ext = s.extracted([d])
        assert all(c["doc_id"] == d and c["source_document"] == PDF.stem for c in ext["chunks"])
        hist = s.history([d])
        target = ext["chunks"][3]

        def fake(chunk_text, difficulty, num, existing, stats, model=None, conditions="", **kw):
            line = next(l for l in chunk_text.splitlines() if len(l) > 25 and "[[PAGE" not in l)
            return [GeneratedQuestion(question=f"{chunk_text[:12]} 관련 질문", choices=["가1", "나1", "다1", "라1"],
                                      answer_index=0, difficulty="중", evidence=line)]

        orig = pl.generate_for_chunk
        pl.generate_for_chunk = fake
        try:
            qs, rep = pl.build_quiz({**ext, "chunks": [target]}, 1, "중", history=hist)
        finally:
            pl.generate_for_chunk = orig
        assert qs and qs[0].source_document == PDF.stem
        hist.record(qs, rep.chunks_used)
        key = f"{d}:{target['page_start']}-{target['page_end']}"
        assert s.history([d]).chunk_usage == {key: 1}


def test_tree_actions():
    """사이드바 트리에서 오는 동작(새 폴더·이름 바꾸기·이동·삭제)이 DB에 반영된다."""
    from quizen.ui.tree import apply_action
    with Sandbox() as s:
        d = s.add_document(PDF, PDF.name, PAGES)
        q = make_quiz(s, d)
        assert apply_action(s, {"type": "new_folder", "parent_id": None, "name": "새 폴더"}) is None
        f = s.tree()["folders"][0]["id"]
        apply_action(s, {"type": "new_folder", "parent_id": f, "name": "새 폴더"})
        sub = next(x["id"] for x in s.tree()["folders"] if x["parent_id"] == f)
        apply_action(s, {"type": "rename", "kind": "folder", "id": f, "name": "머신러닝"})
        apply_action(s, {"type": "rename", "kind": "document", "id": d, "name": "9주차"})
        apply_action(s, {"type": "rename", "kind": "quiz", "id": q, "name": "첫 시험"})
        apply_action(s, {"type": "move", "kind": "document", "id": d, "folder_id": sub})
        assert s.document(d)["title"] == "9주차" and s.document(d)["folder_id"] == sub
        assert s.quiz(q)["name"] == "첫 시험"
        assert apply_action(s, {"type": "rename", "kind": "quiz", "id": q, "name": "   "}) == "이름이 비어 있습니다."
        err = apply_action(s, {"type": "move", "kind": "folder", "id": f, "folder_id": sub})
        assert err and "하위 폴더" in err
        apply_action(s, {"type": "delete", "kind": "folder", "id": f, "keep_contents": True})
        assert s.document(d)["folder_id"] == sub            # 하위 폴더는 맨 위로 올라가고 PDF는 그대로
        apply_action(s, {"type": "delete", "kind": "quiz", "id": q})
        assert s.quiz(q) is None
        apply_action(s, {"type": "delete", "kind": "document", "id": d})
        assert s.document(d) is None


def test_multi_pdf_quiz():
    """C-5: 두 PDF를 섞어 출제 → 청크 번호가 겹치지 않고, 문제마다 원본 PDF가 맞게 저장되며, 두 PDF 모두의 트리에 보인다."""
    with Sandbox() as s:
        d1 = s.add_document(PDF, PDF.name, PAGES, title="9주차")
        other = config.PROJECT_DIR / "10주차.pdf"
        other.write_bytes(PDF.read_bytes() + b"\n% other lecture")
        d2 = s.add_document(other, "10주차.pdf", PAGES, title="10주차")
        ext = s.extracted([d1, d2])
        ids = [c["chunk_id"] for c in ext["chunks"]]
        assert len(ids) == len(set(ids)), "청크 번호 중복"
        assert {c["doc_id"] for c in ext["chunks"]} == {d1, d2}
        assert {c["source_document"] for c in ext["chunks"]} == {"9주차", "10주차"}

        def fake(chunk_text, difficulty, num, existing, stats, model=None, conditions="", **kw):
            lines = [l for l in chunk_text.splitlines() if len(l) > 25 and "[[PAGE" not in l]
            return [GeneratedQuestion(question=f"{len(existing)}-{lines[k][:20]} 질문", choices=[f"{lines[k][:8]}{c}" for c in "가나다라"],
                                      answer_index=0, difficulty="중", evidence=lines[k]) for k in range(num)]

        orig = pl.generate_for_chunk
        pl.generate_for_chunk = fake
        try:
            qs, rep = pl.build_quiz(ext, 6, "중", history=s.history([d1, d2]), rng=random.Random(3))
        finally:
            pl.generate_for_chunk = orig
        chunk_doc = {c["chunk_id"]: c["doc_id"] for c in ext["chunks"]}
        docs_of_q = [chunk_doc[q.chunk_id] for q in qs]
        assert set(docs_of_q) == {d1, d2}, docs_of_q                             # 두 PDF 모두에서 출제
        # 시험용 10주차는 9주차와 내용이 같은 사본이라, 같은 근거 문장은 중복 검사가 걸러 낸다
        assert len(qs) == 6 or rep.discarded["중복"] > 0, (len(qs), rep.discarded)
        for q, d in zip(qs, docs_of_q):
            assert q.source_document == ("9주차" if d == d1 else "10주차")
        quiz = s.create_quiz(QuizSession(qs, seed=1), [d1, d2], "중", len(qs), doc_of_question=docs_of_q)
        for r, d in zip(s.results(quiz), docs_of_q):
            assert r["doc_id"] == d and r["source_document"] == ("9주차" if d == d1 else "10주차")
        tq = [x for x in s.tree()["quizzes"] if x["id"] == quiz]
        assert {x["doc_id"] for x in tq} == {d1, d2}                            # 두 PDF 아래 모두 표시
        hist = s.history([d1, d2])
        hist.record(qs, rep.chunks_used)
        assert {k.split(":")[0] for k in s.history([d1, d2]).chunk_usage} == {str(d1), str(d2)}


def test_balance_docs_allocation():
    """'PDF마다 비슷한 개수로': 글자가 적은 PDF(구간 2개)도 몫만큼 받는다. 끄면 정보량이 많은 PDF가 더 받는다."""
    from quizen.generate import allocate
    with Sandbox() as s:
        d1 = s.add_document(PDF, PDF.name, PAGES, title="9주차")
        other = config.PROJECT_DIR / "w03.pdf"
        other.write_bytes(PDF.read_bytes() + b"\n% w03")
        d2 = s.add_document(other, "w03.pdf", PAGES, title="W03")
        ext = s.extracted([d1, d2])
        small = [c for c in ext["chunks"] if c["doc_id"] == d2][:2]          # W03처럼 구간이 2개뿐인 PDF
        chunks = [c for c in ext["chunks"] if c["doc_id"] == d1] + small
        per_doc = lambda a: {d: sum(n for c, n in zip(chunks, a) if c["doc_id"] == d) for d in (d1, d2)}
        for seed in range(20):
            b = per_doc(allocate(chunks, ext["pages"], 6, rng=random.Random(seed), balance_docs=True))
            assert b == {d1: 3, d2: 3}, b
            b5 = per_doc(allocate(chunks, ext["pages"], 5, rng=random.Random(seed), balance_docs=True))
            assert sorted(b5.values()) == [2, 3], b5
        nb = per_doc(allocate(chunks, ext["pages"], 6, rng=None, balance_docs=False))
        assert nb[d1] > nb[d2], nb


def test_settings_persist():
    """마지막으로 고른 모델 같은 설정이 DB를 다시 열어도 남는다."""
    with Sandbox() as s:
        assert s.get_setting("model") is None and s.get_setting("model", "x") == "x"
        s.set_setting("model", "gemma4:12b")
        s.set_setting("model", "qwen3.8:27b")
        path = s.conn.execute("PRAGMA database_list").fetchone()["file"]
        assert Store(Path(path)).get_setting("model") == "qwen3.8:27b"


def test_restrict_keeps_doc_info():
    with Sandbox() as s:
        d = s.add_document(PDF, PDF.name, PAGES)
        e = restrict(s.extracted([d]), 20, 30)
        assert e["chunks"] and all(c["doc_id"] == d and c["source_document"] == PDF.stem for c in e["chunks"])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 통과")
    sys.exit(1 if failed else 0)
