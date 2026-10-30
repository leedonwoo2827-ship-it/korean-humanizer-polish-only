"""Codex CLI(ChatGPT OAuth 로그인) 호출 — TS 속독 에이전트(tse/judge_codex.py)와 같은 방식.

API 키 없이, 이 PC에 `codex login` 된 ChatGPT 계정으로 `codex exec` 를 부르고
`--output-schema` 로 JSON만 받는다.
"""
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path


def codex_bin():
    exe = shutil.which("codex") or shutil.which("codex.cmd")
    if not exe:
        raise SystemExit("codex CLI를 찾을 수 없습니다. setup.bat 을 먼저 실행하세요.")
    return exe


class EngineStop(RuntimeError):
    """더 불러 봐야 다 실패한다 (로그인 만료 · 사용 한도) — 남은 구간도 바로 멈춘다."""
    kind = None


class CodexAuthError(EngineStop):
    """이 PC의 Codex(ChatGPT) 로그인이 만료·해제됨 — 다시 로그인해야 한다."""
    kind = "auth"


class LimitError(EngineStop):
    """ChatGPT · Claude 구독의 사용 한도 — 풀릴 때까지 기다리거나 엔진을 바꾼다. until = 풀리는 시각(epoch, 모르면 None)."""
    kind = "limit"

    def __init__(self, msg, until=None):
        super().__init__(msg)
        self.until = until


def clock(s):
    """'3:24 PM' · '15:30' · '3pm' → 다음에 오는 그 시각(epoch). 못 읽으면 None."""
    m = re.match(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s or "", re.I)
    if not m:
        return None
    h, mi, ap = int(m[1]), int(m[2] or 0), (m[3] or "").lower()
    h = h + 12 if ap == "pm" and h < 12 else 0 if ap == "am" and h == 12 else h
    if h > 23 or mi > 59:
        return None
    now = datetime.now()
    t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    return (t if t > now else t + timedelta(days=1)).timestamp()


def limit_error(name, when, other):
    return LimitError(limit_msg(name, when, other), clock(when))


def limit_msg(name, when, other):
    return (f"{name} 사용 한도에 걸렸습니다{f' ({when}에 풀림)' if when else ''}. 한도가 풀린 뒤 다시 누르거나, "
            f"왼쪽 아래에서 엔진을 {other}(으)로 바꿔 이어서 하세요. 이미 끝난 구간은 기억해 두므로 거기서부터 이어집니다.")


AUTH_RE = re.compile(r"Failed to refresh token|log out and sign in|unauthorized \(401\)|\(401\)|HTTP 401|not logged in"
                     r"|Please (?:re-?)?login|codex login", re.I)
LIMIT_RE = re.compile(r"usage limit|rate limit|quota|\(429\)|HTTP 429|too many requests", re.I)
AUTH_MSG = ("이 PC의 Codex 로그인이 만료됐습니다. 콘솔 왼쪽 아래 Codex 메뉴의 [로그인]"
            "(또는 codex-다시로그인.bat)으로 다시 로그인한 뒤 다시 누르세요.")


def _tidy(err, keep=6):
    """codex stderr: 시각·중복 줄을 걷어 내고 마지막 몇 줄만."""
    out = []
    for l in err.splitlines():
        l = re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s*", "", l).strip()
        if l and re.search(r"[A-Za-z가-힣]", l) and l not in out:
            out.append(l)
    return " / ".join(out[-keep:])[-600:]


def ask_codex(prompt, schema, model=None, timeout=900):
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "schema.json").write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        cmd = [codex_bin(), "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
               "-C", str(td), "--output-schema", str(td / "schema.json"),
               "-o", str(td / "out.json"), "--color", "never", "-"]
        if model:
            cmd[2:2] = ["-m", model]
        r = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout)
        out = td / "out.json"
        if not out.exists():
            err = r.stderr.decode("utf-8", "replace")
            if AUTH_RE.search(err):
                raise CodexAuthError(AUTH_MSG + f"  (원문: {_tidy(err, 2)})")
            if LIMIT_RE.search(err):
                m = re.search(r"try again (?:at|in) ([^.\n]+)", err, re.I)
                raise limit_error("Codex(ChatGPT)", m and m.group(1).strip(), "Claude")
            raise RuntimeError(f"codex 실패 (exit {r.returncode}): {_tidy(err)}")
        return json.loads(out.read_text(encoding="utf-8"))
