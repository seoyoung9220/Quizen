"""속도 진단: 실제 출제 흐름(build_quiz)에서 생성·검증 호출마다 읽기/쓰기 시간을 출력한다.

사용법: python tools/timing_check.py <PDF> --num 3 --difficulty 중
검증의 '읽기 시간'이 생성과 비슷하게 길면, 생성 때 읽은 자료를 재사용하지 못한 것이다.
"""
import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from quizen import verify  # noqa: E402
from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.pipeline import build_quiz  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--num", type=int, default=3)
    ap.add_argument("--difficulty", default="중")
    a = ap.parse_args()
    ext = analyze_pdf(Path(a.pdf))
    verify.TIMINGS.clear()
    qs, rep = build_quiz(ext, a.num, a.difficulty, verifier=verify.verify_question, rng=random.Random(7),
                         progress=lambda s, i, n, m: print(f"  [{s}] {m}", flush=True))
    # 생성/검증 호출을 시간 순서대로 합치기는 어려우므로 종류별로 출력
    for t in rep.gen.timings + verify.TIMINGS:
        print(f"{t['kind']}  전체 {t['wall']:5.1f}s | 읽기 {t['prompt_tokens']:>5}토큰 {t['prompt_s']:5.1f}s"
              f" | 쓰기 {t['out_tokens']:>4}토큰 {t['out_s']:5.1f}s | 로딩 {t['load_s']:4.1f}s")
    print(f"\n{rep.produced}/{rep.requested}문제, 전체 {rep.seconds:.1f}s, 생성 {rep.gen.seconds:.1f}s, "
          f"검증 {rep.verify_seconds:.1f}s, 폐기 {dict(rep.discarded)}, 보충 구간 {rep.fallback_chunks}")


if __name__ == "__main__":
    main()
