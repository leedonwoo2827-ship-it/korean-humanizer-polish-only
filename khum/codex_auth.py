"""Codex(ChatGPT OAuth) 로그인 상태 · 로그인 · 로그아웃 — 콘솔 좌하단 메뉴용.

`codex login status` 는 토큰이 서버에서 폐기돼도 "Logged in" 이라고 답하는 경우가 있다.
그래서 [새로고침]은 상태 확인 뒤 아주 짧은 실제 호출(ask_codex)까지 해 봐야 확실하다.
"""
import base64
import json
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from .codex import CodexAuthError, ask_codex, codex_bin

AUTH_PATH = Path.home() / ".codex" / "auth.json"
PING_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok"],
               "properties": {"ok": {"type": "boolean"}}}

_lock = threading.Lock()
STATE = {"installed": None, "logged_in": None, "email": None, "verified": None,
         "message": "확인 전", "checking": False, "checked_at": None, "login_open": False}


def _now():
    return datetime.now().strftime("%H:%M:%S")


def _claims():
    """~/.codex/auth.json 의 id_token(JWT) 내용."""
    try:
        obj = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        tok = (obj.get("tokens") or {}).get("id_token") or obj.get("id_token") or ""
        if isinstance(tok, dict):  # 버전에 따라 이미 풀어 둔 형태
            return tok
        part = tok.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def _email():
    return _claims().get("email")


def _plan():
    """ChatGPT 요금제 (plus · pro · team …)."""
    c = _claims()
    return (c.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type") or c.get("chatgpt_plan_type")


def _run(*args, timeout=30):
    r = subprocess.run([codex_bin(), *args], capture_output=True, timeout=timeout)
    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    return r.returncode, out


def state():
    return dict(STATE)


def check(deep=True):
    """상태 확인. deep=True 면 실제로 한 번 짧게 불러 본다 (10~30초)."""
    with _lock:
        if STATE["checking"]:
            return
        STATE["checking"] = True
    try:
        try:
            codex_bin()
            STATE["installed"] = True
        except SystemExit:
            STATE.update(installed=False, logged_in=False, verified=False, email=None,
                         message="codex CLI가 없습니다. setup.bat 을 먼저 실행하세요.")
            return
        try:
            rc, out = _run("login", "status")
        except Exception as e:
            rc, out = 1, str(e)
        logged = rc == 0 and "not logged" not in out.lower()
        STATE.update(logged_in=logged, email=_email() if logged else None, plan=_plan() if logged else None)
        if not logged:
            STATE.update(verified=False, message="로그인되어 있지 않습니다. [로그인]을 누르세요.")
            return
        if not deep:
            STATE.update(verified=None, message="로그인 기록은 있습니다. [새로고침]으로 실제 동작을 확인하세요.")
            return
        STATE["message"] = "실제로 불러 보는 중…"
        try:
            ask_codex('{"ok": true} 만 그대로 답하라. 파일을 읽거나 명령을 실행하지 마라.',
                      timeout=120, schema=PING_SCHEMA)
            STATE.update(verified=True, message="정상 — 윤문에 쓸 수 있습니다.")
        except CodexAuthError:
            STATE.update(verified=False, message="로그인이 만료됐습니다 (토큰 폐기). [로그인]을 누르세요 (기존 로그인을 해제하고 다시 합니다).")
        except Exception as e:
            STATE.update(verified=False, message=f"호출 실패: {str(e)[:200]}")
    finally:
        STATE.update(checking=False, checked_at=_now())


def check_async(deep=True):
    threading.Thread(target=check, args=(deep,), daemon=True).start()


def mark_expired():
    """판정 중 로그인 만료가 나면 메뉴 상태에도 바로 반영."""
    STATE.update(verified=False, checked_at=_now(),
                 message="로그인이 만료됐습니다 (토큰 폐기). [로그인]을 누르세요 (기존 로그인을 해제하고 다시 합니다).")


def logout():
    rc, out = _run("logout")
    STATE.update(logged_in=False, verified=False, email=None, checked_at=_now(),
                 message="로그아웃했습니다. [로그인]을 누르세요.")
    return out


def login(relogin=False):
    """새 명령창에서 codex login (브라우저가 열림). 끝나면 자동으로 새로고침."""
    if STATE["login_open"]:
        raise ValueError("로그인 창이 이미 열려 있습니다. 브라우저에서 로그인을 마쳐 주세요.")
    if relogin:
        try:
            _run("logout")
        except Exception:
            pass
    flags = 0x00000010 if sys.platform.startswith("win") else 0  # CREATE_NEW_CONSOLE
    proc = subprocess.Popen([codex_bin(), "login"], creationflags=flags)
    STATE.update(login_open=True, verified=None, message="브라우저에서 ChatGPT 계정으로 로그인하세요…")

    def wait():
        try:
            proc.wait(timeout=900)
        except Exception:
            pass
        STATE["login_open"] = False
        check(deep=True)

    threading.Thread(target=wait, daemon=True).start()
