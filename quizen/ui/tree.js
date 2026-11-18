// Quizen 사이드바 내역 트리 (Streamlit Components v2)
// Python → data: JSON 문자열 {folders, documents, quizzes, selected}
// JS → Python: setTriggerValue('action', {...})  (한 번만 전달되는 이벤트)
//   open_quiz{id} | new_quiz{doc_id} | rename{kind,id,name} | move{kind,id,folder_id}
//   delete{kind,id,keep_contents} | new_folder{parent_id,name}

const LS_KEY = "quizen-tree-expanded";

function loadExpanded() {
  try { return new Set(JSON.parse(localStorage.getItem(LS_KEY) || "[]")); } catch (e) { return new Set(); }
}
function saveExpanded(set) {
  try { localStorage.setItem(LS_KEY, JSON.stringify([...set])); } catch (e) { /* 저장 못 해도 동작에는 지장 없음 */ }
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;   // textContent: 이름에 HTML이 들어가도 실행되지 않음
  return e;
}

export default function (component) {
  const { data, setTriggerValue, parentElement } = component;
  let tree;
  try { tree = JSON.parse(data || "{}"); } catch (e) { tree = {}; }
  const folders = tree.folders || [], docs = tree.documents || [], quizzes = tree.quizzes || [];
  const selected = tree.selected || {};
  const expanded = loadExpanded();

  // 이전 렌더링 결과를 지우고 새로 그린다 (데이터가 바뀔 때마다 이 함수가 다시 불린다)
  let root = parentElement.querySelector(".qz-root");
  if (root) root.remove();
  root = el("div", "qz-root");
  parentElement.appendChild(root);

  const send = (action) => setTriggerValue("action", action);
  const childFolders = (pid) => folders.filter((f) => (f.parent_id ?? null) === pid);
  const docsIn = (fid) => docs.filter((d) => (d.folder_id ?? null) === fid);
  const quizzesOf = (did) => quizzes.filter((q) => q.doc_id === did).sort((a, b) => b.created_at - a.created_at);

  function descendants(fid) {
    const out = new Set([fid]);
    let grew = true;
    while (grew) {
      grew = false;
      for (const f of folders) if (out.has(f.parent_id) && !out.has(f.id)) { out.add(f.id); grew = true; }
    }
    return out;
  }

  // ------------------------------------------------------------ 팝업 (우클릭 메뉴, 확인 창)
  let popup = null;
  function closePopup() { if (popup) { popup.remove(); popup = null; } }
  function openMenu(x, y, items) {
    closePopup();
    popup = el("div", "qz-menu");
    for (const it of items) {
      if (it === "-") { popup.appendChild(el("div", "qz-sep")); continue; }
      const b = el("div", "qz-item" + (it.danger ? " qz-danger" : ""), it.label);
      b.onclick = (e) => { e.stopPropagation(); closePopup(); it.run(); };
      popup.appendChild(b);
    }
    root.appendChild(popup);
    const r = root.getBoundingClientRect();
    popup.style.left = Math.max(0, Math.min(x - r.left, r.width - 190)) + "px";
    popup.style.top = (y - r.top) + "px";
  }
  function openConfirm(message, buttons) {
    closePopup();
    popup = el("div", "qz-confirm");
    popup.appendChild(el("div", "qz-confirm-msg", message));
    const row = el("div", "qz-confirm-btns");
    for (const b of buttons) {
      const btn = el("button", "qz-btn" + (b.danger ? " qz-btn-danger" : ""), b.label);
      btn.onclick = (e) => { e.stopPropagation(); closePopup(); if (b.run) b.run(); };
      row.appendChild(btn);
    }
    popup.appendChild(row);
    root.appendChild(popup);
  }
  function folderPicker(x, y, kind, id) {
    // 옮길 수 있는 폴더 목록 (폴더를 옮길 때는 자기 자신과 하위 폴더 제외)
    const banned = kind === "folder" ? descendants(id) : new Set();
    const items = [{ label: "📂 맨 위 (폴더 없음)", run: () => send({ type: "move", kind, id, folder_id: null }) }];
    const walk = (pid, depth) => {
      for (const f of childFolders(pid)) {
        if (banned.has(f.id)) continue;
        items.push({ label: "  ".repeat(depth) + "📁 " + f.name,
                     run: () => send({ type: "move", kind, id, folder_id: f.id }) });
        walk(f.id, depth + 1);
      }
    };
    walk(null, 0);
    openMenu(x, y, items);
  }

  // ------------------------------------------------------------ 이름 바꾸기 (그 자리에서 입력)
  function startRename(labelEl, kind, id, current) {
    closePopup();
    const input = el("input", "qz-rename");
    input.value = current;
    labelEl.replaceWith(input);
    input.focus();
    input.select();
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      const v = input.value.trim();
      if (save && v && v !== current) send({ type: "rename", kind, id, name: v });
      else input.replaceWith(labelEl);
    };
    input.onkeydown = (e) => {
      if (e.key === "Enter") finish(true);
      else if (e.key === "Escape") finish(false);
      e.stopPropagation();
    };
    input.onblur = () => finish(true);
    input.onclick = (e) => e.stopPropagation();
  }
  function newFolder(parentId) {
    send({ type: "new_folder", parent_id: parentId, name: "새 폴더" });
  }

  // ------------------------------------------------------------ 행 그리기
  function row(depth, opts) {
    const r = el("div", "qz-row" + (opts.selected ? " qz-selected" : "") + (opts.cls ? " " + opts.cls : ""));
    r.style.paddingLeft = (6 + depth * 14) + "px";
    const tw = el("span", "qz-twisty", opts.twisty ?? "");
    r.appendChild(tw);
    r.appendChild(el("span", "qz-icon", opts.icon));
    const label = el("span", "qz-label", opts.label);
    r.appendChild(label);
    if (opts.meta) r.appendChild(el("span", "qz-meta", opts.meta));
    r.title = opts.tooltip || opts.label;
    if (opts.onclick) r.onclick = (e) => { e.stopPropagation(); closePopup(); opts.onclick(); };
    if (opts.menu) r.oncontextmenu = (e) => { e.preventDefault(); e.stopPropagation(); opts.menu(e, label); };
    return r;
  }

  function toggle(key) {
    if (expanded.has(key)) expanded.delete(key); else expanded.add(key);
    saveExpanded(expanded);
    render();
  }

  function quizMeta(q) {
    if (q.status !== "graded") return `${q.difficulty} · 풀이 중`;
    const pct = q.produced ? Math.round((100 * (q.ok || 0)) / q.produced) : 0;
    return `${q.difficulty} · ${q.ok || 0}/${q.produced} · ${pct}%`;
  }

  function renderDoc(list, d, depth) {
    const key = "d" + d.id, open = expanded.has(key);
    const qs = quizzesOf(d.id);
    list.appendChild(row(depth, {
      twisty: qs.length ? (open ? "▼" : "▶") : "", icon: "📄", label: d.title, meta: `${d.page_count}p`,
      selected: selected.doc_id === d.id && !selected.quiz_id,
      onclick: () => (qs.length ? toggle(key) : send({ type: "new_quiz", doc_id: d.id })),
      menu: (e, labelEl) => openMenu(e.clientX, e.clientY, [
        { label: "✏️ 이 PDF로 문제 만들기", run: () => send({ type: "new_quiz", doc_id: d.id }) },
        "-",
        { label: "이름 바꾸기", run: () => startRename(labelEl, "document", d.id, d.title) },
        { label: "폴더로 이동…", run: () => folderPicker(e.clientX, e.clientY, "document", d.id) },
        "-",
        { label: "삭제", danger: true, run: () => openConfirm(
            `'${d.title}' PDF와 이 PDF로 만든 회차 ${qs.length}개를 삭제할까요? 되돌릴 수 없습니다.`,
            [{ label: "삭제", danger: true, run: () => send({ type: "delete", kind: "document", id: d.id }) },
             { label: "취소" }]) },
      ]),
    }));
    if (!open) return;
    for (const q of qs) {
      list.appendChild(row(depth + 1, {
        icon: q.status === "graded" ? "📝" : "⏳", label: q.name, meta: quizMeta(q), cls: "qz-quiz",
        selected: selected.quiz_id === q.id,
        onclick: () => send({ type: "open_quiz", id: q.id }),
        menu: (e, labelEl) => openMenu(e.clientX, e.clientY, [
          { label: "이름 바꾸기", run: () => startRename(labelEl, "quiz", q.id, q.name) },
          "-",
          { label: "삭제", danger: true, run: () => openConfirm(
              `'${q.name}' 회차(문제·내 답·오답 풀이)를 삭제할까요? 되돌릴 수 없습니다.`,
              [{ label: "삭제", danger: true, run: () => send({ type: "delete", kind: "quiz", id: q.id }) },
               { label: "취소" }]) },
        ]),
      }));
    }
  }

  function renderFolder(list, f, depth) {
    const key = "f" + f.id, open = expanded.has(key);
    const subs = childFolders(f.id), ds = docsIn(f.id);
    list.appendChild(row(depth, {
      twisty: subs.length || ds.length ? (open ? "▼" : "▶") : "", icon: open ? "📂" : "📁", label: f.name,
      onclick: () => toggle(key),
      menu: (e, labelEl) => openMenu(e.clientX, e.clientY, [
        { label: "이름 바꾸기", run: () => startRename(labelEl, "folder", f.id, f.name) },
        { label: "새 하위 폴더", run: () => { expanded.add(key); saveExpanded(expanded); newFolder(f.id); } },
        { label: "폴더로 이동…", run: () => folderPicker(e.clientX, e.clientY, "folder", f.id) },
        "-",
        { label: "삭제", danger: true, run: () => {
            const empty = !subs.length && !ds.length;
            if (empty) {
              openConfirm(`'${f.name}' 폴더를 삭제할까요?`,
                [{ label: "삭제", danger: true, run: () => send({ type: "delete", kind: "folder", id: f.id, keep_contents: true }) },
                 { label: "취소" }]);
            } else {
              openConfirm(`'${f.name}' 폴더 안에 폴더 ${subs.length}개, PDF ${ds.length}개가 있습니다.`,
                [{ label: "폴더만 삭제 (안의 것은 위로)", run: () => send({ type: "delete", kind: "folder", id: f.id, keep_contents: true }) },
                 { label: "안의 것까지 모두 삭제", danger: true, run: () => send({ type: "delete", kind: "folder", id: f.id, keep_contents: false }) },
                 { label: "취소" }]);
            }
          } },
      ]),
    }));
    if (!open) return;
    for (const s of subs) renderFolder(list, s, depth + 1);
    for (const d of ds) renderDoc(list, d, depth + 1);
  }

  function render() {
    closePopup();
    root.querySelectorAll(".qz-list, .qz-head, .qz-empty").forEach((n) => n.remove());
    const head = el("div", "qz-head");
    head.appendChild(el("span", "qz-title", "내 자료"));
    const add = el("button", "qz-add", "＋ 폴더");
    add.onclick = (e) => { e.stopPropagation(); newFolder(null); };
    head.appendChild(add);
    root.appendChild(head);

    const list = el("div", "qz-list");
    for (const f of childFolders(null)) renderFolder(list, f, 0);
    for (const d of docsIn(null)) renderDoc(list, d, 0);
    list.oncontextmenu = (e) => {
      e.preventDefault();
      openMenu(e.clientX, e.clientY, [{ label: "새 폴더", run: () => newFolder(null) }]);
    };
    root.appendChild(list);
    if (!folders.length && !docs.length) {
      root.appendChild(el("div", "qz-empty", "아직 올린 PDF가 없습니다. 오른쪽에서 PDF를 올려 문제를 만들어 보세요."));
    }
  }

  const onDocClick = () => closePopup();
  const onKey = (e) => { if (e.key === "Escape") closePopup(); };
  document.addEventListener("click", onDocClick);
  document.addEventListener("keydown", onKey);
  render();

  // 부품이 사라지거나 다시 그려질 때 문서 전체에 건 이벤트를 정리한다
  return () => {
    document.removeEventListener("click", onDocClick);
    document.removeEventListener("keydown", onKey);
  };
}
