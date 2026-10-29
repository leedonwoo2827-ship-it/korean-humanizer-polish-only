"""Codex CLI(ChatGPT OAuth 로그인) 호출 — TS 속독 에이전트(tse/judge_codex.py)와 같은 방식.

API 키 없이, 이 PC에 `codex login` 된 ChatGPT 계정으로 `codex exec` 를 부르고
`--output-schema` 로 JSON만 받는다.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def codex_bin():
    exe = shutil.which("codex") or shutil.which("codex.cmd")
    if not exe:
        raise SystemExit("codex CLI를 찾을 수 없습니다. setup.bat 을 먼저 실행하세요.")
    return exe


class CodexAuthError(RuntimeError):
    """이 PC의 Codex(ChatGPT) 로그인이 만료·해제됨 — 다시 로그인해야 한다."""
    kind = "auth"


AUTH_RE = re.compile(r"Failed to refresh token|log out and sign in|unauthorized \(401\)|\(401\)|HTTP 401|not logged in"
                     r"|Please (?:re-?)?login|codex login", re.I)
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
            raise RuntimeError(f"codex 실패 (exit {r.returncode}): {_tidy(err)}")
        return json.loads(out.read_text(encoding="utf-8"))
