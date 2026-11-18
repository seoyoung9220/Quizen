"""앱 화면을 Edge(화면 없는 모드)로 열어, 특정 글자가 나타날 때까지 기다린 뒤 캡처한다.

사용법: python tools/screenshot.py <저장할 png> [기다릴 글자] [URL]
예)     python tools/screenshot.py shot.png "문제 생성"
Edge 원격 조작 규약(CDP)을 websockets 라이브러리로 직접 호출한다 (Streamlit과 함께 이미 설치됨).
"""
import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333


async def capture(out: str, wait_text: str, url: str, timeout: float = 60) -> None:
    profile = tempfile.mkdtemp(prefix="edge-cdp-")
    proc = subprocess.Popen([EDGE, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
                             f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
                             "--window-size=1280,1000", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json").read())
                ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.2)
        ws = await websockets.connect(ws_url, max_size=64 * 1024 * 1024)
        seq = 0

        async def call(method, **params):
            nonlocal seq
            seq += 1
            await ws.send(json.dumps({"id": seq, "method": method, "params": params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == seq:
                    return msg.get("result", {})

        await call("Page.enable")
        await call("Page.navigate", url=url)
        deadline = time.time() + timeout
        found = False
        while time.time() < deadline:
            r = await call("Runtime.evaluate", expression="document.body ? document.body.innerText : ''",
                           returnByValue=True)
            if wait_text in r.get("result", {}).get("value", ""):
                found = True
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(1.5)        # 마지막 그리기 마무리 대기
        shot = await call("Page.captureScreenshot", format="png")
        with open(out, "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        print(f"저장: {out} ('{wait_text}' {'찾음' if found else '못 찾음, 시간 초과'})")
        await ws.close()
    finally:
        proc.terminate()


if __name__ == "__main__":
    asyncio.run(capture(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "문제 생성",
                        sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8501"))
