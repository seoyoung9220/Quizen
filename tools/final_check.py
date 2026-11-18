"""C-9 최종 테스트: 원래 명세(Phase C 최종 테스트)의 시나리오를 실제 모델로 처음부터 끝까지 돌린다.
앱 화면과 같은 함수(Store·build_mixed·grade·grade_pending·explain_stream·wrong_questions·create_retake)를 쓴다.

시나리오
 1. PDF 여러 개 등록 (샘플을 3개로 나눠 씀) + 같은 PDF 다시 올리면 중복 감지
 2. PDF 1개로 난이도 하·중·상 각 5문제 (객관식)
 3. 3개 중 2개만 골라 10문제 (객관식 6·단답 2·서술 2, PDF별 고르게)
 4. 회차마다: JSON 구조, 근거↔원문(그 PDF의 그 페이지) 대조, 보기 섞기·정답 번호, 정답 위치 분포
 5. 일부러 몇 문제 틀리게 풀고 채점 (객관식·단답은 코드, 단답 뜻 비교·서술은 모델)
 6. 틀린 문제 오답 풀이 생성·저장
 7. 오답 노트 → 틀린 문제만 재시험 → 다 맞히면 오답 노트에서 빠지는지
측정: PDF 분석 시간, 문제당 생성 시간, 채점·풀이 시간, Mac Studio 메모리·CPU·GPU, 이 PC 메모리·CPU, 오류

사용법: QUIZEN_DATA_DIR=<임시 폴더> python tools/final_check.py <PDF>
(앱 재실행 후 데이터 유지는 tools/final_ui.py가 같은 임시 폴더로 앱을 두 번 띄워 확인)
"""
import json
import random
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

from quizen import config  # noqa: E402
from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.db import Duplicate  # noqa: E402
from quizen.explain import explain_stream  # noqa: E402
from quizen.grade_llm import grade_pending  # noqa: E402
from quizen.history import chunk_key  # noqa: E402
from quizen.llm import use_only  # noqa: E402
from quizen.pipeline import build_mixed  # noqa: E402
from quizen.quiz import QuestionResult, QuizSession  # noqa: E402
from quizen.schemas import GeneratedQuestion  # noqa: E402
from quizen.store import Store  # noqa: E402
from quizen.validate import normalize  # noqa: E402
from quizen.verify import verify_question  # noqa: E402

LOG: list[str] = []
FAIL: list[str] = []


def say(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    LOG.append(s)


def check(ok: bool, what: str):
    say(f"   {'✅' if ok else '❌'} {what}")
    if not ok:
        FAIL.append(what)


# ------------------------------------------------------------------ 자원 측정 (백그라운드)
MAC_CMD = ("ps -axo rss,%cpu,command | grep -i '[o]llama' | sort -nr | head -1; "
           "ioreg -r -d 1 -w 0 -c IOAccelerator | tr ',' '\\n' | grep -E '\"Device Utilization %\"|\"In use system memory\"='; "
           "memory_pressure | tail -1")


class Sampler(threading.Thread):
    def __init__(self, every=10):
        super().__init__(daemon=True)
        self.every, self.stop, self.rows = every, threading.Event(), []
        self.me = psutil.Process()
        self.me.cpu_percent()

    def run(self):
        while not self.stop.is_set():
            row = {"t": time.time(), "pc_rss_mb": self.me.memory_info().rss / 2**20,
                   "pc_cpu": self.me.cpu_percent(), "pc_ram_used_pct": psutil.virtual_memory().percent}
            try:
                out = subprocess.run(["ssh", "macstudio", MAC_CMD], capture_output=True, text=True, timeout=20).stdout
                lines = out.strip().splitlines()
                rss, cpu = lines[0].split()[:2]
                row.update(mac_runner_gb=int(rss) / 2**20, mac_cpu=float(cpu))
                for ln in lines[1:]:
                    if "Device Utilization" in ln:
                        row["mac_gpu_pct"] = int(ln.split("=")[1].strip("}"))
                    elif "In use system memory" in ln:
                        row["mac_gpu_mem_gb"] = int(ln.split("=")[1].strip("}")) / 2**30
                    elif "free percentage" in ln:
                        row["mac_free_pct"] = int(ln.rsplit(":", 1)[1].strip().rstrip("%"))
            except Exception as e:  # noqa: BLE001
                row["err"] = str(e)[:80]
            self.rows.append(row)
            self.stop.wait(self.every)

    def summary(self, t0=None, t1=None):
        rs = [r for r in self.rows if (t0 is None or r["t"] >= t0) and (t1 is None or r["t"] <= t1)]
        out = {}
        for k in ("mac_runner_gb", "mac_cpu", "mac_gpu_pct", "mac_gpu_mem_gb", "mac_free_pct", "pc_rss_mb", "pc_cpu",
                  "pc_ram_used_pct"):
            v = [r[k] for r in rs if k in r]
            if v:
                out[k] = {"avg": round(sum(v) / len(v), 1), "max": round(max(v), 1), "min": round(min(v), 1)}
        out["samples"] = len(rs)
        return out


# ------------------------------------------------------------------ 도우미
def split_pdf(src: Path, out_dir: Path, parts: list[tuple[int, int]]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    doc, paths = pymupdf.open(src), []
    for i, (a, b) in enumerate(parts, 1):
        new = pymupdf.open()
        new.insert_pdf(doc, from_page=a - 1, to_page=b - 1)
        p = out_dir / f"9주차_{i}부_p{a}-{b}.pdf"
        new.save(p)
        paths.append(p)
    return paths


def make_quiz(s: Store, doc_ids, counts, diff, balance=False):
    use_only(config.MODEL)
    ext = s.extracted(doc_ids)
    hist = s.history(doc_ids)
    t = time.time()
    qs, rep = build_mixed(ext, counts, diff, verifier=verify_question, history=hist, rng=random.Random(),
                          balance_docs=balance)
    secs = time.time() - t
    keys = {c["chunk_id"]: chunk_key(c) for c in ext["chunks"]}
    chunk_doc = {c["chunk_id"]: c["doc_id"] for c in ext["chunks"]}
    per_doc = {d: sum(chunk_doc[q.chunk_id] == d for q in qs) for d in doc_ids}
    ordered = sorted(doc_ids, key=lambda d: -per_doc[d])
    qid = s.create_quiz(QuizSession(qs), ordered, diff, sum(counts.values()), chunk_keys=keys,
                        doc_of_question=[chunk_doc[q.chunk_id] for q in qs],
                        scope={"counts": counts, "balance_docs": balance}, gen_seconds=secs,
                        report={"discarded": dict(rep.discarded), "samples": rep.discard_samples[:20],
                                "fallback_chunks": rep.fallback_chunks})
    hist.record(qs, rep.chunks_used)
    n = sum(counts.values())
    say(f"   생성 {len(qs)}/{n}문제 · {secs:.0f}초 (문제당 {secs / max(len(qs), 1):.0f}초) · "
        f"폐기 {dict(rep.discarded) or '없음'} · 재생성 {rep.regen_rounds_used} · 보충 {rep.fallback_chunks} · "
        f"JSON 파싱 실패 {rep.gen.parse_failures}/{rep.gen.calls}호출")
    return qid, qs, rep, secs, per_doc


def audit(s: Store, qid: int, qs) -> None:
    """JSON 구조·근거·페이지·보기 섞기·정답 번호를 코드로 다시 확인한다."""
    rows = s.results(qid)
    by_q = {q.question: q for q in qs}
    ev_ok = struct_ok = key_ok = 0
    moved, pos = 0, Counter()
    for r in rows:
        q = by_q[r["question"]]
        if r["qtype"] == "mcq":
            try:
                GeneratedQuestion(question=r["question"], choices=r["choices"], answer_index=r["answer_index"],
                                  difficulty=r["difficulty"], evidence=r["evidence"])
                struct_ok += 1
            except Exception:  # noqa: BLE001
                pass
            key_ok += r["choices"][r["answer_index"]] == q.choices[q.answer_index]
            moved += r["answer_index"] != q.answer_index
            pos[r["answer_index"] + 1] += 1
        else:
            struct_ok += bool(r["question"] and r["answer_text"] and r["evidence"])
            key_ok += 1
        pages = s.document_pages(r["doc_id"])
        span = "".join(normalize(pages.get(p, "")) for p in range(r["page"], r["page"] + 2))
        head = normalize(r["evidence"])[:20]
        ev_ok += head in span and r["source_document"] == s.document(r["doc_id"])["title"]
    n, m = len(rows), sum(r["qtype"] == "mcq" for r in rows)
    check(struct_ok == n, f"JSON 구조 검증 {struct_ok}/{n}")
    check(ev_ok == n, f"근거 문장이 표시된 PDF·페이지 원문에 있음 {ev_ok}/{n}")
    if m:
        check(key_ok == n, f"섞은 뒤에도 정답 보기가 같은 내용 {key_ok}/{n}")
        say(f"   · 보기 섞기: 객관식 {m}문제 중 {moved}문제 정답 위치 바뀜 · 정답 번호 분포 {dict(sorted(pos.items()))}")
        check(max(pos.values()) - min(pos.get(i, 0) for i in range(1, 5)) <= 2 if m >= 4 else True,
              "정답 번호가 한쪽에 몰리지 않음 (최다-최소 ≤ 2)")


def solve(s: Store, qid: int, wrong_every=2) -> dict:
    """번호가 짝수인 문제는 일부러 틀리고, 홀수는 맞힌다. 기대 결과를 돌려준다."""
    expect = {}
    for r in s.results(qid):
        right = r["number"] % wrong_every == 1
        if r["qtype"] == "mcq":
            s.save_answer(r["id"], r["answer_index"] if right else (r["answer_index"] + 1) % 4)
        elif r["qtype"] == "short":
            alt = (r["accepted"] or [r["answer_text"]])[0]
            s.save_answer(r["id"], user_text=(" " + alt.upper() + " ") if right else "잘 모르겠음")
        else:
            s.save_answer(r["id"], user_text=r["answer_text"] if right else "기억이 나지 않습니다.")
        expect[r["id"]] = right
    return expect


def grade(s: Store, qid: int, expect: dict) -> float:
    t = time.time()
    pending = s.grade(qid)
    use_only(config.MODEL)
    grade_pending(s, pending)
    secs = time.time() - t
    rows = s.results(qid)
    errs = [r for r in rows if (r["auto_detail"] or {}).get("error")]
    agree = sum(bool(r["final_correct"]) == expect[r["id"]] for r in rows)
    n, ok = s.score(qid)
    say(f"   채점 {secs:.0f}초 (모델 채점 {len(pending)}문제) · 점수 {ok}/{n} · 모델 채점 오류 {len(errs)}")
    for r in rows:
        if r["qtype"] != "mcq":
            d = r["auto_detail"] or {}
            say(f"     · {r['number']}번 {r['qtype']} 기대={'정답' if expect[r['id']] else '오답'} → "
                f"판정={'정답' if r['final_correct'] else '오답'} ({d.get('method', '')}"
                f"{', 점수 %.0f%%' % (100 * r['auto_score']) if r['qtype'] == 'essay' and r['auto_score'] is not None else ''})")
    check(agree == len(rows), f"채점 결과가 기대와 일치 {agree}/{len(rows)}")
    check(not errs, "모델 채점 오류 없음")
    return secs


def to_result(r: dict) -> QuestionResult:
    return QuestionResult(number=r["number"], question=r["question"], choices=tuple(r["choices"]),
                          difficulty=r["difficulty"], user_index=r["user_index"],
                          answer_index=r["answer_index"] if r["answer_index"] is not None else -1,
                          correct=False, evidence=r["evidence"], page=r["page"],
                          source_document=r["source_document"], chunk_id=0,
                          evidence_pages=tuple(r["evidence_pages"]), qtype=r["qtype"],
                          user_text=r["user_text"] or "", answer_text=r["answer_text"] or "")


def explain(s: Store, qid: int, limit_per_type=2) -> list[float]:
    times, seen = [], Counter()
    for r in s.results(qid):
        if r["final_correct"] or seen[r["qtype"]] >= limit_per_type:
            continue
        seen[r["qtype"]] += 1
        t = time.time()
        use_only(config.MODEL)
        text = "".join(explain_stream(to_result(r), s.document_pages(r["doc_id"])))
        s.save_explanation(r["id"], text)
        times.append(time.time() - t)
        saved = next(x for x in s.results(qid) if x["id"] == r["id"])["explanation"]
        has_loc = str(r["page"]) in text
        check(bool(saved) and len(text) > 80 and has_loc,
              f"{r['number']}번({r['qtype']}) 오답 풀이 {len(text)}자 · {times[-1]:.0f}초 · 저장됨 · 복습 페이지 표시")
        say("       " + text.replace("\n", " ")[:160] + " …")
    return times


# ------------------------------------------------------------------ 본 시나리오
def main() -> None:
    src = Path(sys.argv[1])
    out_dir = config.DATA_DIR / "final_pdfs"
    s = Store()
    smp = Sampler()
    smp.start()
    t_all = time.time()
    phases = {}
    gen_times = []

    say("\n[1] PDF 여러 개 등록 (샘플 48쪽을 16쪽씩 3개로 나눔) + 중복 감지")
    paths = split_pdf(src, out_dir, [(1, 16), (17, 32), (33, 48)])
    folder = s.create_folder("머신러닝")
    ids, analyze_times = [], []
    for p in paths:
        t = time.time()
        ext = analyze_pdf(p)
        analyze_times.append(time.time() - t)
        ids.append(s.add_document(p, p.name, ext["pages"], folder_id=folder))
        say(f"   등록 {p.name}: {len(ext['pages'])}쪽, 청크 {len(ext['chunks'])}개, 분석 {analyze_times[-1]:.1f}초")
    t = time.time()
    ext_full = analyze_pdf(src)
    full_secs = time.time() - t
    say(f"   (참고) 원본 48쪽 한 번에 분석 {full_secs:.1f}초, 청크 {len(ext_full['chunks'])}개")
    try:
        s.add_document(paths[0], "다른이름.pdf", analyze_pdf(paths[0])["pages"])
        check(False, "같은 PDF를 다른 이름으로 다시 올리면 중복으로 막힘")
    except Duplicate:
        check(True, "같은 PDF를 다른 이름으로 다시 올리면 중복으로 막힘")
    check(len(s.tree()["documents"]) == 3, "등록된 PDF 3개가 목록에 있음")

    say("\n[2] PDF 1개(1부)로 난이도별 객관식 5문제씩")
    diff_quizzes = []
    for diff in ("하", "중", "상"):
        say(f" ▶ 난이도 {diff}")
        t0 = time.time()
        qid, qs, rep, secs, _ = make_quiz(s, [ids[0]], {"mcq": 5}, diff)
        phases[f"난이도 {diff}"] = smp.summary(t0)
        gen_times.append((secs, len(qs)))
        check(len(qs) == 5, f"5문제 모두 생성 ({len(qs)}/5)")
        check(all(q.difficulty == diff for q in qs), f"모든 문제 난이도 표시 = {diff}")
        audit(s, qid, qs)
        diff_quizzes.append(qid)
        for q in qs:
            say(f"     - [p{q.page}] {q.question[:80]}")

    say("\n[3] 3개 중 2개(2부·3부)만 골라 10문제: 객관식 6 · 단답 2 · 서술 2, PDF별 고르게")
    t0 = time.time()
    mix_id, mix_qs, rep, secs, per_doc = make_quiz(s, [ids[1], ids[2]], {"mcq": 6, "short": 2, "essay": 2}, "중",
                                                  balance=True)
    phases["10문제 통합"] = smp.summary(t0)
    gen_times.append((secs, len(mix_qs)))
    check(len(mix_qs) == 10, f"10문제 모두 생성 ({len(mix_qs)}/10)")
    check(Counter(q.qtype for q in mix_qs) == Counter({"mcq": 6, "short": 2, "essay": 2}),
          f"유형별 개수 {dict(Counter(q.qtype for q in mix_qs))}")
    names = {d: s.document(d)["title"] for d in ids}
    say(f"   · PDF별 문제 수 {({names[d]: n for d, n in per_doc.items()})}")
    check(all(n >= 3 for n in per_doc.values()), "두 PDF 모두에서 고르게 출제 (각 3문제 이상)")
    check(all(r["doc_id"] in (ids[1], ids[2]) for r in s.results(mix_id)), "고르지 않은 1부에서는 출제되지 않음")
    audit(s, mix_id, mix_qs)
    for q in mix_qs:
        extra = f" → {q.answer_text}" if q.qtype == "short" else ""
        say(f"     - [{q.qtype} {q.source_document[-12:]} p{q.page}] {q.question[:70]}{extra}")

    say("\n[4] 풀이와 채점 (짝수 번호는 일부러 틀림)")
    grade_secs = []
    for qid in diff_quizzes + [mix_id]:
        say(f" ▶ {s.quiz(qid)['name']} ({s.quiz(qid)['difficulty']})")
        grade_secs.append(grade(s, qid, solve(s, qid)))

    say("\n[5] 오답 풀이 (회차당 유형별 최대 2개)")
    t0 = time.time()
    exp_times = explain(s, diff_quizzes[1]) + explain(s, mix_id)
    phases["오답 풀이"] = smp.summary(t0)

    say("\n[6] 오답 노트 → 틀린 문제만 재시험")
    wrong = s.wrong_questions(ids)
    total_wrong = sum(1 for qid in diff_quizzes + [mix_id] for r in s.results(qid) if not r["final_correct"])
    check(len(wrong) == total_wrong, f"오답 노트 {len(wrong)}문제 = 실제로 틀린 문제 {total_wrong}개")
    n_exp = sum(1 for w in wrong if w.get("explanation"))
    check(n_exp == len(exp_times), f"오답 노트에서 저장된 풀이 {n_exp}개를 다시 볼 수 있음 (만든 풀이 {len(exp_times)}개)")
    say(f"   · 오답 노트 유형별 {dict(Counter(w['qtype'] for w in wrong))}")
    # 통합 회차(세 유형)에서 틀린 문제 먼저, 6문제까지
    pick = [w["id"] for w in sorted(wrong, key=lambda w: w["quiz_id"] != mix_id)][:6]
    retake, skipped = s.create_retake(pick)
    say(f"   재시험 회차: {s.quiz(retake)['name']} · {len(s.results(retake))}문제 (뺀 문제 {skipped})")
    expect = {}
    for r in s.results(retake):                            # 이번엔 모두 맞힌다
        if r["qtype"] == "mcq":
            s.save_answer(r["id"], r["answer_index"])
        else:
            s.save_answer(r["id"], user_text=r["answer_text"])
        expect[r["id"]] = True
    grade(s, retake, expect)
    after = s.wrong_questions(ids)
    check(len(after) == len(wrong) - len(pick), f"재시험에서 맞힌 {len(pick)}문제가 오답 노트에서 빠짐 "
                                                f"({len(wrong)} → {len(after)})")

    smp.stop.set()
    total = time.time() - t_all
    n_gen = sum(n for _, n in gen_times)
    per_q = sum(sec for sec, _ in gen_times) / max(n_gen, 1)
    res = {
        "total_minutes": round(total / 60, 1),
        "analyze_seconds_16p": [round(x, 1) for x in analyze_times], "analyze_seconds_48p": round(full_secs, 1),
        "questions_generated": n_gen, "gen_seconds_per_question": round(per_q, 1),
        "gen_runs": [{"seconds": round(sec), "questions": n} for sec, n in gen_times],
        "grade_seconds": [round(x) for x in grade_secs],
        "explain_seconds": [round(x) for x in exp_times],
        "resources_all": smp.summary(), "resources_by_phase": phases,
        "failures": FAIL,
    }
    say("\n[측정 요약]")
    say(json.dumps(res, ensure_ascii=False, indent=1))
    say(f"\n결과: 확인 항목 실패 {len(FAIL)}개" + (f" → {FAIL}" if FAIL else ""))
    out = config.PROJECT_DIR / "data" / "phase_b" / f"final_check_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({**res, "log": LOG, "samples": smp.rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    say("저장:", out)


if __name__ == "__main__":
    main()
