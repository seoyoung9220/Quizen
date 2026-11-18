"""출제 범위(페이지·키워드)와 추가 조건 프롬프트 테스트. 모델을 부르지 않는다.
실행: python tests/test_scope.py
"""
import os
os.environ["QUIZEN_OFFLINE"] = "1"   # 테스트는 절대 모델(Mac Studio)을 부르지 않는다
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import quizen.pipeline as pl  # noqa: E402
from quizen import prompts  # noqa: E402
from quizen.analyze import analyze_pdf  # noqa: E402
from quizen.scope import parse_keywords, restrict  # noqa: E402

PDF = ROOT / "samples" / "sample.pdf"


def ext():
    return analyze_pdf(PDF)


def test_page_range_only_pages_inside():
    e = restrict(ext(), 20, 30)
    assert e["chunks"], "청크가 없음"
    for c in e["chunks"]:
        assert 20 <= c["page_start"] and c["page_end"] <= 30, c["pages"]
        marks = [int(x) for x in __import__("re").findall(r"\[\[PAGE (\d+)\]\]", c["text"])]
        assert all(20 <= m <= 30 for m in marks), marks           # 범위 밖 페이지 표시가 없음
    assert [p["page"] for p in e["pages"]] == list(range(20, 31))


def test_full_range_keeps_original_chunks():
    full = ext()
    e = restrict(full, 1, full["page_count"])
    assert e["chunks"] == full["chunks"]


def test_keyword_filter():
    e = restrict(ext(), keywords=["학습 곡선"])
    assert e["chunks"] and all("학습곡선" in "".join(c["text"].split()) for c in e["chunks"])
    assert len(e["chunks"]) < len(ext()["chunks"])


def test_keyword_no_match_gives_empty():
    assert restrict(ext(), keywords=["양자역학"])["chunks"] == []


def test_parse_keywords():
    assert parse_keywords(" 경사 하강법, SGD ,\n학습률,,") == ["경사 하강법", "SGD", "학습률"]


def test_conditions_in_prompt_after_material():
    msgs = prompts.build_gen_messages("자료 본문", "중", 3, "", conditions="실제 사례를 넣어서")
    user = msgs[1]["content"]
    assert "[사용자 추가 조건]" in user and "실제 사례를 넣어서" in user
    assert user.index("자료 본문") < user.index("[사용자 추가 조건]")   # 자료 뒤에 와야 캐시가 유지됨
    assert "[사용자 추가 조건]" not in prompts.build_gen_messages("자료 본문", "중", 3, "")[1]["content"]
    long = prompts.build_gen_messages("x", "중", 1, "", conditions="꿁" * 2000)[1]["content"]
    assert long.count("꿁") == prompts.CONDITIONS_MAX                    # 길이 제한


def test_pipeline_passes_conditions():
    seen = []

    def fake(chunk_text, difficulty, num, existing, stats, model=None, conditions="", **kw):
        seen.append(conditions)
        return []

    orig = pl.generate_for_chunk
    pl.generate_for_chunk = fake
    try:
        pl.build_quiz(restrict(ext(), 20, 26), 1, "중", conditions="보기를 짧게")
    finally:
        pl.generate_for_chunk = orig
    assert seen and all(c == "보기를 짧게" for c in seen), seen


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 통과")
    sys.exit(1 if failed else 0)
