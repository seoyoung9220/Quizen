"""Ollama(Mac Studio) 호출을 한곳에 모은 모듈."""
import hashlib
import json
import threading
from functools import lru_cache

from ollama import Client

from . import config


class OfflineError(RuntimeError):
    """QUIZEN_OFFLINE=1(테스트)인데 모델을 부르려 함."""


@lru_cache(maxsize=1)
def _real_client() -> Client:
    return Client(host=config.OLLAMA_HOST, timeout=600)


def client() -> Client:
    if config.OFFLINE:
        raise OfflineError("테스트 환경(QUIZEN_OFFLINE=1)에서는 모델을 부를 수 없습니다.")
    return _real_client()


def loaded_models() -> list[str]:
    """Mac Studio에 지금 올라가 있는 모델 이름들."""
    return [m.model for m in client().ps().models]


def use_only(model: str) -> list[str]:
    """model 외의 모델을 메모리에서 내린다. Mac은 32GB라 두 모델이 함께 올라가면 메모리가 모자란다
    (2026-11-17 qwen+gemma 동시 적재 시 'Insufficient Memory' 오류 실측). 내린 모델 이름을 돌려준다."""
    unloaded = []
    for name in loaded_models():
        if name != model:
            client().generate(model=name, prompt="", keep_alive=0)
            unloaded.append(name)
    return unloaded


TEMPLATE_OVERHEAD = 64     # 채팅 템플릿이 메시지 사이에 넣는 특수 토큰 여유분


class ContextOverflow(ValueError):
    """보내려는 요청이 컨텍스트(NUM_CTX)를 넘는다. Ollama는 넘치면 앞부분을 조용히 잘라 버리므로 미리 막는다."""


def prompt_tokens(messages: list[dict]) -> int:
    return sum(TokenCounter()(m["content"]) for m in messages) + TEMPLATE_OVERHEAD


def fit_context(messages: list[dict], num_predict: int) -> int:
    """요청 토큰 + 최대 출력 토큰이 NUM_CTX 안에 들어가는지 확인하고, 요청 토큰 수를 돌려준다."""
    n = prompt_tokens(messages)
    if n + num_predict > config.NUM_CTX:
        raise ContextOverflow(f"요청 {n}토큰 + 출력 {num_predict}토큰 > 컨텍스트 {config.NUM_CTX}토큰")
    return n


@lru_cache(maxsize=4)
def _local_tokenizer(model: str):
    """data/tokenizer/<모델>/tokenizer.json 이 있으면 불러온다.

    Ollama 모델 안에 들어 있는 tokenizer.json을 Mac Studio에서 복사해 둔 것이다.
    (tools/fetch_tokenizer.py 참고) 모델이 직접 센 값과 51개 텍스트 모두 정확히 일치함을 확인했다.
    """
    path = config.TOKENIZER_DIR / model.replace(":", "_").replace("/", "_") / "tokenizer.json"
    if not path.exists():
        return None
    from tokenizers import Tokenizer
    return Tokenizer.from_file(str(path))


class TokenCounter:
    """모델의 토크나이저로 토큰 수를 센다.

    1순위: 이 PC에 복사해 둔 같은 토크나이저 파일 (48페이지를 0.05초 안에 셈)
    2순위: 파일이 없으면 모델에게 직접 묻는다. raw=True로 템플릿 없이 보내고 1토큰만 생성시키면
           prompt_eval_count가 입력 토큰 수다. 27B 모델에서는 페이지당 약 3초가 걸려서,
           같은 텍스트는 다시 묻지 않도록 (모델, 텍스트 해시) → 토큰 수를 디스크에 저장해 둔다.
    """

    def __init__(self, model: str | None = None):
        self.model = model or config.MODEL
        self.local = _local_tokenizer(self.model)
        safe = self.model.replace(":", "_").replace("/", "_")
        self.path = config.DATA_DIR / "cache" / f"tokens_{safe}.json"
        self._lock = threading.Lock()
        try:
            self._cache: dict[str, int] = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self._cache = {}
        self.calls = 0  # 실제로 모델에 물어본 횟수 (캐시 적중 제외)

    def __call__(self, text: str) -> int:
        if not text.strip():
            return 0
        if self.local is not None:
            return len(self.local.encode(text, add_special_tokens=False).ids)
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if key in self._cache:
            return self._cache[key]
        r = client().generate(
            model=self.model,
            prompt=text,
            raw=True,
            keep_alive=config.KEEP_ALIVE,
            options={"num_predict": 1, "num_ctx": config.NUM_CTX},
        )
        n = int(r.prompt_eval_count or 0)
        self.calls += 1
        with self._lock:
            self._cache[key] = n
        return n

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._cache), encoding="utf-8")
