"""윤문 파이프라인: 트리아지 → 탐지 → 윤문 → 감사 → 등급.

글을 약 6,000자 구간으로 묶어 구간마다 엔진(Codex · Claude, llm.py)을 부른다 (동시 3, 캐시).
구간이 작으면 호출이 너무 많아 구독 사용 한도에 금방 닿는다 (26만 자 원고 = 진단 106회였음).
  detect(did)   02_탐지/detect.json   흔적 · 분류(A–K) · 심각도(S1–S3) · 원문 구절 · 이유 + 구간별 트리아지
  rewrite(did)  03_윤문/rewrite.json  문단별 윤문 + 변경률 · 보존 검사 + 감사(다시 탐지) → 남은 흔적
                변경률 50% 초과 · 숫자/인용 누락 문단은 한 번 다시 윤문시킨다 (롤백 후 재윤문)
"""
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor

from . import audit, config, llm, taxonomy
from .codex import EngineStop

CHUNK = 6000
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
REWRITE_SCHEMA = {  # 수정 항목(정오표 한 줄) 목록 — 문단 전체를 다시 쓰지 않는다
    "type": "object", "additionalProperties": False, "required": ["note", "items"],
    "properties": {"note": {"type": "string"}, "items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["para", "old_text", "new_text", "code", "p_revise", "reason"],
        "properties": {"para": {"type": "integer"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
                       "code": {"type": "string", "enum": sorted(taxonomy.codes())},
                       "p_revise": {"type": "number"}, "reason": {"type": "string"}}}}}}

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

REWRITE_PROMPT = """너는 한국어 원고 윤문가다. [글]의 AI 티를 걷어 낼 '수정 항목'을 정오표 한 줄씩 내라.
원고는 저자가 이 정오표를 보고 직접 고친다. 그래서 문단 전체를 다시 쓰지 말고, 고칠 구절과 고친 구절만 짝지어 낸다.

장르: {genre}
이 구간 트리아지: {triage}

{principles}

[분류표 — 처방 열을 따른다]
{table}

[이 구간에서 찾은 흔적]
{found}
{voice}{length}
규칙
1. 찾은 흔적만 고친다. 흔적이 없는 문단은 항목을 내지 않는다.
2. 트리아지가 "{t1}"이면 서식(줄표·이모지·볼드·가운뎃점 등)만 정리하고 문장은 그대로 둔다.
3. 항목 하나 = 흔적 하나. old_text = 그 문단 원문을 띄어쓰기·기호까지 그대로 복사한 구절(6~120자, 그 문단 안에서 한 번만 나오게,
   흔적 부분과 앞뒤 몇 글자, 길어도 한 문장). new_text = old_text 를 대신할 구절. para = 문단 번호(P 뒤 숫자).
   항목끼리 old_text 범위가 겹치지 않게 한다. 한 문장에 흔적이 여럿이면 그 문장 하나를 한 항목으로 묶어도 된다.
4. 지우지 말고 자연스러운 표현으로 '교체'한다. 원문이 다루는 내용은 빠짐없이 다룬다. 숫자·인용·고유명사는 new_text 에 그대로 남긴다.
5. 윤문하면서 새 흔적을 만들지 마라: 동의어 돌려쓰기, 접속어 추가, 원문에 없던 비유·수사, 줄표(—, –).
6. 문단 전체가 챗봇 잔재(K-1·K-2: 응대·안내·아첨 문구)뿐이면 old_text 를 그 문단 전체, new_text 를 빈 문자열로 낸다(문단 삭제).
7. code = 이 흔적의 분류표 ID. reason = 왜 고치는지 한 문장.
   p_revise = 이 수정을 반영해야 할 확률(0~1). AI 흔적이 분명하고 고친 구절이 뜻을 그대로 지키면 높게(0.8 이상),
   사람도 흔히 쓰는 표현이거나 취향 차이에 가까우면 낮게(0.4 이하). 저자가 이 값을 보고 반영 여부를 고른다.
8. note = 이 구간에서 무엇을 고쳤는지 한 문장. 파일을 읽거나 명령을 실행하지 말고, 이 프롬프트만으로 답하라.
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
def chunks(paras, parts_=None):
    """[[문단번호(1부터)…], …]  약 CHUNK 자씩, 문단은 자르지 않고 부분(절) 경계도 넘지 않는다."""
    out = []
    for pt in parts_ or [{"start": 1, "end": len(paras)}]:
        cur, n = [], 0
        for i in range(pt["start"], pt["end"] + 1):
            if cur and n + len(paras[i - 1]) > CHUNK:
                out.append(cur)
                cur, n = [], 0
            cur.append(i)
            n += len(paras[i - 1])
        out += [cur] if cur else []
    return out


# ---------- 부분(절·장) — 긴 글을 나눠 골라 돌린다 ----------
PART_RX = re.compile(r"^\s*(?:제\s*)?(\d{1,2})\s*(?:절|장|부)\s+\S|^\s*(?:PART|Part|Chapter|CHAPTER)\s*(\d{1,2})\b")
PART_MIN = 20000    # 이보다 짧은 글은 나누지 않는다
PART_CHARS = 30000  # 절·장 제목이 없으면 이만큼씩


def parts(paras):
    """[{k, title, start, end, chars}]  문단 번호는 1부터, end 포함.
    '1절 … 2절 …' 처럼 번호가 1부터 차례로 오르는 짧은 제목 줄로 나눈다 (목차·요약의 같은 번호는 건너뜀).
    제목이 없으면 약 PART_CHARS 자씩."""
    n, total = len(paras), sum(map(len, paras))
    if total < PART_MIN:
        return [{"k": 0, "title": "전체", "start": 1, "end": n, "chars": total}]
    heads, want = [], 1
    for i, p in enumerate(paras, 1):
        m = PART_RX.match(p)
        if not m or len(p) > 80 or re.search(r"\s\d{1,3}\s*$", p):  # 끝에 쪽번호가 붙은 줄은 목차
            continue
        no = int(m.group(1) or m.group(2))
        if no == 1 and heads and heads[-1][0] - heads[0][0] < 60:  # 촘촘히 모인 제목(목차) 뒤에 본문 1절이 다시 나오면 거기서부터
            heads, want = [], 1
        if no == want:
            heads.append((i, p.strip()))
            want += 1
    if len(heads) < 2:
        heads, acc = [], PART_CHARS
        for i, p in enumerate(paras, 1):
            if acc >= PART_CHARS:
                heads.append((i, None))
                acc = 0
            acc += len(p)
    out = [{"start": 1, "end": heads[0][0] - 1, "title": "앞부분 (표지·목차)"}] if heads[0][0] > 1 else []
    for j, (i, t) in enumerate(heads):
        end = heads[j + 1][0] - 1 if j + 1 < len(heads) else n
        out.append({"start": i, "end": end, "title": t or f"{j + 1}부분"})
    for k, pt in enumerate(out):
        pt.update(k=k, chars=sum(len(p) for p in paras[pt["start"] - 1:pt["end"]]))
    return out


def picked(paras, ks):
    """고른 부분 번호들 → 문단 번호 집합 (ks 가 None 이면 None = 전체)."""
    if ks is None:
        return None
    return {i for pt in parts(paras) if pt["k"] in ks for i in range(pt["start"], pt["end"] + 1)}


def audited(nt):
    """감사까지 끝난 구간 기록인가. 바뀐 문단이 없던 구간은 감사할 것이 없으니 끝난 것으로 본다.
    예전 기록(audited 표시 없음)은 감사 요약이 있으면 끝난 것."""
    return nt.get("audited", bool(nt.get("audit")) or nt.get("note") == "그대로 둠")


FMT = 2  # 윤문 결과 형식: 2 = 수정 항목(정오표). 예전(문단 통째로 다시 쓰기) 결과는 다시 윤문할 것으로 본다


def done_paras(rw, stage="rewrite"):
    """윤문(stage='audit' 면 감사까지)이 끝난 문단 번호. 예전 형식으로 한 윤문은 안 한 것으로 센다."""
    return {i for nt in (rw or {}).get("notes", []) if not (nt.get("failed") or nt.get("pending")) and nt.get("fmt") == FMT
            and (stage != "audit" or audited(nt)) for i in range(nt["ids"][0], nt["ids"][1] + 1)}


def _only_run(groups, only):
    """only(문단 번호 집합)에 걸치는 구간 번호만. only 가 None 이면 전부."""
    return [k for k, ids in enumerate(groups) if only is None or only & set(ids)]


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
    print(f"  [{llm.label().lower()}] {label} …", flush=True)
    res = llm.ask(prompt, schema)
    f.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def run_chunks(fn, groups, workers=3):
    """구간마다 fn(k, ids). 로그인 만료 · 사용 한도 · 멈춤이면 남은 구간은 부르지 않고 멈춘다.
    그 밖의 실패는 그 구간만 비운다. (끝난 구간은 캐시에 남아 다시 누르면 이어진다)"""
    stop = []

    def one(arg):
        k, ids = arg
        if stop:
            return None
        try:
            return fn(k, ids)
        except EngineStop as e:
            stop.append(e)
        except Exception as e:
            print(f"  [오류] 구간 {k + 1}: {e}")
        return None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        out = list(ex.map(one, enumerate(groups)))
    if stop:
        print(f"  [멈춤] {sum(r is not None for r in out)}/{len(groups)}구간까지 했습니다 — 다시 누르면 남은 구간부터 이어서 합니다")
        raise stop[0]
    return out


def _doc_paras(did):
    paras = config.load(did, "01_원문", "paragraphs.json")
    if not paras:
        raise SystemExit("원문이 없습니다. 글을 다시 넣어 주세요.")
    return paras


# ---------- 탐지 ----------
def detect(did, only=None):
    """only = 진단할 문단 번호 집합 (고른 절만). 나머지 구간은 예전 진단 결과를 그대로 둔다."""
    d, paras = config.doc(did), _doc_paras(did)
    old = config.load(did, "02_탐지", "detect.json")  # 다시 진단이면 예전 구간 그대로 — 캐시가 맞아 실패 구간만 부른다
    groups = [c["ids"] for c in old["chunks"]] if old and sum(len(c["ids"]) for c in old["chunks"]) == len(paras) \
        else chunks(paras, parts(paras))
    prev = {tuple(c["ids"]): c for c in old["chunks"] if not (c.get("failed") or c.get("pending"))} if old else {}
    run = _only_run(groups, only)
    table, guide = taxonomy.table_text(), taxonomy.guidance_text()
    print(f"[탐지] {d['title']} — 문단 {len(paras)}개 · {len(run)}구간을 {llm.label()}로 진단"
          + (f" (고른 부분만 · 전체 {len(groups)}구간)" if only is not None else ""))

    def fn(k, ids):
        prompt = DETECT_PROMPT.format(genre=d.get("genre", "기타"), table=table, guidance=guide,
                                      t0=TRIAGE[0], t1=TRIAGE[1], t2=TRIAGE[2], part=numbered(paras, ids))
        res = cached(did, "02_탐지", prompt, DETECT_SCHEMA, f"c{k + 1:02d}")
        found = [x for x in (locate(paras, ids, {**it, "source": "ai"}) for it in res["items"]) if x]
        print(f"  [탐지] 구간 {k + 1} → {len(found)}건 · {res['triage']}")
        return {"ids": ids, "triage": res["triage"], "summary": res["summary"], "items": found}

    got = dict(zip(run, run_chunks(lambda j, ids: fn(run[j], ids), [groups[k] for k in run])))
    out_chunks = []
    for k, ids in enumerate(groups):
        if k in got:
            out_chunks.append(got[k] or {"ids": ids, "triage": TRIAGE[2], "summary": "진단 실패 — 다시 누르세요.", "items": [], "failed": True})
        else:
            out_chunks.append(prev.get(tuple(ids)) or {"ids": ids, "triage": TRIAGE[2], "summary": "아직 진단 안 함", "items": [], "pending": True})
    items = merge(taxonomy.rule_scan(paras), [x for c in out_chunks for x in c["items"]])
    out = {"chunks": out_chunks, "items": items}
    config.save(did, "02_탐지", "detect.json", out)
    d["status"] = "진단완료"
    config.save_doc(d)
    s = audit.summary(paras, out)["found"]
    print(f"[탐지] 흔적 {len(items)}건 (S1 {s['S1']} · S2 {s['S2']} · S3 {s['S3']}) → 02_탐지/detect.json")
    return out


# ---------- 윤문 · 감사 ----------
def regroup(det_chunks, paras, parts_=None):
    """진단 구간을 약 CHUNK 자로 다시 묶는다 — 예전(2,500자) 진단을 그대로 쓰면서 윤문 호출 수를 줄인다.
    부분(절) 경계는 넘지 않는다 (걸친 진단 구간은 경계에서 자른다). 트리아지는 묶인 것 중 가장 센 것."""
    part_of = {i: pt["k"] for pt in parts_ or [] for i in range(pt["start"], pt["end"] + 1)}
    pieces = []
    for ch in det_chunks:
        for k in dict.fromkeys(part_of.get(i) for i in ch["ids"]):
            pieces.append({"ids": [i for i in ch["ids"] if part_of.get(i) == k], "triage": ch["triage"], "part": k})
    out, n = [], 0
    for ch in pieces:
        size = sum(len(paras[i - 1]) for i in ch["ids"])
        if out and n + size <= CHUNK and out[-1]["part"] == ch["part"]:
            out[-1] = {"ids": out[-1]["ids"] + ch["ids"], "part": ch["part"],
                       "triage": TRIAGE[max(TRIAGE.index(out[-1]["triage"]), TRIAGE.index(ch["triage"]))]}
            n += size
        else:
            out.append(dict(ch, ids=list(ch["ids"])))
            n = size
    return out


def _rewrite_chunk(did, d, paras, ch, items, k, target_part, retry_note=""):
    """한 구간 → 수정 항목 [{id, para, old_text, new_text, code, p, reason, located, missing}], note.
    old_text 는 원문에서 다시 찾아 정확한 구절로 바꾼다. 못 찾으면 located=False (정오표엔 '위치 확인 필요')."""
    ids = ch["ids"]
    found = "\n".join(f"- P{x['para']} [{x['code']} {x['severity']}] \"{x['old_text']}\" — {x['reason']}"
                      for x in items if x["para"] in ids) or "(없음 — 항목을 내지 마라)"
    voice = f"\n[글쓴이 문체 샘플 — 이 말투로 바꿔 쓴다. 샘플의 어휘 수준·문장 길이·종결어미를 따른다]\n{d['voice'][:3000]}\n" if d.get("voice") else ""
    length = (f"\n[분량] 이 구간을 공백 포함 약 {target_part}자(±5%)로 맞춘다. 늘릴 때는 원문에 이미 있는 사실을 풀어서, "
              f"줄일 때는 군더더기·완충 표현·중복부터. 없는 사실을 지어내지 않는다. 분량 때문이면 흔적 없는 문단도 고쳐도 된다.\n") if target_part else ""
    prompt = REWRITE_PROMPT.format(genre=d.get("genre", "기타"), triage=ch["triage"], principles=PRINCIPLES,
                                   table=taxonomy.table_text(), found=found, voice=voice, length=length,
                                   t1=TRIAGE[1], retry=retry_note, part=numbered(paras, ids))
    res = cached(did, "03_윤문", prompt, REWRITE_SCHEMA, f"c{k + 1:02d}" + ("_retry" if retry_note else ""))
    edits, n = [], {}
    for it in res["items"]:
        para = it["para"] if it["para"] in ids else ids[0]
        hit = locate(paras, ids, {**it, "para": para}) if it["old_text"].strip() else None
        e = {"para": hit["para"] if hit else para, "old_text": hit["old_text"] if hit else it["old_text"],
             "new_text": re.sub(r"^\s*\[?P\d+\]?\s*", "", it["new_text"]).strip(), "code": it["code"],
             "p": round(max(0.0, min(1.0, float(it["p_revise"]))), 2), "reason": it["reason"], "located": bool(hit)}
        if e["old_text"] == e["new_text"]:
            continue
        e["missing"] = audit.missing(e["old_text"], e["new_text"]) if e["new_text"] else []
        n[e["para"]] = n.get(e["para"], 0) + 1
        e["id"] = f"{e['para']}-{n[e['para']]}"
        edits.append(e)
    return edits, res["note"]


def apply_all(orig, edits):
    text, used = audit.apply_edits(orig, edits)
    return text.strip() if used else orig


def _para_row(i, orig, text):
    """text == '' 은 챗봇 잔재 문단 삭제 — 빠진 숫자·인용 검사는 하지 않는다."""
    return {"i": i, "orig": orig, "text": text, "rate": audit.rate(orig, text), "deleted": not text,
            "missing": [] if not text else audit.missing(orig, text)}


def _audit_group(did, d, rows, label, name):
    """윤문한 한 구간을 감사 → {residual, checks, audit_summary}. 프롬프트가 같으면 캐시를 다시 쓴다."""
    new, orig = [r["text"] for r in rows], [r["orig"] for r in rows]
    back = {n + 1: r["i"] for n, r in enumerate(rows)}  # 감사는 구간 안 번호로
    prompt = AUDIT_PROMPT.format(genre=d.get("genre", "기타"), table=taxonomy.table_text(),
                                 guidance=taxonomy.guidance_text(),
                                 orig=numbered(orig, range(1, len(orig) + 1)),
                                 part=numbered(new, range(1, len(new) + 1)))
    a = cached(did, "03_윤문", prompt, AUDIT_SCHEMA, label)
    left = [x for x in (locate(new, range(1, len(new) + 1), {**it, "source": "ai"}) for it in a["items"]) if x]
    left = merge([x for x in taxonomy.rule_scan(new)], left)
    for x in left:
        x["para"] = back[x["para"]]
    print(f"  [감사] {name} → 남은 흔적 {len(left)}건")
    return {"residual": left, "checks": {kk: a[kk] for kk in ("genre_kept", "register_kept", "no_new_flourish")},
            "audit_summary": a["summary"]}


def audit_stage(did, only=None):
    """3 감사 — 윤문은 됐고 감사는 안 된 구간만 (only = 고른 문단 번호). 윤문 안 된 곳은 건너뛴다."""
    d, paras = config.doc(did), _doc_paras(did)
    rw = config.load(did, "03_윤문", "rewrite.json")
    if not rw:
        print(f"[감사] {d['title']} — 아직 윤문하지 않아 건너뜁니다")
        return None
    det = config.load(did, "02_탐지", "detect.json")
    starts = {g["ids"][0]: k for k, g in enumerate(regroup(det["chunks"], paras, parts(paras)))}  # 윤문과 같은 캐시 이름
    by = {r["i"]: r for r in rw["paras"]}
    want = [nt for nt in rw["notes"] if not (nt.get("failed") or nt.get("pending")) and not audited(nt)
            and (only is None or only & set(range(nt["ids"][0], nt["ids"][1] + 1)))]
    print(f"[감사] {d['title']} — {len(want)}구간을 {llm.label()}로 감사" + (" (고른 부분만)" if only is not None else ""))

    def fn(j, nt):
        rows = [by[i] for i in range(nt["ids"][0], nt["ids"][1] + 1)]
        if not any(r["text"] != r["orig"] for r in rows):
            return {"residual": [x for x in det["items"] if nt["ids"][0] <= x["para"] <= nt["ids"][1]], "checks": {}, "audit_summary": ""}
        k = starts.get(nt["ids"][0])
        return _audit_group(did, d, rows, f"c{k + 1:02d}_audit" if k is not None else f"p{nt['ids'][0]}_audit",
                            f"문단 {nt['ids'][0]}–{nt['ids'][1]}")

    results = run_chunks(fn, want)
    for nt, res in zip(want, results):
        if res is None:
            continue
        for i in range(nt["ids"][0], nt["ids"][1] + 1):
            by[i]["residual"] = [x for x in res["residual"] if x["para"] == i]
        nt.update(audited=True, audit=res["audit_summary"])
        for kk, v in res["checks"].items():
            rw["checks"][kk] = rw["checks"].get(kk, True) and v
    config.save(did, "03_윤문", "rewrite.json", rw)
    left = sum(r is None for r in results)
    s = audit.summary(paras, det, rw, config.load(did, "04_검토", "decisions.json") or {}, d.get("target"))
    print(f"[감사] 끝 — 등급 {s['grade']} · 남은 S1 {s['residual']['S1']} / S2 {s['residual']['S2']}"
          + (f" · 실패 {left}구간 (다시 누르면 그것만)" if left else ""))
    return rw


def rewrite(did, do_audit=True, only=None):
    """only = 윤문할 문단 번호 집합 (고른 절만). 나머지는 예전 윤문 결과 · 체크를 그대로 둔다."""
    d, paras = config.doc(did), _doc_paras(did)
    det = config.load(did, "02_탐지", "detect.json") or detect(did, only)
    old = config.load(did, "03_윤문", "rewrite.json") if only is not None else None
    total = sum(len(p) for p in paras) + 2 * (len(paras) - 1)
    target = d.get("target")
    groups = regroup(det["chunks"], paras, parts(paras))
    run = _only_run([g["ids"] for g in groups], only)
    print(f"[윤문] {d['title']} — {len(run)}구간을 {llm.label()}로 윤문" + (f" (목표 {target['chars']}자)" if target else "")
          + (f" (고른 부분만 · 전체 {len(groups)}구간)" if only is not None else ""))

    def fn(k, ch):
        ids = ch["ids"]
        part_len = sum(len(paras[i - 1]) for i in ids)
        tp = round(target["chars"] * part_len / max(1, total)) if target else None
        if ch["triage"] == TRIAGE[0] and not tp and not any(x["para"] in ids and x["severity"] != "S3" for x in det["items"]):
            print(f"  [윤문] 구간 {k + 1} → 흔적이 거의 없어 그대로 둡니다")
            return {"rows": [_para_row(i, paras[i - 1], paras[i - 1]) for i in ids], "note": "그대로 둠"}
        edits, note = _rewrite_chunk(did, d, paras, ch, det["items"], k, tp)
        by = {}
        for e in edits:
            if e["located"]:
                by.setdefault(e["para"], []).append(e)
        rows = [_para_row(i, paras[i - 1], apply_all(paras[i - 1], by.get(i, []))) for i in ids]  # 모두 반영했을 때 (감사·변경률용)
        print(f"  [윤문] 구간 {k + 1} → 수정 항목 {len(edits)}건 (60% 이상 {sum(e['p'] >= audit.EDIT_ON for e in edits)}건)"
              + (f" · 위치 못 찾음 {sum(not e['located'] for e in edits)}건" if any(not e["located"] for e in edits) else ""))
        res = {"rows": rows, "note": note, "audited": False, "edits": edits}
        if not any(r["text"] != r["orig"] for r in rows):
            res.update(residual=[x for x in det["items"] if x["para"] in ids], audited=True)
        elif do_audit:
            res.update(_audit_group(did, d, rows, f"c{k + 1:02d}_audit", f"구간 {k + 1}"), audited=True)
        return res

    got = dict(zip(run, run_chunks(lambda j, ch: fn(run[j], ch), [groups[k] for k in run])))
    # 고르지 않은 부분: 예전 윤문 문단 · 구간 기록을 그대로 (없으면 원문 + '아직 안 함')
    old_rows = {r["i"]: r for r in (old or {}).get("paras", [])}
    redo = {i for k in got for i in groups[k]["ids"]}
    old_notes = [nt for nt in (old or {}).get("notes", []) if not redo & set(range(nt["ids"][0], nt["ids"][1] + 1))]
    rows, notes = [], list(old_notes)
    edits = [e for e in (old or {}).get("edits", []) if e["para"] not in redo]
    checks = {kk: (old or {}).get("checks", {}).get(kk, True) for kk in ("genre_kept", "register_kept", "no_new_flourish")}
    for k, ch in enumerate(groups):
        ids = ch["ids"]
        if k not in got:
            rows += [old_rows.get(i) or dict(_para_row(i, paras[i - 1], paras[i - 1]), residual=[]) for i in ids]
            if not any(set(range(nt["ids"][0], nt["ids"][1] + 1)) & set(ids) for nt in old_notes):
                notes.append({"ids": [ids[0], ids[-1]], "note": "아직 윤문 안 함", "audit": "", "pending": True})
            continue
        res = got[k]
        if res is None:  # 실패한 구간은 원문 유지
            res = {"rows": [_para_row(i, paras[i - 1], paras[i - 1]) for i in ids], "note": "윤문 실패 — 다시 누르세요.", "failed": True}
        for r in res["rows"]:
            r["residual"] = [x for x in res.get("residual", []) if x["para"] == r["i"]]
        rows += res["rows"]
        edits += res.get("edits", [])
        notes.append({"ids": [ids[0], ids[-1]], "note": res["note"], "audit": res.get("audit_summary", ""),
                      "failed": bool(res.get("failed")), "audited": bool(res.get("audited")), "fmt": FMT})
        for kk in checks:
            checks[kk] = checks[kk] and res.get("checks", {}).get(kk, True)
    notes.sort(key=lambda nt: nt["ids"][0])
    edits.sort(key=lambda e: (e["para"], paras[e["para"] - 1].find(e["old_text"])))
    out = {"paras": rows, "notes": notes, "checks": checks, "edits": edits}
    config.save(did, "03_윤문", "rewrite.json", out)
    dec = config.load(did, "04_검토", "decisions.json") or {} if only is not None else {}
    para_of = lambda key: int(key[2:].split("-")[0]) if key.startswith("e:") else int(key)
    config.save(did, "04_검토", "decisions.json", {k: v for k, v in dec.items() if para_of(k) not in redo})  # 새로 윤문한 문단만 체크를 기본값부터
    d["status"] = "윤문완료"
    config.save_doc(d)
    s = audit.summary(paras, det, out, {}, target)
    print(f"[윤문] 끝 — 등급 {s['grade']} · 변경률 {s['rate']:.0%} · 남은 S1 {s['residual']['S1']} / S2 {s['residual']['S2']}")
    return out


if __name__ == "__main__":
    import sys
    mode, did = sys.argv[1], sys.argv[2]
    detect(did) if mode == "detect" else rewrite(did)
