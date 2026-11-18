"""Mac Studio의 Ollama 모델에서 tokenizer.json을 SSH로 복사해 온다.

사용법: python tools/fetch_tokenizer.py [모델명]     (기본: config.MODEL)
전제: `ssh macstudio` 로 키 접속이 되어 있어야 한다 (~/.ssh/config).
모델을 바꾸면 그 모델에 대해 한 번 실행한다. 파일이 없어도 앱은 동작하지만, 새 PDF 분석이 느려진다.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quizen import config  # noqa: E402

HOST = "macstudio"
OLLAMA = "~/.ollama/models"


def main(model: str) -> None:
    name, tag = (model.split(":") + ["latest"])[:2]
    manifest = subprocess.run(
        ["ssh", HOST, f"cat {OLLAMA}/manifests/registry.ollama.ai/library/{name}/{tag}"],
        check=True, capture_output=True, text=True).stdout
    layers = {l.get("name"): l["digest"] for l in json.loads(manifest)["layers"] if l.get("name")}
    if "tokenizer.json" not in layers:
        sys.exit(f"{model}: 모델 안에 tokenizer.json이 없습니다 (GGUF 형식 모델일 수 있음).")
    digest = layers["tokenizer.json"]
    dest = config.TOKENIZER_DIR / model.replace(":", "_").replace("/", "_") / "tokenizer.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["scp", "-q", f"{HOST}:{OLLAMA}/blobs/{digest.replace(':', '-')}", str(dest)], check=True)
    got = "sha256:" + hashlib.sha256(dest.read_bytes()).hexdigest()
    if got != digest:
        dest.unlink()
        sys.exit(f"해시 불일치로 삭제함: {got} != {digest}")
    print(f"저장: {dest} ({dest.stat().st_size:,} bytes, 해시 일치)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else config.MODEL)
