"""C-2: 사이드바 내역 트리 부품과, 부품에서 온 동작(이름 바꾸기·이동·삭제 등)을 DB에 반영하는 처리.

부품 자체는 tree.js / tree.css (Streamlit Components v2, 외부 라이브러리 없음).
트리 데이터는 JSON 문자열로 넘긴다. dict로 넘기면 Streamlit이 표 데이터인지 검사하며 pyarrow를 부르는데,
이 PC는 Windows 보안 정책이 pyarrow를 막기 때문이다.
"""
import json
from pathlib import Path

import streamlit as st

from ..store import Store

_DIR = Path(__file__).parent
_component = st.components.v2.component(
    "quizen_tree",
    html='<div class="qz-mount"></div>',
    css=(_DIR / "tree.css").read_text(encoding="utf-8"),
    js=(_DIR / "tree.js").read_text(encoding="utf-8"),
)


def tree_sidebar(store: Store, selected: dict | None = None, key: str = "qz_tree") -> dict | None:
    """트리를 그리고, 이번 실행에서 들어온 동작이 있으면 돌려준다 (없으면 None)."""
    data = {**store.tree(), "selected": selected or {}}
    result = _component(data=json.dumps(data, ensure_ascii=False), key=key, on_action_change=lambda: None)
    return getattr(result, "action", None)


def apply_action(store: Store, action: dict) -> str | None:
    """DB를 바꾸는 동작을 처리한다. 화면 이동이 필요한 동작(open_quiz, new_quiz)은 호출한 쪽이 처리.
    사용자에게 보여 줄 오류 메시지가 있으면 돌려준다."""
    t, kind = action.get("type"), action.get("kind")
    try:
        if t == "new_folder":
            store.create_folder(action.get("name") or "새 폴더", action.get("parent_id"))
        elif t == "rename":
            name = (action.get("name") or "").strip()[:100]
            if not name:
                return "이름이 비어 있습니다."
            {"folder": store.rename_folder, "document": store.rename_document, "quiz": store.rename_quiz}[kind](
                action["id"], name)
        elif t == "move":
            if kind == "folder":
                store.move_folder(action["id"], action.get("folder_id"))
            elif kind == "document":
                store.move_document(action["id"], action.get("folder_id"))
        elif t == "delete":
            if kind == "folder":
                store.delete_folder(action["id"], keep_contents=bool(action.get("keep_contents", True)))
            elif kind == "document":
                store.delete_document(action["id"])
            elif kind == "quiz":
                store.delete_quiz(action["id"])
    except ValueError as e:            # 예: 폴더를 자기 하위 폴더로 옮기기
        return str(e)
    return None
