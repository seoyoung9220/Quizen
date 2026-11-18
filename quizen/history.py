"""PDF별 출제 이력: 어떤 구간(청크)을 몇 번 썼는지, 어떤 문제를 냈는지.

- 구간 돌려쓰기: 많이 쓴 청크일수록 다음 출제에서 덜 뽑히게 한다 (generate.allocate).
- 중복 검사: 이전에 낸 문제와 비슷한 문제를 버린다 (dedup).
지금은 data/history/<PDF 해시 앞 16자>.json 에 저장한다. Phase C에서 SQLite로 옮긴다.
청크 키는 페이지 범위("14-19")라서, 같은 설정으로 분석한 같은 PDF면 회차가 달라도 같은 구간을 가리킨다.
"""
import json
from dataclasses import dataclass, field

from . import config

MAX_QUESTIONS = 300   # 너무 오래된 문제까지 비교하면 느려지므로 최근 것만 보관


def chunk_key(chunk: dict) -> str:
    """구간 이름. PDF가 여러 개 섞여도 겹치지 않게, PDF 번호가 있으면 'PDF번호:페이지범위'."""
    rng = f"{chunk['page_start']}-{chunk['page_end']}"
    return f"{chunk['doc_id']}:{rng}" if chunk.get("doc_id") is not None else rng


@dataclass
class History:
    sha: str
    chunk_usage: dict[str, int] = field(default_factory=dict)
    questions: list[dict] = field(default_factory=list)    # {"question", "choices", "evidence"}

    @property
    def path(self):
        return config.DATA_DIR / "history" / f"{self.sha[:16]}.json"

    @classmethod
    def load(cls, sha: str) -> "History":
        h = cls(sha)
        try:
            d = json.loads(h.path.read_text(encoding="utf-8"))
            h.chunk_usage = d.get("chunk_usage", {})
            h.questions = d.get("questions", [])
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return h

    def record(self, questions, chunks_used: list[dict]) -> None:
        for c in chunks_used:
            k = chunk_key(c)
            self.chunk_usage[k] = self.chunk_usage.get(k, 0) + 1
        self.questions.extend({"question": q.question, "choices": list(q.choices), "evidence": q.evidence,
                               "answer": getattr(q, "answer_text", "") if getattr(q, "qtype", "mcq") == "short" else ""}
                              for q in questions)
        self.questions = self.questions[-MAX_QUESTIONS:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"chunk_usage": self.chunk_usage, "questions": self.questions},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
