"""C-9 최종 테스트 (화면·재실행): final_check.py가 만든 임시 데이터 폴더로 앱을 띄워 실제 브라우저로 확인하고,
앱을 완전히 끈 뒤 다시 켜도 같은 데이터(폴더·PDF·회차·점수·오답 노트·저장된 풀이)가 보이는지 본다.

사용법: python tools/final_ui.py <임시 데이터 폴더>   (앱은 127.0.0.1:8502에 따로 띄움, 원래 앱과 기록은 건드리지 않음)
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT, URL = 8502, "http://127.0.0.1:8502"
SHOT_DIR = Path(tempfile.gettempdir()) / "quizen-final-shots"
HELPER = """
window.qzRoot = () => { for (const e of document.querySelectorAll('*')) { if (e.shadowRoot && e.shadowRoot.querySelector('.qz-root')) return e.shadowRoot; } return null; };
window.qzLabels = () => { const r = qzRoot(); return r ? [...r.querySelectorAll('.qz-label')].map(x => x.textContent.trim()) : []; };
window.qzOpenAll = () => { const r = qzRoot(); let n = 0; if (!r) return -1;
  for (const x of r.querySelectorAll('.qz-row')) { const t = x.querySelector('.qz-twisty'); if (t && t.textContent === '▶') { x.click(); n++; } } return n; };
window.qzClick = (t) => { const r = qzRoot(); const x = r && [...r.querySelectorAll('.qz-row')].find(x => x.querySelector('.qz-label').textContent.trim() === t);
  if (!x) return 'no-row:' + t; x.click(); return 'ok'; };
window.btn = (t) => { const b = [...document.querySelectorAll('button')].find(b => b.innerText.includes(t)); if (!b) return 'no-button:' + t; b.click(); return 'ok'; };
"""


def start_app(data_dir: str) -> subprocess.Popen:
    env = {**os.environ, "QUIZEN_DATA_DIR": data_dir}
    env.pop("QUIZEN_OFFLINE", None)
    p = subprocess.Popen([PY, "-m", "streamlit", "run", "app.py", "--server.port", str(PORT),
                          "--server.address", "127.0.0.1"], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        try:
            if urllib.request.urlopen(URL + "/_stcore/health", timeout=2).read() == b"ok":
                return p
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise RuntimeError("앱이 켜지지 않음")


def stop_app(p: subprocess.Popen) -> None:
    p.terminate()
    p.wait(20)


async def look(tag: str) -> dict:
    """앱을 열어 트리·회차 결과·오답 노트를 읽는다."""
    prof = tempfile.mkdtemp()
    edge = subprocess.Popen([EDGE, "--headless=new", "--disable-gpu", "--remote-debugging-port=9339",
                             f"--user-data-dir={prof}", "--window-size=1300,1400", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = {}
    try:
        for _ in range(50):
            try:
                ws_url = next(t["webSocketDebuggerUrl"] for t in
                              json.loads(urllib.request.urlopen("http://127.0.0.1:9339/json").read())
                              if t["type"] == "page")
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.2)
        ws = await websockets.connect(ws_url, max_size=64 << 20)
        seq = 0

        async def call(m, **pr):
            nonlocal seq
            seq += 1
            await ws.send(json.dumps({"id": seq, "method": m, "params": pr}))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == seq:
                    return r.get("result", {})

        async def ev(e):
            return (await call("Runtime.evaluate", expression=e, returnByValue=True)).get("result", {}).get("value")

        async def wait(e, t=40):
            for _ in range(int(t / 0.25)):
                v = await ev(e)
                if v:
                    return v
                await asyncio.sleep(0.25)

        async def shot(name):
            import base64
            SHOT_DIR.mkdir(exist_ok=True)
            d = await call("Page.captureScreenshot", format="png")
            p = SHOT_DIR / f"{tag}_{name}.png"
            p.write_bytes(base64.b64decode(d["data"]))
            return str(p)

        await call("Page.navigate", url=URL)
        await wait("document.body && document.body.innerText.includes('새 문제 만들기')")
        await ev(HELPER)
        await wait("window.qzRoot && qzRoot()")
        for _ in range(4):                                  # 폴더 → PDF → 회차까지 모두 펼침
            await ev("qzOpenAll()")
            await asyncio.sleep(0.6)
        labels = await ev("qzLabels()")
        out["tree"] = labels
        out["shot_tree"] = await shot("tree")
        quiz = next((x for x in labels if "재시험" in x), None)
        if quiz:
            await ev(f"qzClick({json.dumps(quiz)})")
            await wait("document.body.innerText.includes('정답률')")
            body = await ev("document.body.innerText")
            out["retake_view"] = {"name": quiz, "has_score": "정답률" in body,
                                  "score_line": next((ln for ln in body.splitlines() if "정답률" in ln), "")}
            out["shot_retake"] = await shot("retake")
        await ev("btn('오답 노트')")
        await wait("document.body.innerText.includes('틀린 문제')")
        body = await ev("document.body.innerText")
        out["wrong_note"] = next((ln for ln in body.splitlines() if "틀린 문제" in ln), "")
        out["wrong_note_has_explanation"] = "헷갈리지 않는 팁" in body or "오답 풀이" in body
        out["shot_wrong"] = await shot("wrong")
        await ws.close()
    finally:
        edge.terminate()
    return out


def main() -> None:
    data_dir = sys.argv[1]
    results = []
    for i in (1, 2):
        p = start_app(data_dir)
        print(f"[{i}번째 실행] 앱 켜짐 (pid {p.pid})", flush=True)
        try:
            r = asyncio.run(look(f"run{i}"))
        finally:
            stop_app(p)
        print(f"   앱 완전히 종료 (종료 코드 {p.returncode})")
        print("   트리:", r["tree"])
        print("   재시험 회차 화면:", r.get("retake_view"))
        print("   오답 노트:", r["wrong_note"], "| 저장된 풀이 보임:", r["wrong_note_has_explanation"])
        print("   화면 캡처:", r["shot_tree"], r.get("shot_retake"), r["shot_wrong"])
        results.append(r)
    a, b = results
    same = a["tree"] == b["tree"] and a.get("retake_view") == b.get("retake_view") and a["wrong_note"] == b["wrong_note"]
    print("\n재실행 전후 같은 데이터:", "✅ 같음" if same else "❌ 다름")


if __name__ == "__main__":
    main()
