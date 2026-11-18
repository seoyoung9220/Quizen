# Quizen

PDF 강의자료로 문제(객관식·단답·서술)를 만들고, 채점하고, 틀린 문제를 풀이해 주는 개인용 앱입니다.
모든 추론은 **내 컴퓨터(또는 내가 관리하는 컴퓨터)의 Ollama**에서 합니다. 외부 API·클라우드 LLM은 쓰지 않습니다.

- 여러 PDF를 폴더로 관리하고, 여러 PDF를 묶어 한 회차로 출제
- 문제 수·난이도(하/중/상)·유형 비율·추가 조건 설정
- 모든 문제에 근거 문장과 페이지 표시 (근거가 원문에 없으면 코드가 버림)
- 객관식은 보기 순서를 섞음, 단답·서술형은 모델이 채점하고 판정을 직접 바꿀 수 있음
- 오답 풀이, 오답 노트, 재시험. 기록은 SQLite(`data/quizen.db`)에 저장

## 1. 컴퓨터 사양별 권장 구성

Quizen은 문제 하나를 만들 때 모델을 여러 번 부릅니다(생성 → 근거 확인 → 정답 재확인).
그래서 **모델 크기가 품질과 속도를 거의 다 결정합니다.** 아래 표에서 내 컴퓨터에 맞는 줄을 고르세요.

> 실측: **Apple M1 Max 32GB + `qwen3.8:27b` 조합 하나뿐입니다.** 나머지 줄은 모델 파일 크기를 보고 고른
> 권장안이고, 직접 측정하지는 않았습니다. 모델 이름과 크기는 2026-11 기준 Ollama 라이브러리 값입니다.

### Apple Silicon Mac (Ollama가 MLX 엔진 사용)

| 통합 메모리 | 권장 모델 (기본 / 가벼운 선택) | 모델 파일 | 비고 |
|---|---|---|---|
| 64GB 이상 | `qwen3.8:27b-mxfp8` / `qwen3.8:27b` | 30GB / 18GB | 정밀도가 더 높은 버전을 쓸 여유가 있음 |
| **32GB** ✅ 실측 | **`qwen3.8:27b`** / `gemma4:12b` | 18GB / 7.7GB | 실행 중 약 25GB 사용(측정값). 문제당 평균 약 47초, '상'은 약 65초 |
| 24GB | `gemma4:12b` / `gemma4:e4b` | 7.7GB / 9.5GB | 27b 모델은 메모리가 부족함 |
| 16GB | `gemma4:12b` / `gemma4:e4b` | 7.7GB / 9.5GB | 다른 프로그램을 닫고 쓰기 |
| 8GB | 권장하지 않음 (`gemma4:e2b`는 가능) | 7.5GB | 근거 확인·채점 품질이 크게 떨어질 수 있음 |

### Windows·Linux + NVIDIA 그래픽카드

| 그래픽 메모리(VRAM) | 권장 모델 | 모델 파일 | 비고 |
|---|---|---|---|
| 24GB 이상 (RTX 3090·4090 등) | `qwen3.8:27b-q4_K_M` | 18GB | 실측 구성과 같은 모델 크기 |
| 12~16GB | `gemma4:12b-it-q4_K_M` 또는 `gemma4:12b-it-qat` | 8.0GB / 7.2GB | |
| 8GB | `gemma4:e4b-it-q4_K_M` | 6.6GB | '상' 난이도 품질은 떨어질 수 있음 |
| 6GB 이하 | 이 컴퓨터에서 모델을 돌리지 말고 아래 "다른 컴퓨터의 Ollama 쓰기"를 권장 | — | 예) GTX 1060 6GB는 이 앱 기준으로 느리고 품질이 낮음 |

### 그래픽카드가 없는 컴퓨터

CPU만으로는 문제 하나에 몇 분씩 걸려 권장하지 않습니다.
메모리가 넉넉한 다른 컴퓨터에서 Ollama를 돌리고 이 컴퓨터에서는 앱만 실행하세요.

> 주의: Ollama 라이브러리의 `:cloud`가 붙은 모델(예: `gemma4:cloud`)은 외부 서버에서 돌아갑니다.
> 이 앱의 원칙(로컬 추론)에 맞지 않으므로 쓰지 마세요.

## 2. 소프트웨어 버전 (실측 환경)

| 구분 | 버전 |
|---|---|
| 앱을 실행한 PC | Windows 11, Python 3.12.10 |
| 파이썬 패키지 | `requirements.txt` 참고 (streamlit 1.67.0, pymupdf 2.1, ollama 0.6.3, pydantic 2.14.3, tokenizers 0.23.3) |
| 추론 서버 | macOS 26.5.2, Ollama 0.40.16 (MLX 엔진) |

Python 3.12 이상을 권장합니다. Ollama는 위 버전이나 그보다 새 버전을 쓰세요.

## 3. 설치

### 3-1. Ollama와 모델 (추론할 컴퓨터)

1. <https://ollama.com>에서 Ollama를 설치합니다.
2. 위 표에서 고른 모델을 받습니다. 예: `ollama pull qwen3.8:27b`

### 3-2. 앱 (앱을 실행할 컴퓨터)

```powershell
git clone <이 저장소 주소> Quizen
cd Quizen
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3-3. 설정 파일 `quizen.local.json` (선택)

Ollama가 같은 컴퓨터에 있고 기본 모델(`qwen3.8:27b`, `gemma4:12b`)을 쓴다면 설정이 필요 없습니다.
그렇지 않으면 프로젝트 폴더에 `quizen.local.json`을 만듭니다. 이 파일은 깃에 올라가지 않습니다.

```json
{
  "ollama_host": "http://127.0.0.1:11434",
  "models": {
    "gemma4:12b-it-q4_K_M": "기본 · 12GB 그래픽카드용",
    "gemma4:e4b-it-q4_K_M": "가벼움 · 속도 우선"
  }
}
```

- `ollama_host`: Ollama 주소. 다른 컴퓨터의 Ollama를 쓰면 그 컴퓨터 주소를 적습니다.
- `models`: 화면에서 고를 수 있는 모델 목록. **첫 번째가 기본 모델**입니다. 반드시 `ollama pull`로 받아 둔 이름이어야 합니다.

환경변수로도 바꿀 수 있습니다. 환경변수가 설정 파일보다 우선합니다.

| 환경변수 | 뜻 | 기본값 |
|---|---|---|
| `QUIZEN_OLLAMA_HOST` | Ollama 주소 | `http://127.0.0.1:11434` |
| `QUIZEN_MODEL` | 쓸 모델 | `models`의 첫 번째 |
| `QUIZEN_NUM_CTX` | 한 번에 넣는 최대 토큰 수 | `8192` (줄이면 출제가 막힐 수 있음) |
| `QUIZEN_KEEP_ALIVE` | 모델을 메모리에 남겨 둘 시간(초). `-1`은 계속 | `-1` (메모리가 빠듯하면 `300`) |
| `QUIZEN_DATA_DIR` | 기록 폴더 | `data` |

## 4. 실행

```powershell
.\run.ps1
```

브라우저에서 <http://127.0.0.1:8501>을 엽니다. 앱은 이 컴퓨터 안에서만 열립니다.

## 5. 다른 컴퓨터의 Ollama 쓰기 / 내 다른 기기에서 접속하기

[Tailscale](https://tailscale.com)처럼 내 기기끼리만 연결되는 사설망을 권장합니다.

- **Ollama를 다른 컴퓨터에서 돌릴 때:** 그 컴퓨터에서 `OLLAMA_HOST`를 사설망 주소로 지정해 Ollama를 실행하고,
  앱 쪽 `quizen.local.json`의 `ollama_host`에 그 주소를 적습니다.
- **휴대폰·노트북에서 앱에 접속할 때:** `.\run.ps1 -Remote`로 실행합니다. 앱은 이 PC의 Tailscale 주소에만 열립니다.

> ⚠️ Ollama나 앱을 `0.0.0.0`(모든 네트워크)에 열지 마세요. 공인 IP를 쓰는 컴퓨터라면 인터넷 전체에 노출됩니다.

## 6. 참고

- **토크나이저 파일 (선택):** `data/tokenizer/<모델>/tokenizer.json`이 있으면 토큰 수를 이 PC에서 바로 셉니다.
  없으면 모델에게 물어서 세므로 PDF 분석이 조금 느려질 뿐 동작은 같습니다.
  `tools/fetch_tokenizer.py`로 Ollama 컴퓨터에서 복사해 올 수 있습니다(SSH 접속 필요).
- **OCR(이미지 속 글자 읽기)은 없습니다.** 스캔한 PDF처럼 글자를 선택·복사할 수 없는 PDF로는 문제를 만들 수 없습니다.
- **테스트:** `$env:QUIZEN_OFFLINE = "1"` 상태에서 `python tests\test_*.py`를 실행합니다. 모델을 부르지 않습니다.
  일부 테스트와 `tools/` 스크립트는 저작권 때문에 저장소에 넣지 않은 강의자료 PDF(`samples/`)가 있어야 돌아갑니다.
