"""윤문 파이프라인: 트리아지 → 탐지 → 윤문 → 감사 → 등급.

글을 약 2,500자 구간으로 묶어 구간마다 Codex를 부른다 (동시 3, 캐시).
  detect(did)   02_탐지/detect.json   흔적 · 분류(A–K) · 심각도(S1–S3) · 원문 구절 · 이유 + 구간별 트리아지
  rewrite(did)  03_윤문/rewrite.json  문단별 윤문 + 변경률 · 보존 검사 + 감사(다시 탐지) → 남은 흔적
                변경률 50% 초과 · 숫자/인용 누락 문단은 한 번 다시 윤문시킨다 (롤백 후 재윤문)
"""
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor

from . import audit, config, taxonomy
from .codex import CodexAuthError, ask_codex

CHUNK = 2500
TRIAGE = ["거의 손댈 것 없음", "서식만 정리", "문장 단위 윤문"]

PRINCIPLES = """4대 철칙
1. 의미 불변 — 사실·주장·수치·고유명사·직접 인용은 원문 그대로. 한 글자도 바꾸거나 지어내지 않는다.
2. 근거 기반 — 탐지된 흔적에만 수술적으로 손댄다. 탐지 없는 멀쩡한 문장은 건드리지 않는다.
3. 장르 유지 — 원고를 에세이·블로그체로 옮기지 않는다. 원문의 격식과 종결 문체(한다체/합니다체 등)를 지킨다.
4. 과윤문 금지 — 문단 변경률 30%를 넘기지 않는다. 멀쩡한 사람 글을 평균값으로 깎아내는 게 가장 흔한 실패다.

절대 건드리지 않는 것: 고유명사·제품명·기관명·인명·지명 / 수치·날짜·단위·통계·수식 / 큰따옴표 안 직접 인용·법률 조문 /
업계 표준 영어 약어(LLM·GPU·API 등) / 글쓴이가 일부러 넣은 구체적 디테일·곁말."""


def _items_schema(extra=None):
    item = {"type": "object", "additionalProperties": False,
            "required": ["para", "code", "severity", "old_text", "reason"],
            "properties": {"para": {"type": "integer"},
                           "code": {"type": "string", "enum": sorted(taxonomy.codes())},
                           "severity": {"type": "string", "enum": taxonomy.SEVERITY},
                           "old_text": {"type": "string"}, "reason": {"type": "string"}}}
    props = {"summary": {"type": "string"}, "items": {"type": "array", "items": item}, **(extra or {})}
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


DETECT_SCHEMA = _items_schema({"triage": {"type": "string", "enum": TRIAGE}})
AUDIT_SCHEMA = _items_schema({k: {"type": "boolean"} for k in ("genre_kept", "register_kept", "no_new_flourish")})
REWRITE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["note", "paras"],
    "properties": {"note": {"type": "string"}, "paras": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["para", "text"],
        "properties": {"para": {"type": "integer"}, "text": {"type": "string"}}}}}}

DETECT_PROMPT = """너는 한국어 원고의 'AI 티'를 진단하는 편집자다. [글]의 각 문단(P번호)을 읽고, 아래 분류표의 AI 흔적을 찾아라. 고치지는 마라.

장르: {genre}

[분류표]
{table}

[오탐 방지]
{guidance}

규칙
1. 흔적은 단발이 아니라 무더기로 판단한다. S3(약함)는 다른 흔적과 겹칠 때만 낸다. 의심스러우면 내지 마라.
2. old_text = 그 문단 원문을 그대로(띄어쓰기·기호 포함) 6~60자 복사. 흔적 부분과 앞뒤 몇 글자. para = 문단 번호(P 뒤 숫자).
3. code 는 분류표의 ID, severity 는 이 글 안의 빈도까지 보고 S1/S2/S3. reason = 왜 AI 흔적인지 한 문장.
4. 맞춤법·사실관계는 판단하지 마라. 같은 흔적이 여러 곳이면 곳마다 따로 낸다.
5. triage: 이 구간을 어디까지 고칠지 — "{t0}"(흔적이 거의 없음) / "{t1}"(줄표·이모지·볼드 같은 서식만 문제) / "{t2}"(산문 자체가 AI식).
6. summary = 이 구간의 AI 티 정도를 한 문장으로. 파일을 읽거나 명령을 실행하지 말고, 이 프롬프트만으로 답하라.

[글]
{part}
"""

REWRITE_PROMPT = """너는 한국어 원고 윤문가다. [글]의 AI 티를 걷어 내고 사람이 쓴 자연스러운 한국어로 되돌려라.

장르: {genre}
이 구간 트리아지: {triage}

{principles}

[분류표 — 처방 열을 따른다]
{table}

[이 구간에서 찾은 흔적]
{found}
{voice}{length}
규칙
1. 찾은 흔적만 고친다. 흔적이 없는 문단은 원문 그대로 돌려준다(한 글자도 바꾸지 마라).
2. 트리아지가 "{t1}"이면 서식(줄표·이모지·볼드·가운뎃점 등)만 정리하고 문장은 그대로 둔다.
3. 지우지 말고 자연스러운 표현으로 '교체'한다. 원문이 다루는 내용은 빠짐없이 다룬다.
4. 윤문하면서 새 흔적을 만들지 마라: 동의어 돌려쓰기, 접속어 추가, 원문에 없던 비유·수사, 줄표(—, –).
5. 문단을 합치거나 나누지 마라. paras 에 [글]의 모든 문단을 같은 para 번호로 하나씩 낸다. text 에는 P번호 표시 없이 문장만.
   문단 전체가 챗봇 잔재(K-1·K-2: 응대·안내·아첨 문구)뿐이면 text 를 빈 문자열로 낸다(문단 삭제). 그 밖의 문단은 지우지 마라.
6. note = 이 구간에서 무엇을 고쳤는지 한 문장. 파일을 읽거나 명령을 실행하지 말고, 이 프롬프트만으로 답하라.
{retry}
[글]
{part}
"""

AUDIT_PROMPT = """너는 윤문 감수자다. [윤문본]이 왜 아직 AI 같은지 다시 묻고, 남은 AI 흔적을 찾아라. 고치지는 마라.

장르: {genre}

[분류표]
{table}

[오탐 방지]
{guidance}

규칙
1. old_text = [윤문본] 문단 원문 그대로 6~60자, para = 문단 번호. 무더기로 판단하고 의심스러우면 내지 마라.
2. genre_kept: [원문]과 비교해 장르가 유지됐는가 (원고가 에세이·블로그체로 떨어지지 않았는가).
3. register_kept: 원문의 격식·종결 문체(한다체/합니다체 등)가 유지됐는가.
4. no_new_flourish: 원문에 없던 비유·수사·문학적 표현을 더하지 않았는가 (더하지 않았으면 true).
5. summary = 남은 AI 티를 한 문장으로. 파일을 읽거나 명령을 실행하지 말고, 이 프롬프트만으로 답하라.

[원문]
{orig}

[윤문본]
{part}
"""


# ---------- 공통 ----------
def chunks(paras):
    """[[문단번호(1부터)…], …]  약 CHUNK 자씩, 문단은 자르지 않는다."""
    out, cur, n = [], [], 0
    for i, p in enumerate(paras, 1):
        if cur and n + len(p) > CHUNK:
            out.append(cur)
            cur, n = [], 0
        cur.append(i)
        n += len(p)
    return out + ([cur] if cur else [])


def numbered(paras, ids):
    return "\n\n".join(f"[P{i}] {paras[i - 1]}" for i in ids)


def loose(s):
    chars = [re.escape(c) for c in re.sub(r"\s+", "", s)]
    return re.compile(r"\s*".join(chars)) if chars else None


def locate(paras, ids, item):
    """old_text 를 실제 문단에서 다시 찾는다 (지정한 문단 먼저). 못 찾으면 None."""
    pat = loose(item["old_text"])
    if not pat:
        return None
    for i in sorted(ids, key=lambda i: i != item["para"]):
        m = pat.search(paras[i - 1])
        if m:
            return {**item, "para": i, "old_text": m.group(0)}
    return None


def merge(rule_items, ai_items):
    """규칙 · AI 결과를 합친다. 같은 문단에서 같은 분류로 겹치는 구절은 하나만 (AI 이유 우선)."""
    out = list(ai_items)
    for r in rule_items:
        if not any(a["para"] == r["para"] and a["code"] == r["code"]
                   and (r["old_text"] in a["old_text"] or a["old_text"] in r["old_text"]) for a in ai_items):
            out.append(r)
    return sorted(out, key=lambda x: (x["para"], x["severity"]))


def cached(did, stage_name, prompt, schema, label):
    cache = config.stage(did, stage_name) / "cache"
    cache.mkdir(exist_ok=True)
    key = hashlib.sha1((prompt + json.dumps(schema)).encode("utf-8")).hexdigest()[:12]
    f = cache / f"{label}_{key}.json"
    if f.exists():
        print(f"  [cache] {label}")
        return json.loads(f.read_text(encoding="utf-8"))
    print(f"  [codex] {label} …", flush=True)
    res = ask_codex(prompt, schema)
    f.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def run_chunks(fn, groups, workers=3):
    """구간마다 fn(k, ids). 로그인 만료는 바로 멈추고, 그 밖의 실패는 그 구간만 비운다."""
    def one(arg):
        k, ids = arg
        try:
            return fn(k, ids)
        except CodexAuthError:
            raise
        except Exception as e:
            print(f"  [오류] 구간 {k + 1}: {e}")
            return None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, enumerate(groups)))


def _doc_paras(did):
    paras = config.load(did, "01_원문", "paragraphs.json")
    if not paras:
        raise SystemExit("원문이 없습니다. 글을 다시 넣어 주세요.")
    return paras


# ---------- 탐지 ----------
def detect(did):
    d, paras = config.doc(did), _doc_paras(did)
    groups = chunks(paras)
    table, guide = taxonomy.table_text(), taxonomy.guidance_text()
    print(f"[탐지] {d['title']} — 문단 {len(paras)}개 · {len(groups)}구간을 Codex로 진단")

    def fn(k, ids):
        prompt = DETECT_PROMPT.format(genre=d.get("genre", "기타"), table=table, guidance=guide,
                                      t0=TRIAGE[0], t1=TRIAGE[1], t2=TRIAGE[2], part=numbered(paras, ids))
        res = cached(did, "02_탐지", prompt, DETECT_SCHEMA, f"c{k + 1:02d}")
        found = [x for x in (locate(paras, ids, {**it, "source": "ai"}) for it in res["items"]) if x]
        print(f"  [탐지] 구간 {k + 1} → {len(found)}건 · {res['triage']}")
        return {"ids": ids, "triage": res["triage"], "summary": res["summary"], "items": found}

    results = run_chunks(fn, groups)
    ai = [x for r in results if r for x in r["items"]]
    items = merge(taxonomy.rule_scan(paras), ai)
    out = {"chunks": [r or {"ids": ids, "triage": TRIAGE[2], "summary": "진단 실패 — 다시 누르세요.", "items": [], "failed": True}
                      for r, ids in zip(results, groups)],
           "items": items}
    config.save(did, "02_탐지", "detect.json", out)
    d["status"] = "진단완료"
    config.save_doc(d)
    s = audit.summary(paras, out)["found"]
    print(f"[탐지] 흔적 {len(items)}건 (S1 {s['S1']} · S2 {s['S2']} · S3 {s['S3']}) → 02_탐지/detect.json")
    return out


# ---------- 윤문 · 감사 ----------
def _rewrite_chunk(did, d, paras, ch, items, k, target_part, retry_note=""):
    ids = ch["ids"]
    found = "\n".join(f"- P{x['para']} [{x['code']} {x['severity']}] \"{x['old_text']}\" — {x['reason']}"
                      for x in items if x["para"] in ids) or "(없음 — 모든 문단을 원문 그대로 돌려줘라)"
    voice = f"\n[글쓴이 문체 샘플 — 이 말투로 바꿔 쓴다. 샘플의 어휘 수준·문장 길이·종결어미를 따른다]\n{d['voice'][:3000]}\n" if d.get("voice") else ""
    length = (f"\n[분량] 이 구간을 공백 포함 약 {target_part}자(±5%)로 맞춘다. 늘릴 때는 원문에 이미 있는 사실을 풀어서, "
              f"줄일 때는 군더더기·완충 표현·중복부터. 없는 사실을 지어내지 않는다. 분량 때문이면 흔적 없는 문단도 고쳐도 된다.\n") if target_part else ""
    prompt = REWRITE_PROMPT.format(genre=d.get("genre", "기타"), triage=ch["triage"], principles=PRINCIPLES,
                                   table=taxonomy.table_text(), found=found, voice=voice, length=length,
                                   t1=TRIAGE[1], retry=retry_note, part=numbered(paras, ids))
    res = cached(did, "03_윤문", prompt, REWRITE_SCHEMA, f"c{k + 1:02d}" + ("_retry" if retry_note else ""))
    got = {p["para"]: re.sub(r"^\s*\[?P\d+\]?\s*", "", p["text"]).strip() for p in res["paras"]}
    return {i: got[i] if i in got else paras[i - 1] for i in ids}, res["note"]


def _para_row(i, orig, text):
    """text == '' 은 챗봇 잔재 문단 삭제 — 빠진 숫자·인용 검사는 하지 않는다."""
    return {"i": i, "orig": orig, "text": text, "rate": audit.rate(orig, text), "deleted": not text,
            "missing": [] if not text else audit.missing(orig, text)}


def rewrite(did, do_audit=True):
    d, paras = config.doc(did), _doc_paras(did)
    det = config.load(did, "02_탐지", "detect.json") or detect(did)
    total = sum(len(p) for p in paras) + 2 * (len(paras) - 1)
    target = d.get("target")
    print(f"[윤문] {d['title']} — {len(det['chunks'])}구간을 Codex로 윤문" + (f" (목표 {target['chars']}자)" if target else ""))

    def fn(k, ch):
        ids = ch["ids"]
        part_len = sum(len(paras[i - 1]) for i in ids)
        tp = round(target["chars"] * part_len / max(1, total)) if target else None
        if ch["triage"] == TRIAGE[0] and not tp and not any(x["para"] in ids and x["severity"] != "S3" for x in det["items"]):
            print(f"  [윤문] 구간 {k + 1} → 흔적이 거의 없어 그대로 둡니다")
            return {"rows": [_para_row(i, paras[i - 1], paras[i - 1]) for i in ids], "note": "그대로 둠"}
        texts, note = _rewrite_chunk(did, d, paras, ch, det["items"], k, tp)
        rows = [_para_row(i, paras[i - 1], texts[i]) for i in ids]
        bad = [r for r in rows if not r["deleted"] and (r["rate"] > audit.STOP and not tp or r["missing"])]
        if bad:  # 롤백 후 재윤문: 문제 문단만 짚어 주고 한 번 더
            why = "\n".join(f"- P{r['i']}: " + (f"변경률 {r['rate']:.0%} (50% 초과) " if r["rate"] > audit.STOP else "")
                            + (f"원문의 {', '.join(r['missing'])} 이(가) 빠짐" if r["missing"] else "") for r in bad)
            print(f"  [재윤문] 구간 {k + 1} — 문단 {len(bad)}개가 너무 많이 바뀌었거나 숫자·인용이 빠져 다시 시킵니다")
            texts2, note2 = _rewrite_chunk(did, d, paras, ch, det["items"], k, tp,
                                           retry_note=f"\n[다시 하기] 지난번 윤문에서 아래 문단이 철칙을 어겼다. 이번엔 탐지된 흔적만 최소로 고치고, 빠진 것은 원문대로 살려라.\n{why}\n")
            for n, r in enumerate(rows):
                r2 = _para_row(r["i"], r["orig"], texts2[r["i"]])
                if r in bad and (len(r2["missing"]), r2["rate"]) < (len(r["missing"]), r["rate"]):
                    rows[n] = r2
            note = note2
        print(f"  [윤문] 구간 {k + 1} → 바뀐 문단 {sum(r['text'] != r['orig'] for r in rows)}/{len(rows)}")
        res = {"rows": rows, "note": note}
        if do_audit and any(r["text"] != r["orig"] for r in rows):
            new = [r["text"] for r in rows]
            orig = [r["orig"] for r in rows]
            local = {i: n + 1 for n, i in enumerate(ids)}  # 감사는 구간 안 번호로
            prompt = AUDIT_PROMPT.format(genre=d.get("genre", "기타"), table=taxonomy.table_text(),
                                         guidance=taxonomy.guidance_text(),
                                         orig=numbered(orig, range(1, len(orig) + 1)),
                                         part=numbered(new, range(1, len(new) + 1)))
            a = cached(did, "03_윤문", prompt, AUDIT_SCHEMA, f"c{k + 1:02d}_audit")
            back = {v: kk for kk, v in local.items()}
            left = [x for x in (locate(new, range(1, len(new) + 1), {**it, "source": "ai"}) for it in a["items"]) if x]
            left = merge([x for x in taxonomy.rule_scan(new)], left)
            for x in left:
                x["para"] = back[x["para"]]
            res.update(residual=left, checks={kk: a[kk] for kk in ("genre_kept", "register_kept", "no_new_flourish")},
                       audit_summary=a["summary"])
            print(f"  [감사] 구간 {k + 1} → 남은 흔적 {len(left)}건")
        elif do_audit:
            res["residual"] = [x for x in det["items"] if x["para"] in ids]
        return res

    results = run_chunks(fn, det["chunks"])
    rows, notes, checks = [], [], {"genre_kept": True, "register_kept": True, "no_new_flourish": True}
    for ch, res in zip(det["chunks"], results):
        if res is None:  # 실패한 구간은 원문 유지
            res = {"rows": [_para_row(i, paras[i - 1], paras[i - 1]) for i in ch["ids"]], "note": "윤문 실패 — 다시 누르세요.", "failed": True}
        for r in res["rows"]:
            r["residual"] = [x for x in res.get("residual", []) if x["para"] == r["i"]]
        rows += res["rows"]
        notes.append({"ids": [ch["ids"][0], ch["ids"][-1]], "note": res["note"], "audit": res.get("audit_summary", ""),
                      "failed": bool(res.get("failed"))})
        for kk in checks:
            checks[kk] = checks[kk] and res.get("checks", {}).get(kk, True)
    out = {"paras": rows, "notes": notes, "checks": checks}
    config.save(did, "03_윤문", "rewrite.json", out)
    config.save(did, "04_검토", "decisions.json", {})  # 새 윤문이면 체크는 기본값부터
    d["status"] = "윤문완료"
    config.save_doc(d)
    s = audit.summary(paras, det, out, {}, target)
    print(f"[윤문] 끝 — 등급 {s['grade']} · 변경률 {s['rate']:.0%} · 남은 S1 {s['residual']['S1']} / S2 {s['residual']['S2']}")
    return out


if __name__ == "__main__":
    import sys
    mode, did = sys.argv[1], sys.argv[2]
    detect(did) if mode == "detect" else rewrite(did)
