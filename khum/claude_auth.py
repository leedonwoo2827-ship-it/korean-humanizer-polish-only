"""Claude(claude.ai OAuth) 로그인 상태 · 로그인 · 로그아웃 — 콘솔 좌하단 메뉴용 (codex_auth.py 와 같은 모양).

`claude auth status` 가 JSON(loggedIn · email · authMethod)으로 답한다. [새로고침]은 거기에 짧은 실제 호출까지.
"""
import json
import subprocess
import sys
import threading
from datetime import datetime

from .claude import ClaudeAuthError, ask_claude, claude_bin, env
from .codex import LimitError

PING_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok"],
               "properties": {"ok": {"type": "boolean"}}}

_lock = threading.Lock()
STATE = {"installed": None, "logged_in": None, "email": None, "verified": None,
         "message": "확인 전", "checking": False, "checked_at": None, "login_open": False}


def _now():
    return datetime.now().strftime("%H:%M:%S")


def _run(*args, timeout=30):
    r = subprocess.run([claude_bin(), *args], capture_output=True, timeout=timeout, env=env())
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace").strip()


def state():
    return dict(STATE)


def check(deep=True):
    with _lock:
        if STATE["checking"]:
            return
        STATE["checking"] = True
    try:
        try:
            claude_bin()
            STATE["installed"] = True
        except SystemExit as e:
            STATE.update(installed=False, logged_in=False, verified=False, email=None, message=str(e))
            return
        try:
            _, out = _run("auth", "status")
            st = json.loads(out[out.find("{"):])
        except Exception:
            st = {}
        logged = bool(st.get("loggedIn")) and st.get("authMethod") != "api_key"
        STATE.update(logged_in=logged, email=st.get("email") if logged else None, plan=st.get("subscriptionType") if logged else None)
        if not logged:
            STATE.update(verified=False, message="Claude 계정(claude.ai)으로 로그인되어 있지 않습니다. [로그인]을 누르세요.")
            return
        if not deep:
            STATE.update(verified=None, message="로그인 기록은 있습니다. [새로고침]으로 실제 동작을 확인하세요.")
            return
        STATE["message"] = "실제로 불러 보는 중…"
        try:
            ask_claude('{"ok": true} 만 그대로 답하라.', PING_SCHEMA, timeout=120)
            STATE.update(verified=True, message="정상 — 윤문에 쓸 수 있습니다.")
        except ClaudeAuthError:
            STATE.update(verified=False, message="로그인이 만료됐습니다. [로그인]을 누르세요.")
        except LimitError as e:
            STATE.update(verified=True, message=f"로그인은 정상이지만 지금은 {e}")
        except Exception as e:
            STATE.update(verified=False, message=f"호출 실패: {str(e)[:200]}")
    finally:
        STATE.update(checking=False, checked_at=_now())


def check_async(deep=True):
    threading.Thread(target=check, args=(deep,), daemon=True).start()


def mark_expired():
    STATE.update(verified=False, checked_at=_now(), message="로그인이 만료됐습니다. [로그인]을 누르세요.")


def logout():
    _, out = _run("auth", "logout")
    STATE.update(logged_in=False, verified=False, email=None, checked_at=_now(),
                 message="로그아웃했습니다. [로그인]을 누르세요.")
    return out


def login():
    """새 명령창에서 claude auth login (브라우저가 열림). 끝나면 자동으로 새로고침."""
    if STATE["login_open"]:
        raise ValueError("로그인 창이 이미 열려 있습니다. 브라우저에서 로그인을 마쳐 주세요.")
    flags = 0x00000010 if sys.platform.startswith("win") else 0  # CREATE_NEW_CONSOLE
    proc = subprocess.Popen([claude_bin(), "auth", "login", "--claudeai"], creationflags=flags, env=env())
    STATE.update(login_open=True, verified=None, message="브라우저에서 Claude 계정으로 로그인하세요…")

    def wait():
        try:
            proc.wait(timeout=900)
        except Exception:
            pass
        STATE["login_open"] = False
        check(deep=True)

    threading.Thread(target=wait, daemon=True).start()
