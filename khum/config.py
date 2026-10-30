"""워크스페이스 구조. 글 하나 = 폴더 하나.

workspace/
  01_<제목>/
    doc.json          글 설정 (제목 · 장르 · 모드 · 목표 글자수 · 문체 샘플) + 진행 상태
    01_원문/          올린 파일 그대로 + paragraphs.json (문단 나눔)
    02_탐지/          detect.json (AI 흔적 · 분류 · 심각도), cache/
    03_윤문/          rewrite.json (문단별 윤문 · 감사), cache/
    04_검토/          decisions.json (문단별 채택/원문 유지 · 직접 고친 문장)
    05_반영본/        <제목>_윤문본.docx · .txt · <제목>_진단표.xlsx
  _settings.json      콘솔 설정 (엔진 · 한도 대기 시간)
backup/               [보관]한 글 (지우지 않음)
"""
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WS = Path(os.environ.get("KHUM_WORKSPACE", ROOT / "workspace"))
BACKUP = WS.parent / "backup"
STAGES = ["01_원문", "02_탐지", "03_윤문", "04_검토", "05_반영본"]

SETTINGS = WS / "_settings.json"
ENGINES = {"auto": "자동 (Codex 먼저, 한도면 Claude)", "codex": "Codex (ChatGPT)", "claude": "Claude"}
DEFAULTS = {"engine": "auto", "wait_hours": 8}

GENRES = ["단행본 원고", "논문·보고서", "칼럼·에세이", "보도자료·공문", "블로그·후기", "기타"]


def _read(p, default=None):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def settings():
    return {**DEFAULTS, **_read(SETTINGS, {})}


def save_settings(body):
    s = settings()
    if body.get("engine") in ENGINES:
        s["engine"] = body["engine"]
    if "wait_hours" in body:
        s["wait_hours"] = max(0, min(48, float(body["wait_hours"] or 0)))
    _write(SETTINGS, s)
    return s


def slug(title):
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", title.strip())
    return s.strip("_.")[:36] or "글"


def docs():
    if not WS.exists():
        return []
    out = [d for d in (_read(p / "doc.json") for p in WS.iterdir() if p.is_dir()) if d]
    return sorted(out, key=lambda d: d["id"])


def doc_dir(did):
    if WS.exists():
        for p in WS.iterdir():
            d = _read(p / "doc.json") if p.is_dir() else None
            if d and d["id"] == did:
                return p
    raise SystemExit(f"글이 없습니다: {did}")


def doc(did):
    return _read(doc_dir(did) / "doc.json")


def save_doc(d):
    _write(doc_dir(d["id"]) / "doc.json", d)
    return d


def new_doc(title, **opts):
    """다음 번호로 글 폴더를 만든다. 번호는 '01', '02' …"""
    WS.mkdir(parents=True, exist_ok=True)
    used = [int(d["id"]) for d in docs() if d["id"].isdigit()]
    used += [int(m.group(1)) for m in (re.match(r"\d{8}-\d{6}_(\d+)_", p.name) for p in BACKUP.glob("*")) if m] \
        if BACKUP.exists() else []
    did = f"{max(used, default=0) + 1:02d}"
    p = WS / f"{did}_{slug(title)}"
    for s in STAGES:
        (p / s).mkdir(parents=True, exist_ok=True)
    d = {"id": did, "title": title, "created": datetime.now().strftime("%Y-%m-%d %H:%M"), **opts}
    _write(p / "doc.json", d)
    return d


def stage(did, name):
    p = doc_dir(did) / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def load(did, name, file):
    return _read(doc_dir(did) / name / file)


def save(did, name, file, data):
    _write(stage(did, name) / file, data)


def archive(did):
    """글 폴더를 backup/<시각>_<폴더이름>/ 으로 옮긴다 (지우지 않음)."""
    src = doc_dir(did)
    dest = BACKUP / f"{datetime.now():%Y%m%d-%H%M%S}_{src.name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return dest


def backups():
    if not BACKUP.exists():
        return []
    out = []
    for p in sorted(BACKUP.iterdir(), reverse=True):
        d = _read(p / "doc.json") if p.is_dir() else None
        if d:
            out.append({"name": p.name, "id": d["id"], "title": d["title"]})
    return out


def restore(name):
    src = BACKUP / name
    d = _read(src / "doc.json")
    if not d or src.parent != BACKUP:
        raise ValueError(f"보관한 글이 없습니다: {name}")
    if any(x["id"] == d["id"] for x in docs()):
        raise ValueError(f"같은 번호({d['id']})의 글이 이미 있습니다. 그 글을 먼저 보관하세요.")
    dest = WS / re.sub(r"^\d{8}-\d{6}_", "", src.name)
    shutil.move(str(src), str(dest))
    return d
