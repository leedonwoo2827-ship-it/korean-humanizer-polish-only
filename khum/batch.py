"""대시보드 [체크한 글 이어서 진행] — 글(또는 고른 절)마다 진단 → 윤문(→ 감사)을 끝까지. 퇴근 뒤 무인용.

  - 이미 끝난 단계는 건너뛰고, 실패 구간이 남은 단계만 다시 한다 (끝난 구간은 캐시라 호출하지 않음).
  - 한 글이 실패해도 다음 글로 넘어간다.
  - 사용 한도: 자동 엔진이면 llm.py 가 다른 엔진으로 넘긴다. 둘 다 막히면 풀릴 때까지 기다렸다가
    같은 글부터 이어서 한다 (설정 wait_hours, 0 = 바로 멈춤). 기다리는 동안 PC 절전을 막는다.
  - 로그인 만료 · [멈춤] 이면 거기서 멈춘다.
"""
import os
import time
from datetime import datetime

from . import config, humanize, llm
from .codex import EngineStop, LimitError

POLL_MIN = 30          # 풀리는 시각을 모를 때 다시 해 보는 간격
CHUNK_MIN = 60         # 시각을 알아도 길게는 이만큼씩만 자고 확인
AFTER_RESET_SEC = 90

STATE = {"ids": [], "cur": None, "parts": {}, "done": {}, "waiting": None}


def state():
    return dict(STATE, done=dict(STATE["done"]), parts=dict(STATE["parts"]))


def keep_awake(on=True):
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))
    except Exception:
        pass


def todo(did, only=None, det=None, rw=None, paras=None):
    """이 글(only = 고른 문단 번호만)에 남은 단계 — ['detect', 'rewrite'] 중."""
    det = det or config.load(did, "02_탐지", "detect.json")
    rw = rw or config.load(did, "03_윤문", "rewrite.json")
    paras = paras or config.load(did, "01_원문", "paragraphs.json") or []
    want = only if only is not None else set(range(1, len(paras) + 1))
    redo_det = not det or any((c.get("failed") or c.get("pending")) and want & set(c["ids"]) for c in det["chunks"])
    redo_rw = redo_det or not rw or bool(want - humanize.done_paras(rw))
    return (["detect"] if redo_det else []) + (["rewrite"] if redo_rw else [])


def part_status(did):
    """대시보드의 절 행: [{k, title, start, end, chars, todo}]."""
    paras = config.load(did, "01_원문", "paragraphs.json") or []
    det, rw = config.load(did, "02_탐지", "detect.json"), config.load(did, "03_윤문", "rewrite.json")
    return [dict(pt, todo=todo(did, set(range(pt["start"], pt["end"] + 1)), det, rw, paras)) for pt in humanize.parts(paras)]


def _wait(e, deadline):
    """한도가 풀릴 때까지 잔다. 풀렸을 것 같으면 True, 최대 시간을 넘기거나 [멈춤]이면 False."""
    now = time.time()
    if now >= deadline:
        return False
    wake = min(e.until + AFTER_RESET_SEC if e.until and e.until > now else now + POLL_MIN * 60,
               now + CHUNK_MIN * 60, deadline)
    STATE["waiting"] = datetime.fromtimestamp(wake).strftime("%H:%M")
    print(f"⏸ 사용 한도 — {STATE['waiting']}에 다시 해 봅니다. 콘솔 창(run.bat)은 켜 두세요. 기다리는 동안 PC 절전을 막습니다.")
    while time.time() < wake:
        if llm.CANCEL.wait(10):
            return False
    STATE["waiting"] = None
    llm.reset()
    print("▶ 다시 해 봅니다")
    return True


def run(items, do_audit=True):
    """items = [(글 번호, 고른 절 번호 목록 | None = 전체)]"""
    hours = config.settings()["wait_hours"]
    ids = [did for did, _ in items]
    STATE.update(ids=ids, cur=None, parts={did: ks for did, ks in items}, done={}, waiting=None)
    print(f"[이어서 진행] {len(ids)}편 — 엔진 {config.ENGINES[config.settings()['engine']]} · 한도면 최대 {hours:g}시간 기다림")
    keep_awake(True)
    try:
        for n, (did, ks) in enumerate(items, 1):
            STATE["cur"] = did
            title = config.doc(did)["title"]
            paras = config.load(did, "01_원문", "paragraphs.json") or []
            only = humanize.picked(paras, ks)
            if only == set():  # 글이 바뀌어 고른 절이 없어짐
                print(f"[{n}/{len(ids)}] {did} {title} — 고른 절이 없습니다. 대시보드를 새로고침하고 다시 고르세요")
                STATE["done"][did] = "fail"
                continue
            if ks is not None:
                pts = humanize.parts(paras)
                title += " — " + ", ".join(pts[k]["title"][:24] for k in ks if k < len(pts))
            deadline = time.time() + hours * 3600
            while True:
                steps = todo(did, only)
                if not steps:
                    print(f"[{n}/{len(ids)}] {did} {title} — 이미 끝났습니다")
                    STATE["done"][did] = "ok"
                    break
                print(f"[{n}/{len(ids)}] {did} {title} — " + " → ".join({"detect": "진단", "rewrite": "윤문"}[s] for s in steps))
                try:
                    if "detect" in steps:
                        humanize.detect(did, only)
                    humanize.rewrite(did, do_audit=do_audit, only=only)
                    STATE["done"][did] = "ok" if not todo(did, only) else "part"
                    break
                except LimitError as e:
                    print(f"  {e}")
                    if not _wait(e, deadline):
                        STATE["done"][did] = "limit"
                        raise
                except EngineStop:
                    STATE["done"][did] = "stop"
                    raise
                except Exception as e:
                    print(f"[건너뜀] {did} {title}: {e}")
                    STATE["done"][did] = "fail"
                    break
        bad = [k for k, v in STATE["done"].items() if v != "ok"]
        print(f"[이어서 진행] 끝 — {len(ids) - len(bad)}/{len(ids)}편 완료" + (f" · 다시 볼 글: {', '.join(bad)}" if bad else ""))
    finally:
        STATE.update(cur=None, waiting=None)
        keep_awake(False)
