"""엔진 고르기 — Codex · Claude · 자동(Codex 먼저, 사용 한도에 걸리면 Claude로).

  ask(prompt, schema)   설정(_settings.json engine)대로 부른다. 자동이면 한도에 걸린 엔진은 잠시 건너뛴다.
  CANCEL                [멈춤]을 누르면 켜진다 — 다음 호출부터 Cancelled 로 멈춘다 (이미 끝난 구간은 캐시에 남음).
"""
import threading
import time

from . import config
from .claude import ask_claude
from .codex import EngineStop, LimitError, ask_codex

ENGINES = {"codex": ("Codex", ask_codex), "claude": ("Claude", ask_claude)}
SKIP_SEC = 30 * 60  # 자동 모드: 한도에 걸린 엔진을 이만큼 (또는 풀리는 시각까지) 건너뛴다
CANCEL = threading.Event()
_spent = {}  # 엔진 → 이 시각까지 건너뜀


class Cancelled(EngineStop):
    kind = "cancel"


def order():
    e = config.settings()["engine"]
    return [e] if e in ENGINES else list(ENGINES)


def label():
    now = time.time()
    names = [ENGINES[k][0] for k in order() if _spent.get(k, 0) <= now] or [ENGINES[k][0] for k in order()]
    return names[0]


def reset():
    """한도가 풀렸을 거라 보고 처음 순서부터 다시."""
    _spent.clear()


def ask(prompt, schema):
    if CANCEL.is_set():
        raise Cancelled("멈춤을 눌러 멈췄습니다. 끝난 구간은 기억해 두므로 다시 누르면 이어서 합니다.")
    now, ks = time.time(), order()
    live = [k for k in ks if _spent.get(k, 0) <= now] or ks  # 다 막혔어도 사람이 다시 눌렀으면 한 번 더
    last = None
    for k in live:
        try:
            return ENGINES[k][1](prompt, schema)
        except LimitError as e:
            nxt = [ENGINES[x][0] for x in ks if x != k and _spent.get(x, 0) <= time.time()]
            if nxt and _spent.get(k, 0) <= time.time():
                print(f"  [엔진] {ENGINES[k][0]} 사용 한도 → {nxt[0]}(으)로 넘어갑니다")
            _spent[k] = max(time.time() + SKIP_SEC, e.until or 0)
            last = e
    if len(ks) > 1:
        until = min((u for u in (_spent.get(k) for k in ks) if u), default=None)
        raise LimitError("Codex와 Claude 둘 다 사용 한도에 걸렸습니다. 한도가 풀린 뒤 다시 누르세요. "
                         "이미 끝난 구간은 기억해 두므로 거기서부터 이어집니다.", until)
    raise last
