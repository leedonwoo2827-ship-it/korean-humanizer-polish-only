"""AI 흔적 분류 체계(A–K) — references/ai-tell-taxonomy.md 를 읽어 프롬프트·화면에 쓴다.
더불어 LLM 없이도 확실히 잡히는 흔적(줄표 · 이모지 · 이중피동 · 연결어미 뒤 쉼표 · 챗봇 잔재 · 결산 상투구)은
정규식으로 먼저 찾는다 (rule_scan). 이것들은 Codex가 놓쳐도 빠지지 않는다.
"""
import re
from functools import lru_cache
from pathlib import Path

REF = Path(__file__).with_name("references")
SEVERITY = ["S1", "S2", "S3"]


@lru_cache
def table_text():
    """프롬프트에 넣을 패턴 표 (A. 번역투 ~ 챗봇 잔재)."""
    t = (REF / "ai-tell-taxonomy.md").read_text(encoding="utf-8")
    return t[t.index("## A. 번역투"):t.index("## 자가검증")].strip().rstrip("-").strip()


@lru_cache
def guidance_text():
    """오탐 방지 · 사람이 쓴 글의 신호 (instruction.md 의 DETECTION GUIDANCE)."""
    t = (REF / "instruction.md").read_text(encoding="utf-8")
    return t[t.index("## 깃발 꽂으면 안 되는 것"):t.index("# 자가검증 체크리스트")].strip()


@lru_cache
def codes():
    """{'A-1': '"~에 대해(서)"', …}  표에서 ID와 패턴 이름."""
    out = {}
    for m in re.finditer(r"^\| ([A-K]-\d+) \| (.+?) \| (S\d)[^|]*\| (.+?) \|$", (REF / "ai-tell-taxonomy.md").read_text(encoding="utf-8"), re.M):
        out[m.group(1)] = {"name": m.group(2), "severity": m.group(3), "fix": m.group(4)}
    return out


GROUPS = {"A": "번역투", "B": "영어 인용·용어", "C": "구조", "D": "AI 관용구", "E": "리듬·종결어미",
          "F": "수식·중복", "G": "완충 남발", "H": "접속사", "I": "형식명사", "J": "시각 장식", "K": "챗봇 잔재"}


# ---------- 규칙 검사 (LLM 없이) ----------
EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF⭐⬆↔-↪]")
RULES = [
    # (코드, 정규식, 심각도, 이유)  — 심각도 None 은 문서 전체 빈도로 정한다
    ("J-2", re.compile(r"[—–]"), None, "줄표(—, –)는 한국어 AI 글의 대표 흔적. 마침표·쉼표·괄호로 바꾸거나 문장을 다시 짠다."),
    ("C-5", EMOJI, "S1", "이모지 장식. 원고·리포트에서는 지운다."),
    ("A-8", re.compile(r"(?:되어|보여)(?:지|진|졌)"), "S1",
     "이중 피동(~되어지다/보여지다). 단일 피동이나 능동으로."),
    ("C-11", re.compile(r"[가-힣](?:고|며|지만|면서|아서|어서|해서|으며|는데),(?=\s)"), None,
     "연결어미 바로 뒤 쉼표. 대부분 쉼표를 빼면 된다."),
    ("K-1", re.compile(r"좋은 질문(?:이에요|입니다)|도움이 되었으면|더 궁금한 점이|궁금한 점이 있으(?:시)?면|물론이죠|아래는 [^.]{0,30}입니다"), "S1",
     "챗봇 응대 잔재. 통째로 지운다."),
    ("D-1", re.compile(r"결론적으로|요약하자면|요약하면"), "S1", "결산 피벗 상투구."),
    ("D-2", re.compile(r"시사하는 바가 크|주목할 만하"), "S1", "AI 관용구. 지우거나 구체 결론으로."),
    ("D-3", re.compile(r"본질적으로|핵심적으로"), "S1", "AI 관용구. 지운다."),
]


def _ctx(text, m, pad=8):
    s, e = max(0, m.start() - pad), min(len(text), m.end() + pad)
    return text[s:e]


def rule_scan(paras):
    """paras: [문단 문자열] → [{para, code, severity, old_text, reason, source:'rule'}]"""
    hits = []
    for i, p in enumerate(paras):
        for code, rx, sev, why in RULES:
            for m in rx.finditer(p):
                hits.append({"para": i + 1, "code": code, "severity": sev, "old_text": _ctx(p, m),
                             "reason": why, "source": "rule"})
    n = {c: sum(h["code"] == c for h in hits) for c in ("J-2", "C-11")}
    for h in hits:  # 줄표 · 쉼표는 '무더기'일 때만 결정적 (단발은 사람도 쓴다)
        if h["code"] == "J-2":
            h["severity"] = "S1" if n["J-2"] >= 3 else "S3"
        elif h["code"] == "C-11":
            h["severity"] = "S1" if n["C-11"] >= 6 else "S2"
    return hits


if __name__ == "__main__":
    print(len(codes()), "patterns")
    print(table_text()[:300])
