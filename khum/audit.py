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


def default_adopt(p):
    """처음 보여 줄 때의 기본 체크: 바뀌었고, 50% 이하로 바뀌었고, 숫자·인용이 다 남은 문단만 채택.
    챗봇 잔재 문단 삭제는 채택이 기본."""
    if p.get("deleted"):
        return True
    return p["text"] != p["orig"] and p["rate"] <= STOP and not p["missing"]


def final_paras(rw, decisions):
    """[(문단번호, 최종 문장, 채택여부)]  decisions = {'3': {'adopt': bool, 'text': 직접 고친 문장|None}}"""
    out = []
    for p in rw["paras"]:
        d = decisions.get(str(p["i"]), {})
        adopt = d.get("adopt", default_adopt(p))
        text = d.get("text") or p["text"]
        out.append((p["i"], text if adopt else p["orig"], adopt))
    return out


def summary(paras, detect, rw=None, decisions=None, target=None):
    """화면 · 진단표 위쪽 요약. rw 가 없으면(진단만) 원문 기준."""
    sev = lambda items: {s: sum(x["severity"] == s for x in items) for s in ("S1", "S2", "S3")}
    orig_text = "\n\n".join(paras)
    out = {"found": sev(detect["items"]), "orig_chars": chars(orig_text), "n_paras": len(paras)}
    if not rw:
        return out
    fin = final_paras(rw, decisions or {})
    by = {p["i"]: p for p in rw["paras"]}
    residual = []
    for i, _, adopt in fin:  # 채택한 문단은 윤문본 감사 결과, 원문 유지 문단은 원래 탐지가 남은 흔적
        residual += by[i].get("residual", []) if adopt else [x for x in detect["items"] if x["para"] == i]
    final_text = "\n\n".join(t for _, t, _ in fin if t)
    r = rate(orig_text, final_text)
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
    if target:
        n = fc["no_space" if target.get("no_space") else "with_space"]
        tol = target.get("tol", 0.05)
        out["target"] = {**target, "actual": n, "ok": abs(n - target["chars"]) <= target["chars"] * tol}
    return out
