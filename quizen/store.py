"""C-1: DB를 읽고 쓰는 기능 모음. 화면(app.py)은 이 함수들만 쓴다.

정답은 DB에 있지만, 화면에는 채점 전까지 question_view()(정답 없는 정보)만 넘긴다.
"""
import json
import shutil
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import config
from .db import Duplicate, connect, now, to_json, tx
from .history import chunk_key
from .pdf_extract import file_sha256
from .quiz import QuizSession

UPLOAD_DIR = config.DATA_DIR / "uploads"   # 시험 때 config.DATA_DIR를 바꾸면 함께 바꿔야 함
HISTORY_MAX_QUESTIONS = 300


class Store:
    instances: list["Store"] = []     # 테스트 정리용 (열린 연결 닫기)

    def __init__(self, path: Path | None = None):
        self.conn = connect(path)
        Store.instances.append(self)

    # ------------------------------------------------------------------ 설정 (예: 마지막으로 고른 모델)
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (f"setting:{key}",)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        with tx(self.conn) as c:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (f"setting:{key}", value))

    # ------------------------------------------------------------------ 폴더
    def create_folder(self, name: str, parent_id: int | None = None) -> int:
        with tx(self.conn) as c:
            return c.execute("INSERT INTO folders(name, parent_id, created_at) VALUES (?,?,?)",
                             (name.strip() or "새 폴더", parent_id, now())).lastrowid

    def rename_folder(self, folder_id: int, name: str) -> None:
        with tx(self.conn) as c:
            c.execute("UPDATE folders SET name=? WHERE id=?", (name.strip(), folder_id))

    def _descendants(self, folder_id: int) -> set[int]:
        rows = self.conn.execute("""
            WITH RECURSIVE sub(id) AS (SELECT ? UNION ALL SELECT f.id FROM folders f JOIN sub ON f.parent_id = sub.id)
            SELECT id FROM sub""", (folder_id,)).fetchall()
        return {r["id"] for r in rows}

    def move_folder(self, folder_id: int, new_parent_id: int | None) -> None:
        """폴더를 다른 폴더 안으로 옮긴다. 자기 자신이나 자기 하위 폴더 안으로는 못 옮긴다."""
        if new_parent_id is not None and new_parent_id in self._descendants(folder_id):
            raise ValueError("폴더를 자기 자신이나 하위 폴더 안으로 옮길 수 없습니다.")
        with tx(self.conn) as c:
            c.execute("UPDATE folders SET parent_id=? WHERE id=?", (new_parent_id, folder_id))

    def delete_folder(self, folder_id: int, keep_contents: bool) -> None:
        """keep_contents=True: 안의 폴더·PDF를 상위 폴더로 옮기고 이 폴더만 삭제.
        keep_contents=False: 하위 폴더와 그 안의 PDF(와 그 회차)까지 모두 삭제."""
        row = self.conn.execute("SELECT parent_id FROM folders WHERE id=?", (folder_id,)).fetchone()
        if row is None:
            return
        if keep_contents:
            with tx(self.conn) as c:
                c.execute("UPDATE folders SET parent_id=? WHERE parent_id=?", (row["parent_id"], folder_id))
                c.execute("UPDATE documents SET folder_id=? WHERE folder_id=?", (row["parent_id"], folder_id))
                c.execute("DELETE FROM folders WHERE id=?", (folder_id,))
            return
        ids = self._descendants(folder_id)
        q = ",".join("?" * len(ids))
        for d in self.conn.execute(f"SELECT id FROM documents WHERE folder_id IN ({q})", tuple(ids)).fetchall():
            self.delete_document(d["id"])
        with tx(self.conn) as c:
            c.execute("DELETE FROM folders WHERE id=?", (folder_id,))      # 하위 폴더는 CASCADE

    # ------------------------------------------------------------------ PDF
    def find_document_by_sha(self, sha: str):
        return self.conn.execute("SELECT * FROM documents WHERE user_id=1 AND sha256=?", (sha,)).fetchone()

    def add_document(self, src: Path, filename: str, pages: list[dict], folder_id: int | None = None,
                     title: str | None = None) -> int:
        """PDF를 등록한다. 같은 내용의 PDF가 이미 있으면 Duplicate를 던진다 (파일 이름이 달라도)."""
        sha = file_sha256(src)
        dup = self.find_document_by_sha(sha)
        if dup is not None:
            raise Duplicate(dup["id"], dup["title"])
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dest = UPLOAD_DIR / f"{sha[:16]}_{filename}"
        if src.resolve() != dest.resolve():
            shutil.copyfile(src, dest)
        rel = dest.relative_to(config.DATA_DIR).as_posix()
        with tx(self.conn) as c:
            doc_id = c.execute(
                "INSERT INTO documents(folder_id, sha256, title, filename, path, page_count, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (folder_id, sha, title or Path(filename).stem, filename, rel, len(pages), now())).lastrowid
            c.executemany("INSERT INTO doc_pages(doc_id, page, text) VALUES (?,?,?)",
                          [(doc_id, p["page"], p["text"]) for p in pages])
        return doc_id

    def document(self, doc_id: int):
        return self.conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()

    def document_path(self, doc_id: int) -> Path:
        return config.DATA_DIR / self.document(doc_id)["path"]

    def document_pages(self, doc_id: int) -> dict[int, str]:
        return {r["page"]: r["text"] for r in
                self.conn.execute("SELECT page, text FROM doc_pages WHERE doc_id=? ORDER BY page", (doc_id,))}

    def rename_document(self, doc_id: int, title: str) -> None:
        with tx(self.conn) as c:
            c.execute("UPDATE documents SET title=? WHERE id=?", (title.strip(), doc_id))

    def move_document(self, doc_id: int, folder_id: int | None) -> None:
        with tx(self.conn) as c:
            c.execute("UPDATE documents SET folder_id=? WHERE id=?", (folder_id, doc_id))

    def delete_document(self, doc_id: int) -> None:
        """PDF 삭제. 이 PDF만 쓴 회차는 삭제하고, 여러 PDF를 섞은 회차는 남긴다(문제의 원본 연결만 끊김)."""
        doc = self.document(doc_id)
        if doc is None:
            return
        only_this = [r["quiz_id"] for r in self.conn.execute("""
            SELECT qd.quiz_id FROM quiz_documents qd WHERE qd.doc_id=?
              AND (SELECT COUNT(*) FROM quiz_documents x WHERE x.quiz_id = qd.quiz_id) = 1""", (doc_id,))]
        with tx(self.conn) as c:
            if only_this:
                c.execute(f"DELETE FROM quizzes WHERE id IN ({','.join('?' * len(only_this))})", only_this)
            c.execute("DELETE FROM documents WHERE id=?", (doc_id,))   # 페이지·사용 이력은 CASCADE
        # 같은 파일을 다른 행이 쓰지 않으면 원본 파일도 지운다
        if not self.conn.execute("SELECT 1 FROM documents WHERE path=?", (doc["path"],)).fetchone():
            (config.DATA_DIR / doc["path"]).unlink(missing_ok=True)

    def extracted(self, doc_ids: list[int]) -> dict:
        """출제에 쓸 자료: 여러 PDF의 페이지·청크를 합친다. 청크와 페이지마다 PDF 번호와 이름을 붙인다.

        청크 번호는 PDF끼리 겹치지 않게 'PDF번호 × 1000 + 원래 번호'로 바꾼다.
        분석 결과는 analyze_pdf가 파일 해시로 캐시하므로 다시 계산하지 않는다.
        """
        from .analyze import analyze_pdf
        pages, chunks, names = [], [], []
        for doc_id in doc_ids:
            doc = self.document(doc_id)
            ext = analyze_pdf(config.DATA_DIR / doc["path"])
            names.append(doc["title"])
            pages += [{**p, "doc_id": doc_id} for p in ext["pages"]]
            chunks += [{**c, "doc_id": doc_id, "source_document": doc["title"],
                        "chunk_id": doc_id * 1000 + c["chunk_id"]} for c in ext["chunks"]]
        return {"source_document": " + ".join(names), "doc_ids": doc_ids,
                "page_count": sum(self.document(d)["page_count"] for d in doc_ids),
                "pages": pages, "chunks": chunks}

    # ------------------------------------------------------------------ 출제 이력 (구간 돌려쓰기·중복 검사)
    def history(self, doc_ids: list[int]) -> "DbHistory":
        return DbHistory(self, doc_ids)

    # ------------------------------------------------------------------ 회차
    def _next_name(self, primary_doc_id: int, day: str) -> str:
        n = self.conn.execute("""SELECT COUNT(*) AS n FROM quizzes q JOIN quiz_documents qd ON qd.quiz_id=q.id
                                 WHERE qd.doc_id=? AND qd.position=0 AND q.day=?""", (primary_doc_id, day)).fetchone()["n"]
        return f"{day}({n + 1})"

    def create_quiz(self, session: QuizSession, doc_ids: list[int], difficulty: str, requested: int,
                    chunk_keys: dict[int, str] | None = None, conditions: str = "", scope: dict | None = None,
                    gen_seconds: float | None = None, report: dict | None = None,
                    parent_quiz_id: int | None = None, origin_ids: list[int] | None = None,
                    doc_of_question: list[int | None] | None = None, name_suffix: str = "") -> int:
        """섞은 순서 그대로 문제를 저장하고 회차 id를 돌려준다. 이름은 대표 PDF 기준 YYMMDD(n) (+ 붙임말)."""
        day = time.strftime("%y%m%d")
        name = self._next_name(doc_ids[0], day) + name_suffix
        with tx(self.conn) as c:
            quiz_id = c.execute(
                "INSERT INTO quizzes(name, day, difficulty, requested, produced, conditions, scope, model, "
                "gen_seconds, report, parent_quiz_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, day, difficulty, requested, len(session.public), conditions, to_json(scope or {}),
                 config.MODEL, gen_seconds, to_json(report or {}), parent_quiz_id, now())).lastrowid
            c.executemany("INSERT INTO quiz_documents(quiz_id, doc_id, position) VALUES (?,?,?)",
                          [(quiz_id, d, i) for i, d in enumerate(doc_ids)])
            for i, p in enumerate(session.public):
                k = session._keys[p.qid]
                doc_id = (doc_of_question[i] if doc_of_question else None) or doc_ids[0]
                # accepted 칸: 단답은 인정 답안 목록, 서술은 채점 기준 목록 [{point, evidence, page}]
                extra = list(k.rubric) if k.qtype == "essay" else list(k.accepted)
                c.execute(
                    "INSERT INTO questions(quiz_id, number, qtype, question, choices, answer_index, answer_text, "
                    "accepted, difficulty, evidence, page, evidence_pages, doc_id, source_document, chunk_key, "
                    "origin_question_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (quiz_id, p.number, k.qtype, p.question, to_json(list(p.choices)),
                     k.answer_index if k.qtype == "mcq" else None, k.answer_text or None, to_json(extra),
                     p.difficulty, k.evidence, k.page, to_json(list(k.evidence_pages)), doc_id,
                     k.source_document, (chunk_keys or {}).get(k.chunk_id),
                     origin_ids[i] if origin_ids else None))
        return quiz_id

    def quiz(self, quiz_id: int):
        return self.conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()

    def quiz_doc_ids(self, quiz_id: int) -> list[int]:
        return [r["doc_id"] for r in self.conn.execute(
            "SELECT doc_id FROM quiz_documents WHERE quiz_id=? ORDER BY position", (quiz_id,))]

    def question_view(self, quiz_id: int) -> list[dict]:
        """풀이 화면용: 정답·근거 없이 문제와 보기, 내가 고른 답만."""
        rows = self.conn.execute("""
            SELECT q.id, q.number, q.qtype, q.question, q.choices, q.difficulty, a.user_index, a.user_text
            FROM questions q LEFT JOIN answers a ON a.question_id = q.id
            WHERE q.quiz_id=? ORDER BY q.number""", (quiz_id,)).fetchall()
        return [{"id": r["id"], "number": r["number"], "qtype": r["qtype"], "question": r["question"],
                 "choices": json.loads(r["choices"]), "difficulty": r["difficulty"],
                 "user_index": r["user_index"], "user_text": r["user_text"]} for r in rows]

    def save_answer(self, question_id: int, user_index: int | None = None, user_text: str | None = None) -> None:
        with tx(self.conn) as c:
            c.execute("""INSERT INTO answers(question_id, user_index, user_text, answered_at) VALUES (?,?,?,?)
                         ON CONFLICT(question_id) DO UPDATE SET user_index=excluded.user_index,
                         user_text=excluded.user_text, answered_at=excluded.answered_at""",
                      (question_id, user_index, user_text, now()))

    def set_current(self, quiz_id: int, current: int) -> None:
        with tx(self.conn) as c:
            c.execute("UPDATE quizzes SET current=? WHERE id=?", (current, quiz_id))

    def grade(self, quiz_id: int) -> list[int]:
        """코드로 끝나는 채점을 하고, 모델 채점이 필요한 문제 id 목록을 돌려준다.
        - 객관식: 고른 번호 = 정답 번호
        - 단답형: 공백·대소문자·문장부호를 무시하고 정답·인정 답안과 같으면 정답. 다르면 모델에게 '같은 뜻인지' 묻는다.
        - 서술형: 답을 썼으면 모델 채점. 안 쓴 문제는 오답.
        안 푼 문제는 오답. 내가 판정을 바꾼 문제는 그 판정을 유지한다. 모델 채점 결과는 set_auto_grade로 넣는다."""
        from .verify import short_matches
        rows = self.conn.execute("""SELECT q.id, q.qtype, q.answer_index, q.answer_text, q.accepted,
                                           a.user_index, a.user_text
                                    FROM questions q LEFT JOIN answers a ON a.question_id=q.id
                                    WHERE q.quiz_id=?""", (quiz_id,)).fetchall()
        pending = []
        with tx(self.conn):
            for r in rows:
                text = (r["user_text"] or "").strip()
                if r["qtype"] == "mcq":
                    ok = r["user_index"] is not None and r["user_index"] == r["answer_index"]
                    self._set_auto(r["id"], ok, 1.0 if ok else 0.0, {"method": "code"})
                elif not text:
                    self._set_auto(r["id"], False, 0.0, {"method": "code", "reason": "답을 쓰지 않음"})
                elif r["qtype"] == "short" and short_matches(text, r["answer_text"], json.loads(r["accepted"])):
                    self._set_auto(r["id"], True, 1.0, {"method": "code", "reason": "정답과 일치"})
                else:
                    pending.append(r["id"])
            self.conn.execute("UPDATE quizzes SET status='graded', graded_at=? WHERE id=?", (now(), quiz_id))
        self.conn.commit()
        return pending

    def _set_auto(self, question_id: int, correct: bool, score: float, detail: dict) -> None:
        self.conn.execute("""INSERT INTO answers(question_id, auto_correct, auto_score, auto_detail, final_correct,
                                                 final_score, graded_at) VALUES (?,?,?,?,?,?,?)
                             ON CONFLICT(question_id) DO UPDATE SET auto_correct=excluded.auto_correct,
                             auto_score=excluded.auto_score, auto_detail=excluded.auto_detail,
                             final_correct=CASE WHEN answers.overridden=1 THEN answers.final_correct
                                                ELSE excluded.final_correct END,
                             final_score=CASE WHEN answers.overridden=1 THEN answers.final_score
                                              ELSE excluded.final_score END,
                             graded_at=excluded.graded_at""",
                          (question_id, int(correct), score, to_json(detail), int(correct), score, now()))

    def set_auto_grade(self, question_id: int, correct: bool, score: float, detail: dict) -> None:
        """모델 채점 결과를 넣는다 (내가 판정을 바꾼 문제는 최종 판정 유지)."""
        with tx(self.conn):
            self._set_auto(question_id, correct, score, detail)

    def question(self, question_id: int) -> dict:
        d = dict(self.conn.execute("""SELECT q.*, a.user_index, a.user_text FROM questions q
                                      LEFT JOIN answers a ON a.question_id=q.id WHERE q.id=?""",
                                   (question_id,)).fetchone())
        return self._decode(d)

    @staticmethod
    def _decode(d: dict) -> dict:
        d["choices"] = json.loads(d["choices"])
        d["evidence_pages"] = json.loads(d["evidence_pages"])
        extra = json.loads(d.get("accepted") or "[]")
        d["accepted"] = extra if d["qtype"] == "short" else []
        d["rubric"] = extra if d["qtype"] == "essay" else []
        if "auto_detail" in d:
            d["auto_detail"] = json.loads(d["auto_detail"] or "{}")
        return d

    def override(self, question_id: int, correct: bool, tag: str = "", feedback: str = "") -> None:
        """채점 판정을 내가 바꾼다 (예: '같은 뜻이야', '오타야'). 점수와 오답 노트에 바로 반영된다."""
        with tx(self.conn) as c:
            c.execute("""INSERT INTO answers(question_id, final_correct, overridden, feedback_tag, feedback)
                         VALUES (?,?,1,?,?) ON CONFLICT(question_id) DO UPDATE SET
                         final_correct=excluded.final_correct, overridden=1,
                         feedback_tag=excluded.feedback_tag, feedback=excluded.feedback""",
                      (question_id, int(correct), tag, feedback))

    def clear_override(self, question_id: int) -> None:
        with tx(self.conn) as c:
            c.execute("""UPDATE answers SET final_correct=auto_correct, overridden=0, feedback_tag=NULL,
                         feedback=NULL WHERE question_id=?""", (question_id,))

    def results(self, quiz_id: int) -> list[dict]:
        """채점 후 보기: 문제, 보기, 내 답, 정답, 판정, 근거, 페이지, 오답 풀이."""
        rows = self.conn.execute("""
            SELECT q.*, a.user_index, a.user_text, a.auto_correct, a.auto_score, a.auto_detail, a.final_correct,
                   a.final_score, a.overridden, a.feedback_tag, a.feedback, e.text AS explanation
            FROM questions q LEFT JOIN answers a ON a.question_id=q.id
            LEFT JOIN explanations e ON e.question_id=q.id
            WHERE q.quiz_id=? ORDER BY q.number""", (quiz_id,)).fetchall()
        out = []
        for r in rows:
            d = self._decode(dict(r))
            d["correct"] = bool(d["final_correct"])
            out.append(d)
        return out

    def score(self, quiz_id: int) -> tuple[int, int]:
        r = self.conn.execute("""SELECT COUNT(*) AS total, SUM(COALESCE(a.final_correct,0)) AS ok
                                 FROM questions q LEFT JOIN answers a ON a.question_id=q.id
                                 WHERE q.quiz_id=?""", (quiz_id,)).fetchone()
        return r["total"], r["ok"] or 0

    def save_explanation(self, question_id: int, text: str) -> None:
        with tx(self.conn) as c:
            c.execute("INSERT OR REPLACE INTO explanations(question_id, text, model, created_at) VALUES (?,?,?,?)",
                      (question_id, text, config.MODEL, now()))

    def rename_quiz(self, quiz_id: int, name: str) -> None:
        with tx(self.conn) as c:
            c.execute("UPDATE quizzes SET name=? WHERE id=?", (name.strip(), quiz_id))

    def delete_quiz(self, quiz_id: int) -> None:
        with tx(self.conn) as c:
            c.execute("DELETE FROM quizzes WHERE id=?", (quiz_id,))     # 문제·답·풀이는 CASCADE

    # ------------------------------------------------------------------ 사이드바 트리
    def tree(self) -> dict:
        """폴더·PDF·회차를 한 번에 읽어 사이드바가 그릴 수 있는 형태로 돌려준다."""
        folders = [dict(r) for r in self.conn.execute("SELECT id, parent_id, name FROM folders ORDER BY name")]
        docs = [dict(r) for r in self.conn.execute(
            "SELECT id, folder_id, title, page_count FROM documents ORDER BY title")]
        quizzes = [dict(r) for r in self.conn.execute("""
            SELECT q.id, q.name, q.difficulty, q.status, q.produced, q.created_at, qd.doc_id,
                   (SELECT SUM(COALESCE(a.final_correct,0)) FROM questions x LEFT JOIN answers a ON a.question_id=x.id
                    WHERE x.quiz_id=q.id) AS ok
            FROM quizzes q JOIN quiz_documents qd ON qd.quiz_id=q.id
            ORDER BY q.created_at""")]
        return {"folders": folders, "documents": docs, "quizzes": quizzes}

    # ------------------------------------------------------------------ 오답 노트·재시험
    def wrong_questions(self, doc_ids: list[int] | None = None, quiz_id: int | None = None) -> list[dict]:
        """틀린 문제 (최종 판정 기준). 재시험에서 다시 맞힌 문제는 빼고, 가장 최근 시도 기준으로 본다."""
        sql = """SELECT q.*, a.user_index, a.user_text, a.final_correct, a.auto_score, a.auto_detail, a.overridden,
                        a.feedback_tag, a.feedback, qz.name AS quiz_name, qz.created_at AS quiz_created,
                        e.text AS explanation
                 FROM questions q JOIN answers a ON a.question_id=q.id JOIN quizzes qz ON qz.id=q.quiz_id
                 LEFT JOIN explanations e ON e.question_id=q.id
                 WHERE qz.status='graded' AND a.final_correct=0"""
        args: list = []
        if quiz_id is not None:
            sql += " AND q.quiz_id=?"
            args.append(quiz_id)
        if doc_ids:
            sql += f" AND q.doc_id IN ({','.join('?' * len(doc_ids))})"
            args += doc_ids
        sql += " ORDER BY qz.created_at DESC, q.number"
        rows = [dict(r) for r in self.conn.execute(sql, args)]
        # 같은 원래 문제를 여러 번 틀렸으면 한 번만, 이후 재시험에서 맞혔으면 제외
        solved = {r["origin_question_id"] for r in self.conn.execute("""
            SELECT q.origin_question_id FROM questions q JOIN answers a ON a.question_id=q.id
            WHERE q.origin_question_id IS NOT NULL AND a.final_correct=1""")}
        seen, out = set(), []
        for r in rows:
            root = r["origin_question_id"] or r["id"]
            if root in seen or root in solved:
                continue
            seen.add(root)
            out.append(self._decode(r))
        return out

    def folder_doc_ids(self, folder_id: int) -> list[int]:
        """폴더와 그 하위 폴더 전체에 들어 있는 PDF id."""
        ids = self._descendants(folder_id)
        q = ",".join("?" * len(ids))
        return [r["id"] for r in self.conn.execute(f"SELECT id FROM documents WHERE folder_id IN ({q})", tuple(ids))]

    def create_retake(self, question_ids: list[int]) -> tuple[int | None, int]:
        """틀린 문제만 다시 풀기: 고른 문제로 새 회차를 만든다. (회차 id, 원본 PDF가 지워져 뺀 문제 수)
        - 객관식은 보기를 새로 섞는다 (정답 위치도 고르게 다시 배정)
        - 각 문제는 '원래 문제'(origin_question_id)를 기억해서, 맞히면 오답 노트에서 빠진다
        - 모든 문제가 한 회차에서 왔으면 그 회차를 부모로 기록한다"""
        from collections import Counter

        from .validate import ValidQuestion
        rows = [self.question(i) for i in question_ids]
        skipped = sum(r["doc_id"] is None for r in rows)
        rows = [r for r in rows if r["doc_id"] is not None]
        if not rows:
            return None, skipped
        qs = []
        for r in rows:
            qs.append(ValidQuestion(
                question=r["question"], choices=r["choices"],
                answer_index=r["answer_index"] if r["answer_index"] is not None else 0,
                difficulty=r["difficulty"], evidence=r["evidence"], page=r["page"],
                evidence_pages=r["evidence_pages"], evidence_mode="full", source_document=r["source_document"],
                chunk_id=0, qtype=r["qtype"], answer_text=r["answer_text"] or "",
                accepted=r["accepted"], rubric=r["rubric"]))
        doc_ids = list(dict.fromkeys(r["doc_id"] for r in rows))
        doc_ids.sort(key=lambda d: -sum(r["doc_id"] == d for r in rows))      # 문제가 많은 PDF가 대표
        parents = {r["quiz_id"] for r in rows}
        counts = Counter(r["qtype"] for r in rows)
        quiz_id = self.create_quiz(
            QuizSession(qs), doc_ids, Counter(r["difficulty"] for r in rows).most_common(1)[0][0], len(qs),
            scope={"counts": dict(counts), "retake": True},
            parent_quiz_id=parents.pop() if len(parents) == 1 else None,
            origin_ids=[r["origin_question_id"] or r["id"] for r in rows],
            doc_of_question=[r["doc_id"] for r in rows], name_suffix=" 재시험")
        return quiz_id, skipped


class DbHistory:
    """pipeline.build_quiz가 쓰는 출제 이력 (history.History와 같은 모양, 저장소만 DB)."""

    def __init__(self, store: Store, doc_ids: list[int]):
        self.store = store
        self.doc_ids = doc_ids
        q = ",".join("?" * len(doc_ids))
        self.chunk_usage = {f"{r['doc_id']}:{r['chunk_key']}": r["count"] for r in store.conn.execute(
            f"SELECT doc_id, chunk_key, count FROM chunk_usage WHERE doc_id IN ({q})", doc_ids)}
        self.questions = [{"question": r["question"], "choices": json.loads(r["choices"]), "evidence": r["evidence"],
                           "answer": r["answer_text"] if r["qtype"] == "short" else ""}
                          for r in store.conn.execute(
                              f"SELECT question, choices, evidence, qtype, answer_text FROM questions WHERE doc_id IN ({q}) "
                              f"ORDER BY id DESC LIMIT {HISTORY_MAX_QUESTIONS}", doc_ids)][::-1]

    def record(self, questions, chunks_used: list[dict]) -> None:
        """구간 사용 횟수만 올린다 (문제 자체는 create_quiz가 questions 테이블에 저장)."""
        with tx(self.store.conn) as c:
            for ch in chunks_used:
                rng = f"{ch['page_start']}-{ch['page_end']}"
                c.execute("""INSERT INTO chunk_usage(doc_id, chunk_key, count) VALUES (?,?,1)
                             ON CONFLICT(doc_id, chunk_key) DO UPDATE SET count = count + 1""",
                          (ch["doc_id"], rng))
        for ch in chunks_used:
            k = chunk_key(ch)
            self.chunk_usage[k] = self.chunk_usage.get(k, 0) + 1
