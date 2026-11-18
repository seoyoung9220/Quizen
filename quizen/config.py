"""앱 전체 설정. 우선순위: 환경변수 > quizen.local.json > 기본값.

quizen.local.json (프로젝트 폴더, 깃에 올리지 않음) 예:
    {"ollama_host": "http://100.x.y.z:11434",
     "models": {"gemma4:12b": "속도 우선", "gemma4:e4b": "가벼움"}}
예) PowerShell에서 모델만 바꿔 실행:
    $env:QUIZEN_MODEL = "gemma4:12b"
"""
import json
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
LOCAL_FILE = PROJECT_DIR / "quizen.local.json"
_local = json.loads(LOCAL_FILE.read_text(encoding="utf-8")) if LOCAL_FILE.exists() else {}

# 추론 서버(Ollama) 주소. 기본은 같은 컴퓨터. 다른 컴퓨터의 Ollama를 쓰면 quizen.local.json에 적는다.
OLLAMA_HOST = os.environ.get("QUIZEN_OLLAMA_HOST", _local.get("ollama_host", "http://127.0.0.1:11434"))
# 화면에서 고를 수 있는 모델 (2026-11-17 같은 조건 비교: qwen 문제당 51초·품질 우선, gemma 35초·메모리 여유)
# 앱은 마지막으로 고른 모델을 DB(meta 'model')에 기억해 다음 실행 때 config.MODEL로 쓴다.
MODELS = _local.get("models") or {
    "qwen3.8:27b": "품질 우선 · 상 난이도에 강함",
    "gemma4:12b": "속도 우선 · 메모리 여유",
}
# 환경변수로 지정한 모델은 목록에 없어도 쓰고, 화면에 저장된 선택보다 우선한다.
MODEL_FROM_ENV = "QUIZEN_MODEL" in os.environ
MODEL = os.environ.get("QUIZEN_MODEL", next(iter(MODELS)))
if MODEL not in MODELS:
    MODELS = {MODEL: "환경변수로 지정한 모델", **MODELS}

# 모델에 한 번에 넣을 수 있는 최대 토큰 수. Ollama 기본값(4096)은 너무 작아서 직접 지정한다.
# 실제 최대 사용량: 문제 생성 ≈ 자료 2,500 + 지시 900 + 이미 출제된 문제 ≤1,000 + 조건 ≤250 + 출력 ≤2,700
# ≈ 7,350토큰. 16,384에서 8,192로 줄여 Mac 메모리를 아낀다. 넘칠 것 같으면 llm.fit_context()가 막는다.
# 주의: 모든 요청이 같은 값을 써야 한다. 값이 다른 요청이 오면 Ollama가 모델을 다시 올리고,
#       다시 올린 직후 몇 분간은 자료 읽기가 약 5배 느려진다 (2026-11-17 실측).
NUM_CTX = int(os.environ.get("QUIZEN_NUM_CTX", "8192"))

# 모델을 메모리에 계속 올려 둔다 (-1 = 내리지 않음). Ollama 기본은 5분 뒤 내림.
# 내렸다 다시 올리면 위의 '느린 예열 구간'이 생기므로, Quizen 전용 Mac에서는 계속 올려 둔다.
KEEP_ALIVE = int(os.environ.get("QUIZEN_KEEP_ALIVE", "-1"))

# 청크 크기 (토큰 기준)
# 컨텍스트 안에 시스템 프롬프트(~1K) + 청크 + 기존 문제 목록(~1K) + 출력(~2.7K)이 들어가야 한다.
# 청크를 너무 크게 잡으면 모델이 앞부분 내용을 놓치기 쉬워서 여유 있게 잡는다.
CHUNK_TARGET_TOKENS = int(os.environ.get("QUIZEN_CHUNK_TOKENS", "2500"))
CHUNK_OVERLAP_TOKENS = int(os.environ.get("QUIZEN_CHUNK_OVERLAP", "300"))

# 시험할 때는 QUIZEN_DATA_DIR로 다른 폴더를 지정해 실제 기록(data/quizen.db)을 건드리지 않는다
DATA_DIR = Path(os.environ.get("QUIZEN_DATA_DIR", PROJECT_DIR / "data"))
# 토크나이저 파일은 기록이 아니라 고정 자료라서, 데이터 폴더를 바꿔도 항상 원래 위치에서 읽는다
TOKENIZER_DIR = PROJECT_DIR / "data" / "tokenizer"
# 1이면 모델(Mac Studio)을 절대 부르지 않는다. 테스트가 실수로 모델을 불러 다른 측정과 충돌하는 것을 막는다
OFFLINE = os.environ.get("QUIZEN_OFFLINE") == "1"
