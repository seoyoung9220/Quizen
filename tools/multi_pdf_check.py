"""C-5 실제 모델 확인: 서로 다른 PDF 여러 개를 임시 DB에 등록해 통합 출제하고,
문제마다 원본 PDF·페이지가 맞는지(근거 문장이 그 PDF의 그 페이지에 실제로 있는지) 코드로 대조한다.

사용법: QUIZEN_DATA_DIR=<임시 폴더> python tools/multi_pdf_check.py <PDF1> <PDF2> [--num 6] [--difficulty 중]
"""
import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.pipeline import build_quiz  # noqa: E402
from quizen.quiz import QuizSession  # noqa: E402
from quizen.store import Store  # noqa: E402
from quizen.validate import normalize  # noqa: E402
from quizen.verify import verify_question  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--num", type=int, default=6)
    ap.add_argument("--difficulty", default="중")
    a = ap.parse_args()
    s = Store()
    ids = []
    for p in map(Path, a.pdfs):
        pages = analyze_pdf(p)["pages"]
        low = sum(len("".join(x["text"].split())) < 50 for x in pages)
        d = s.add_document(p, p.name, pages, title=p.stem[-20:])
        ids.append(d)
        print(f"등록: {s.document(d)['title']} ({len(pages)}p, 글자 적은 페이지 {low}개)")
    ext = s.extracted(ids)
    print(f"청크: {len(ext['chunks'])}개 " + str({s.document(d)['title']: sum(c['doc_id'] == d for c in ext['chunks']) for d in ids}))
    t = time.time()
    qs, rep = build_quiz(ext, a.num, a.difficulty, verifier=verify_question, history=s.history(ids),
                         rng=random.Random(5), progress=lambda st, i, n, m: print(f"  [{st}] {m}", flush=True))
    secs = time.time() - t
    chunk_doc = {c["chunk_id"]: c["doc_id"] for c in ext["chunks"]}
    docs_of_q = [chunk_doc[q.chunk_id] for q in qs]
    quiz = s.create_quiz(QuizSession(qs), ids, a.difficulty, a.num, doc_of_question=docs_of_q)
    print(f"\n{len(qs)}/{a.num}문제, {secs:.0f}s (문제당 {secs / max(len(qs), 1):.0f}s), 폐기 {dict(rep.discarded) or '없음'}")
    ok_all = True
    for r in s.results(quiz):
        page_text = normalize(s.document_pages(r["doc_id"]).get(r["page"], ""))
        head = normalize(r["evidence"])[:20]
        nxt = normalize(s.document_pages(r["doc_id"]).get(r["page"] + 1, ""))
        ok = head in page_text or head in page_text + nxt
        ok_all &= ok
        print(f"- [{r['source_document']} p{r['page']}] 근거가 그 PDF·페이지에 있음={ok} | {r['question'][:60]}")
    print("전체 근거·페이지 대조:", "통과" if ok_all else "불일치 있음")
    print("사이드바 표시 PDF:", sorted({x['doc_id'] for x in s.tree()['quizzes'] if x['id'] == quiz}), "회차 이름:", s.quiz(quiz)["name"])


if __name__ == "__main__":
    main()
