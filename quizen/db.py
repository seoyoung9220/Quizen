"""C-1: SQLite 저장소. 앱을 껐다 켜도 모든 기록이 남는다.

파일: data/quizen.db (파이썬 기본 sqlite3, 추가 설치 없음)
PDF 원본은 data/uploads/ 에 파일로 두고, DB에는 경로와 정보만 저장한다.

구조 (괄호 안은 지울 때 함께 지워지는 것)
  folders      폴더 트리. parent_id로 폴더 안 폴더 가능
  documents    PDF 1개 = 1행. 같은 파일(sha256)은 한 번만 등록   (페이지, 구간 사용 이력, 이 PDF만 쓴 회차)
  doc_pages    페이지별 추출 텍스트
  chunk_usage  구간별 출제 횟수 (다음 출제 때 덜 뽑기)
  quizzes      출제 1회 = 1회차. 이름은 YYMMDD(n)                 (문제, 답, 오답 풀이)
  quiz_documents  회차에 쓴 PDF들 (여러 PDF 통합 출제)
  questions    문제. 보기는 '섞은 뒤 화면에 보인 순서'로 저장
  answers      내 답, 자동 채점, 최종 판정(내가 바꿀 수 있음), 피드백
  explanations 오답 풀이 (한 번 만든 것은 다시 만들지 않음)

삭제 규칙
- 회차 삭제: 그 회차의 문제·답·오답 풀이 삭제
- PDF 삭제: 페이지·사용 이력·원본 파일 삭제. 이 PDF만 쓴 회차는 삭제.
  여러 PDF를 섞은 회차는 남기되, 그 PDF에서 나온 문제의 원본 연결만 끊는다(파일명은 문제에 남아 있음).
- 폴더 삭제: '안의 것까지 모두 삭제' 또는 '안의 것은 상위 폴더로 옮기고 폴더만 삭제' 중 선택
"""
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import config

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS folders (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id),
  parent_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id),
  folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
  sha256 TEXT NOT NULL,
  title TEXT NOT NULL,            -- 사이드바에 보이는 이름 (바꿀 수 있음)
  filename TEXT NOT NULL,         -- 처음 올린 파일 이름
  path TEXT NOT NULL,             -- 데이터 폴더 기준 상대 경로 (예: uploads/xxxx_9주차.pdf)
  page_count INTEGER NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(user_id, sha256)
);

CREATE TABLE IF NOT EXISTS doc_pages (
  doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  page INTEGER NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY (doc_id, page)
);

CREATE TABLE IF NOT EXISTS chunk_usage (
  doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_key TEXT NOT NULL,        -- 페이지 범위 "14-19"
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (doc_id, chunk_key)
);

CREATE TABLE IF NOT EXISTS quizzes (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id),
  name TEXT NOT NULL,             -- 기본 YYMMDD(n), 바꿀 수 있음
  day TEXT NOT NULL,              -- YYMMDD (이름 번호 매기기용)
  difficulty TEXT NOT NULL,
  requested INTEGER NOT NULL,
  produced INTEGER NOT NULL,
  conditions TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT '{}',          -- 페이지 범위·키워드 (JSON)
  model TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'solving',    -- solving(풀이 중) | graded(채점 완료)
  current INTEGER NOT NULL DEFAULT 0,        -- 이어 풀기용: 마지막으로 보던 문제 위치
  gen_seconds REAL,
  report TEXT NOT NULL DEFAULT '{}',         -- 생성 보고서 (폐기 사유 등, JSON)
  parent_quiz_id INTEGER REFERENCES quizzes(id) ON DELETE SET NULL,   -- 재시험이면 원래 회차
  created_at REAL NOT NULL,
  graded_at REAL
);

CREATE TABLE IF NOT EXISTS quiz_documents (
  quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
  doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  position INTEGER NOT NULL DEFAULT 0,       -- 0이면 대표 PDF (이름 번호 기준)
  PRIMARY KEY (quiz_id, doc_id)
);

CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY,
  quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
  number INTEGER NOT NULL,
  qtype TEXT NOT NULL DEFAULT 'mcq',         -- mcq(객관식) | short(단답형) | essay(서술형)
  question TEXT NOT NULL,
  choices TEXT NOT NULL DEFAULT '[]',        -- 화면에 보인 순서 그대로 (JSON)
  answer_index INTEGER,                      -- 객관식 정답 번호 (0~3)
  answer_text TEXT,                          -- 단답·서술 정답/모범 답안
  accepted TEXT NOT NULL DEFAULT '[]',       -- 단답 인정 답안 / 서술 채점 기준 (JSON)
  difficulty TEXT NOT NULL,
  evidence TEXT NOT NULL,
  page INTEGER NOT NULL,
  evidence_pages TEXT NOT NULL DEFAULT '[]',
  doc_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
  source_document TEXT NOT NULL,             -- 원본이 지워져도 파일명은 남긴다
  chunk_key TEXT,
  origin_question_id INTEGER REFERENCES questions(id) ON DELETE SET NULL,  -- 재시험이면 원래 문제
  UNIQUE (quiz_id, number)
);

CREATE TABLE IF NOT EXISTS answers (
  question_id INTEGER PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
  user_index INTEGER,                        -- 객관식에서 고른 번호
  user_text TEXT,                            -- 단답·서술에서 쓴 답
  auto_correct INTEGER,                      -- 자동 채점 결과 (1/0, 채점 전 NULL)
  auto_score REAL,                           -- 서술형 부분 점수 등 (0~1)
  auto_detail TEXT,                          -- 모델 채점 근거 (JSON)
  final_correct INTEGER,                     -- 최종 판정 (내가 바꾸면 이 값이 바뀜)
  final_score REAL,
  overridden INTEGER NOT NULL DEFAULT 0,     -- 내가 판정을 바꿨는지
  feedback_tag TEXT,                         -- 같은 뜻 | 부분 정답 | 오타 | 직접 입력
  feedback TEXT,                             -- 피드백 내용
  answered_at REAL,
  graded_at REAL
);

CREATE TABLE IF NOT EXISTS explanations (
  question_id INTEGER PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  model TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_folder ON documents(folder_id);
CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);
CREATE INDEX IF NOT EXISTS idx_questions_quiz ON questions(quiz_id);
CREATE INDEX IF NOT EXISTS idx_questions_doc ON questions(doc_id);
CREATE INDEX IF NOT EXISTS idx_quiz_documents_doc ON quiz_documents(doc_id);
"""


def db_path() -> Path:
    return config.DATA_DIR / "quizen.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")      # 이게 있어야 ON DELETE CASCADE가 동작한다
    conn.execute("PRAGMA journal_mode = WAL")     # 쓰는 중에도 읽기 가능, 갑자기 꺼져도 덜 깨짐
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO users(id, name) VALUES (1, '나')")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """여러 작업을 한 번에 저장하거나, 중간에 오류가 나면 모두 취소한다."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def now() -> float:
    return time.time()


def to_json(v) -> str:
    return json.dumps(v, ensure_ascii=False)


@dataclass
class Duplicate(Exception):
    """같은 PDF가 이미 등록되어 있음."""
    doc_id: int
    title: str
