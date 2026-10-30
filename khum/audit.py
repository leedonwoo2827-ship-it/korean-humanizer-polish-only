"""감사 · 등급 — LLM 없이 세는 것들.

- 변경률: 문단별 1 − 글자 일치율 (difflib). 30% 넘으면 경고, 50% 넘으면 그 문단은 원문 유지가 기본.
- 보존 검사: 숫자 · 따옴표 안 인용 · 영문 약어가 윤문본에서 빠졌는지.
- 글자수: 공백 포함 / 제외, 목표 글자수 ±5%.
- 등급 A–D (k-skill korean-humanizer 기준).
"""
import html
import re
from difflib import SequenceMatcher

WARN, STOP = 0.30, 0.50

KEEP_RX = re.compile(r"\d[\d,.]*\d%?|\d%?|[“\"][^”\"]{2,80}[”\"]|「[^」]{1,80}」|\b[A-Z][A-Za-z0-9]*[A-Z0-9][A-Za-z0-9]*\b")


def rate(a, b):
    if a == b:
        return 0.0
    return round(1 - SequenceMatcher(None, a, b, autojunk=False).ratio(), 3)


def missing(orig, new):
    """원문의 숫자 · 인용 · 영문 약어 중 윤문본에 없는 것."""
    squash = re.sub(r"\s+", "", new)
    return sorted({m.group(0) for m in KEEP_RX.finditer(orig) if re.sub(r"\s+", "", m.group(0)) not in squash})


def chars(text):
    return {"with_space": len(text), "no_space": len(re.sub(r"\s", "", text))}


def diff_html(a, b):
    """원문 → 윤문: 빠진 글자 <del>, 들어간 글자 <ins>."""
    out = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(html.escape(a[i1:i2]))
        if op in ("delete", "replace"):
            out.append(f"<del>{html.escape(a[i1:i2])}</del>")
        if op in ("insert", "replace"):
            out.append(f"<ins>{html.escape(b[j1:j2])}</ins>")
    return "".join(out)


def diff_spans(a, b):
    """정오표용: 기존 → 수정을 글자 단위로 나눠 [(글자, 바뀜?)] 두 줄로. 바뀐 글자만 색칠하려고."""
    xa, xb = [], []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        xa.append((a[i1:i2], op != "equal"))
        xb.append((b[j1:j2], op != "equal"))
    return [x for x in xa if x[0]], [x for x in xb if x[0]]


def mark_pair(a, b):
    """기존 문구는 바뀌는 글자만 <b class=x>, 수정 문구는 새로 든 글자만 <b class=y>. 나머지는 그대로(검정)."""
    xa, xb = diff_spans(a, b)
    tag = lambda xs, c: "".join(f"<b class={c}>{html.escape(t)}</b>" if ch else html.escape(t) for t, ch in xs)
    return tag(xa, "x"), tag(xb, "y")


def default_adopt(p):
    """처음 보여 줄 때의 기본 체크: 바뀌었고, 50% 이하로 바뀌었고, 숫자·인용이 다 남은 문단만 채택.
    챗봇 잔재 문단 삭제는 채택이 기본."""
    if p.get("deleted"):
        return True
    return p["text"] != p["orig"] and p["rate"] <= STOP and not p["missing"]


# ---------- 수정 항목(정오표) — 윤문 결과가 rw["edits"] 로 오는 방식 ----------
EDIT_ON = 0.6  # 수정확률 이 이상이면 처음부터 체크 (jev 와 같음)


def edit_default(e):
    """처음 체크: 수정확률 60% 이상이고, 원문에서 위치를 찾았고, 숫자·인용을 빠뜨리지 않은 것."""
    return e["p"] >= EDIT_ON and e.get("located", True) and not e.get("missing")


def edit_on(e, decisions):
    d = decisions.get("e:" + e["id"], {})
    return d.get("apply", edit_default(e)), d.get("new_text") or e["new_text"], d.get("note", "")


def apply_edits(orig, edits):
    """원문 문단에 수정 항목들을 적용 (old_text 위치 순서, 겹치면 앞의 것만) → (새 문단, 적용한 id 목록)."""
    spans, at = [], 0
    for e in sorted(edits, key=lambda e: orig.find(e["old_text"])):
        k = orig.find(e["old_text"])
        if k < 0 or k < at or not e["old_text"]:
            continue
        spans.append((k, k + len(e["old_text"]), e["new_text"], e["id"]))
        at = k + len(e["old_text"])
    out, pos = [], 0
    for s, t, new, _ in spans:
        out += [orig[pos:s], new]
        pos = t
    return "".join(out) + orig[pos:], [x[3] for x in spans]


def final_paras(rw, decisions):
    """[(문단번호, 최종 문장, 채택여부)]
    수정 항목 방식(rw['edits']): 체크한 항목만 원문에 적용. decisions = {'e:<id>': {'apply', 'new_text', 'note'}}
    예전 문단 방식: decisions = {'3': {'adopt': bool, 'text': 직접 고친 문장|None}}"""
    if "edits" in rw:
        by = {}
        for e in rw["edits"]:
            on, new, _ = edit_on(e, decisions)
            if on and e.get("located", True):
                by.setdefault(e["para"], []).append(dict(e, new_text=new))
        out = []
        for p in rw["paras"]:
            text, used = apply_edits(p["orig"], by.get(p["i"], []))
            out.append((p["i"], text.strip() if used else p["orig"], bool(used)))
        return out
    out = []
    for p in rw["paras"]:
        d = decisions.get(str(p["i"]), {})
        adopt = d.get("adopt", default_adopt(p))
        text = d.get("text") or p["text"]
        out.append((p["i"], text if adopt else p["orig"], adopt))
    return out


def doc_rate(fin, by, orig_text, final_text):
    """글 전체 변경률. 짧으면 전체를 한 번에 비교하고, 길면(difflib 가 제곱으로 느려져 26만 자면 멈춘 듯 걸린다)
    문단별 변경률을 원문 길이로 가중 평균한다."""
    if len(orig_text) <= 20000:
        return rate(orig_text, final_text)
    tot = sum(len(by[i]["orig"]) for i, _, _ in fin) or 1
    part = lambda i, t, a: 0 if not a else by[i]["rate"] if t == by[i]["text"] and "rate" in by[i] else rate(by[i]["orig"], t)
    return round(sum(len(by[i]["orig"]) * part(i, t, a) for i, t, a in fin) / tot, 3)


def summary(paras, detect, rw=None, decisions=None, target=None):
    """화면 · 진단표 위쪽 요약. rw 가 없으면(진단만) 원문 기준."""
    sev = lambda items: {s: sum(x["severity"] == s for x in items) for s in ("S1", "S2", "S3")}
    orig_text = "\n\n".join(paras)
    out = {"found": sev(detect["items"]), "orig_chars": chars(orig_text), "n_paras": len(paras)}
    if not rw:
        return out
    fin = final_paras(rw, decisions or {})
    by = {p["i"]: p for p in rw["paras"]}
    found = {}
    for x in detect["items"]:
        found.setdefault(x["para"], []).append(x)
    residual = []
    for i, _, adopt in fin:  # 채택한 문단은 윤문본 감사 결과, 원문 유지 문단은 원래 탐지가 남은 흔적
        residual += by[i].get("residual", []) if adopt else found.get(i, [])
    final_text = "\n\n".join(t for _, t, _ in fin if t)
    r = doc_rate(fin, by, orig_text, final_text)
    lost = sorted({m for i, t, a in fin if a and t for m in missing(by[i]["orig"], t)})
    checks = rw.get("checks", {})
    selfcheck = {
        "고유명사·수치·인용 보존": not lost,
        "변경률 30% 이하": r <= WARN,
        "장르 유지": checks.get("genre_kept", True),
        "문체(격식) 유지": checks.get("register_kept", True),
        "남은 S1 0건": not any(x["severity"] == "S1" for x in residual),
        "없던 수사·비유 안 더함": checks.get("no_new_flourish", True),
    }
    left = sev(residual)
    passed = sum(selfcheck.values())
    if left["S1"] >= 3 or r > STOP:
        grade, advice = "D", "작업을 멈추고 사람이 읽어 보세요 (남은 S1이 많거나 너무 많이 바뀌었습니다)."
    elif left["S1"] >= 1 or passed <= 4:
        grade, advice = "C", "한 번 더 윤문하거나, 남은 흔적을 직접 고쳐 보세요."
    elif left["S2"] <= 2 and r <= 0.25 and passed == 6:
        grade, advice = "A", "그대로 써도 됩니다."
    elif left["S2"] <= 4 and passed >= 5:
        grade, advice = "B", "대체로 자연스럽습니다. 남은 S2만 훑어보세요."
    else:
        grade, advice = "C", "한 번 더 윤문하거나, 남은 흔적을 직접 고쳐 보세요."
    fc = chars(final_text)
    out.update(residual=left, rate=r, lost=lost, selfcheck=selfcheck, grade=grade, advice=advice,
               final_chars=fc, adopted=sum(a for _, _, a in fin), changed=sum(p["text"] != p["orig"] for p in rw["paras"]))
    if "edits" in rw:
        dec = decisions or {}
        out.update(n_edits=len(rw["edits"]), checked=sum(edit_on(e, dec)[0] for e in rw["edits"]))
    if target:
        n = fc["no_space" if target.get("no_space") else "with_space"]
        tol = target.get("tol", 0.05)
        out["target"] = {**target, "actual": n, "ok": abs(n - target["chars"]) <= target["chars"] * tol}
    return out
