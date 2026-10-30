"""대시보드 단계 버튼 — 고른 글 · 절에 「1 진단」「2 윤문」「3 감사」「4 반영」 또는 ▶1→3 · ▶1→4.

단계를 나눠 두는 건 Codex 사용 한도를 다른 일과 나눠 쓰며 시간대를 갈라 돌리기 위해서다.
  - 한 단계 버튼은 그 단계만 한다. 앞 단계가 안 된 부분은 건너뛰고 로그에 남긴다 (감사를 눌렀는데 윤문 호출이 생기지 않게).
  - ▶ 묶음 버튼은 앞 단계부터 차례로. 이미 끝난 부분은 건너뛴다 (끝난 구간은 캐시라 호출하지 않음).
  - 한 글이 실패해도 다음 글로 넘어간다.
  - 사용 한도: 자동 엔진이면 llm.py 가 다른 엔진으로 넘긴다. 다 막히면 풀릴 때까지 기다렸다가
    같은 글부터 이어서 한다 (설정 wait_hours, 0 = 바로 멈춤). 기다리는 동안 PC 절전을 막는다.
  - 로그인 만료 · [멈춤] 이면 거기서 멈춘다.
"""
import os
import time
from datetime import datetime

from . import config, export, humanize, llm
from .codex import EngineStop, LimitError

POLL_MIN = 30          # 풀리는 시각을 모를 때 다시 해 보는 간격
CHUNK_MIN = 60         # 시각을 알아도 길게는 이만큼씩만 자고 확인
AFTER_RESET_SEC = 90

STEPS = {"detect": "진단", "rewrite": "윤문", "audit": "감사", "export": "반영"}
STATE = {"ids": [], "cur": None, "parts": {}, "steps": [], "done": {}, "waiting": None}


def state():
    return dict(STATE, done=dict(STATE["done"]), parts=dict(STATE["parts"]), steps=list(STATE["steps"]))


def keep_awake(on=True):
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))
    except Exception:
        pass


def detected(det):
    """진단이 끝난 문단 번호."""
    return {i for c in (det or {}).get("chunks", []) if not (c.get("failed") or c.get("pending")) for i in c["ids"]}


def left(did, only=None, det=None, rw=None, paras=None):
    """단계마다 아직 안 된 문단 번호 — {'detect': {…}, 'rewrite': {…}, 'audit': {…}}."""
    det = det or config.load(did, "02_탐지", "detect.json")
    rw = rw or config.load(did, "03_윤문", "rewrite.json")
    paras = paras or config.load(did, "01_원문", "paragraphs.json") or []
    want = only if only is not None else set(range(1, len(paras) + 1))
    return {"detect": want - detected(det), "rewrite": want - humanize.done_paras(rw),
            "audit": want - humanize.done_paras(rw, "audit")}


def todo(did, only=None, det=None, rw=None, paras=None):
    """이 글(only = 고른 문단 번호만)에 남은 단계 — ['detect', 'rewrite', 'audit'] 중."""
    return [k for k, v in left(did, only, det, rw, paras).items() if v]


def part_status(did):
    """대시보드의 절 행: [{k, title, start, end, chars, todo, left: {단계: 남은 글자수}, exported}]."""
    paras = config.load(did, "01_원문", "paragraphs.json") or []
    det, rw = config.load(did, "02_탐지", "detect.json"), config.load(did, "03_윤문", "rewrite.json")
    ex = config.doc(did).get("exported_parts", {})
    out = []
    for pt in humanize.parts(paras):
        lf = left(did, set(range(pt["start"], pt["end"] + 1)), det, rw, paras)
        out.append(dict(pt, todo=[k for k, v in lf.items() if v], exported=ex.get(str(pt["k"])),
                        left={k: sum(len(paras[i - 1]) for i in v) for k, v in lf.items()}))
    return out


def _step(did, step, only, ks):
    """한 단계. 앞 단계가 안 된 문단은 건너뛴다."""
    lf = left(did, only)
    if step == "detect":
        if not lf["detect"]:
            return print("  [진단] 이미 끝났습니다")
        return humanize.detect(did, lf["detect"])
    if step == "rewrite":
        go, skip = lf["rewrite"] - lf["detect"], lf["rewrite"] & lf["detect"]
        if skip:
            print(f"  [윤문] 진단이 안 된 문단 {len(skip)}개는 건너뜁니다 — 먼저 「1 진단」")
        if not go:
            return print("  [윤문] 할 것이 없습니다" if skip else "  [윤문] 이미 끝났습니다")
        return humanize.rewrite(did, do_audit=False, only=go)
    if step == "audit":
        go, skip = lf["audit"] - lf["rewrite"], lf["audit"] & lf["rewrite"]
        if skip:
            print(f"  [감사] 윤문이 안 된 문단 {len(skip)}개는 건너뜁니다 — 먼저 「2 윤문」")
        if not go:
            return print("  [감사] 할 것이 없습니다" if skip else "  [감사] 이미 끝났습니다")
        return humanize.audit_stage(did, go)
    if step == "export":
        if lf["rewrite"]:
            print(f"  [반영] 윤문이 안 된 문단 {len(lf['rewrite'])}개는 원문 그대로 들어갑니다")
        for k in ks if ks is not None else [None]:
            export.run(did, part=k)


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


def run(items, steps=("detect", "rewrite", "audit")):
    """items = [(글 번호, 고른 절 번호 목록 | None = 전체)], steps = 차례로 할 단계."""
    hours = config.settings()["wait_hours"]
    ids = [did for did, _ in items]
    STATE.update(ids=ids, cur=None, parts={did: ks for did, ks in items}, steps=list(steps), done={}, waiting=None)
    name = " → ".join(STEPS[s] for s in steps)
    print(f"[{name}] {len(ids)}편 — 엔진 {config.ENGINES[config.settings()['engine']]} · 한도면 최대 {hours:g}시간 기다림")
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
                print(f"[{n}/{len(ids)}] {did} {title} — {name}")
                try:
                    for st in steps:
                        _step(did, st, only, ks)
                    lf = left(did, only)
                    STATE["done"][did] = "ok" if not any(lf[s] for s in steps if s in lf) else "part"
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
        print(f"[{name}] 끝 — {len(ids) - len(bad)}/{len(ids)}편 완료" + (f" · 다시 볼 글: {', '.join(bad)}" if bad else ""))
    finally:
        STATE.update(cur=None, waiting=None)
        keep_awake(False)
