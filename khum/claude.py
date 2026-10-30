"""Claude CLI(claude.ai OAuth 로그인) 호출 — Codex와 같은 자리에 끼워 쓰는 두 번째 엔진.

API 키 없이, 이 PC에 `claude auth login` 된 Claude 계정(Pro·Max·Team)으로 `claude -p` 를 부르고
`--json-schema` 로 JSON만 받는다. 도구·설정·세션 기록은 모두 끈다 (프롬프트만으로 답하게).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .codex import EngineStop, limit_error

MODEL = os.environ.get("KHUM_CLAUDE_MODEL", "sonnet")
SYSTEM = "너는 한국어 원고 편집자다. 사용자 프롬프트의 지시만 따르고, 정해진 JSON 형식으로만 답한다."


def claude_bin():
    """npm 의 claude.cmd 는 cmd.exe 를 거쳐 JSON 인자가 깨진다 — 옆의 claude.exe 를 바로 부른다."""
    found = shutil.which("claude")
    cands = [shutil.which("claude.exe"), Path.home() / ".local" / "bin" / "claude.exe"]
    if found:
        cands.insert(0, Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
        if os.name != "nt":
            cands.insert(0, found)
    for p in cands:
        if p and Path(p).is_file():
            return str(p)
    raise SystemExit("claude CLI를 찾을 수 없습니다. 명령창에서 npm install -g @anthropic-ai/claude-code 를 실행하세요.")


def env():
    """API 키가 잡혀 있으면 OAuth 대신 그 키로 과금된다 — 비우고 부른다. 이 콘솔이 Claude Code 안에서 떠도 섞이지 않게."""
    e = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))}
    e.update(ANTHROPIC_API_KEY="", ANTHROPIC_AUTH_TOKEN="", ANTHROPIC_BASE_URL="")
    return e


class ClaudeAuthError(EngineStop):
    """이 PC의 Claude 로그인이 만료·해제됨."""
    kind = "claude_auth"


AUTH_RE = re.compile(r"not logged in|please run /login|/login|invalid api key|oauth token (?:has )?expired|"
                     r"authentication_error|\(401\)|unauthorized", re.I)
LIMIT_RE = re.compile(r"usage limit|limit reached|hit your limit|hour limit|weekly limit|rate.?limit|"
                      r"too many requests|overloaded|\b429\b|\b529\b", re.I)
AUTH_MSG = ("이 PC의 Claude 로그인이 만료됐습니다. 콘솔 왼쪽 아래 Claude 메뉴의 [로그인]"
            "(또는 명령창에서 claude auth login)으로 다시 로그인한 뒤 다시 누르세요.")


def reset_when(text):
    """「limit reached|1759380000」 · 「resets 3pm」 → 읽을 수 있는 시각 (없으면 None)."""
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        return datetime.fromtimestamp(int(m.group(1))).strftime("%H:%M")
    m = re.search(r"resets?\s*(?:at\s*)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", text, re.I)
    return m.group(1).strip() if m else None


def ask_claude(prompt, schema, model=None, timeout=900):
    with tempfile.TemporaryDirectory() as td:
        cmd = [claude_bin(), "-p", "--output-format", "json", "--json-schema", json.dumps(schema, ensure_ascii=False),
               "--model", model or MODEL, "--tools", "", "--no-session-persistence", "--setting-sources", "",
               "--system-prompt", SYSTEM]
        r = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout, cwd=td, env=env())
    out, err = r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    try:
        res = json.loads(out)
    except ValueError:
        res = {"is_error": True, "result": (out + "\n" + err).strip()}
    if not res.get("is_error") and isinstance(res.get("structured_output"), dict):
        return res["structured_output"]
    msg = str(res.get("result") or err or out)[-600:]
    if LIMIT_RE.search(msg):
        raise limit_error("Claude", reset_when(msg), "Codex")
    if AUTH_RE.search(msg):
        raise ClaudeAuthError(AUTH_MSG + f"  (원문: {msg[-200:]})")
    raise RuntimeError(f"claude 실패 (exit {r.returncode}): {msg}")
