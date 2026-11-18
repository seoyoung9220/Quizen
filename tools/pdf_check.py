"""B-1: PDF 텍스트 추출 품질 점검.

사용법: python tools/pdf_check.py <PDF 경로>
페이지별 추출 글자 수, 이미지 수, 이미지가 차지하는 면적 비율을 출력하고
텍스트가 거의 없는(스캔/이미지형) 페이지를 표시한다.
"""
import sys

import pymupdf

LOW_TEXT = 50  # 공백 제외 글자 수가 이보다 적으면 '텍스트 부족' 페이지로 본다


def main(path: str) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    doc = pymupdf.open(path)
    page_area_total = 0.0
    rows, low_pages, empty_pages = [], [], []

    for i, page in enumerate(doc, start=1):
        text = page.get_text("text")
        chars = len("".join(text.split()))  # 공백·줄바꿈을 뺀 실제 글자 수

        # 페이지에 그려진 이미지들의 면적 합 / 페이지 면적 → 이미지가 얼마나 덮고 있는지
        page_area = page.rect.width * page.rect.height
        img_area = 0.0
        imgs = page.get_images(full=True)
        for img in imgs:
            for r in page.get_image_rects(img[0]):
                img_area += (r & page.rect).width * (r & page.rect).height
        cover = min(img_area / page_area, 1.0) if page_area else 0.0
        page_area_total += page_area

        if chars == 0:
            empty_pages.append(i)
        elif chars < LOW_TEXT:
            low_pages.append(i)
        rows.append((i, chars, len(imgs), cover, text.strip().replace("\n", " ")[:50]))

    print(f"파일: {path}")
    print(f"페이지 수: {doc.page_count}\n")
    print(f"{'페이지':>4} {'글자수':>6} {'이미지':>4} {'이미지면적':>8}  앞부분")
    for i, chars, n, cover, head in rows:
        flag = "  <-- 텍스트 없음" if chars == 0 else ("  <-- 텍스트 적음" if chars < LOW_TEXT else "")
        print(f"{i:>4} {chars:>6} {n:>4} {cover:>8.0%}  {head}{flag}")

    total = sum(r[1] for r in rows)
    print("\n=== 요약 ===")
    print(f"총 글자 수: {total:,} / 페이지당 평균: {total / max(doc.page_count, 1):,.0f}")
    print(f"텍스트 없음 페이지 ({len(empty_pages)}): {empty_pages}")
    print(f"텍스트 적음(<{LOW_TEXT}자) 페이지 ({len(low_pages)}): {low_pages}")
    big_img = [r[0] for r in rows if r[3] >= 0.5]
    print(f"이미지가 절반 이상 덮은 페이지 ({len(big_img)}): {big_img}")


if __name__ == "__main__":
    main(sys.argv[1])
