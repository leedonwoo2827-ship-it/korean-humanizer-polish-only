"""[반영] → 05_반영본/
  <제목>_윤문본.docx · .txt   채택한 문단은 윤문, 나머지는 원문 그대로
  <제목>_진단표.xlsx          [요약] 등급 · 변경률 · 글자수 · 자가검증 / [문단 대조] / [AI 흔적]
진단만 했으면 진단표만 만든다.
수정 항목 방식(rw["edits"])이면 <제목>_정오표.xlsx · .html(게시판) · .txt 를 만든다 — 책 전체 한 번에, 절 · 쪽 순.
원고는 이 정오표를 보고 저자 확인 뒤 사람이 고친다. 윤문본(docx · txt)은 체크한 것을 적용해 본 참고용.
"""
import html
import json
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import audit, config, humanize, taxonomy

HEAD_FILL = PatternFill("solid", fgColor="E9EEF6")
THIN = Side(style="thin", color="C9D1DD")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")


def _sheet(ws, cols, rows):
    ws.append([c for c, _ in cols])
    for k, (_, w) in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(k)].width = w
        c = ws.cell(row=1, column=k)
        c.font, c.fill, c.border = Font(bold=True), HEAD_FILL, BOX
    for r in rows:
        ws.append(r)
        for k in range(1, len(cols) + 1):
            c = ws.cell(row=ws.max_row, column=k)
            c.alignment, c.border = WRAP, BOX
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{ws.max_row}"


def _items_rows(items):
    codes = taxonomy.codes()
    return [[x["para"], x["code"], codes.get(x["code"], {}).get("name", ""), x["severity"], x["old_text"],
             x["reason"], "규칙" if x.get("source") == "rule" else "AI"] for x in items]


ITEM_COLS = [("문단", 6), ("분류", 7), ("패턴", 30), ("심각도", 7), ("원문 구절", 40), ("이유", 50), ("찾은 곳", 7)]


def errata_rows(did, rw, decisions):
    """수정 항목 전부 → 정오표 행 (절 · 쪽 순). on = 반영(체크) 여부."""
    paras = config.load(did, "01_원문", "paragraphs.json")
    pages = config.pages(did)
    part_of = {i: pt["title"] for pt in humanize.parts(paras) for i in range(pt["start"], pt["end"] + 1)}
    codes = taxonomy.codes()
    out = []
    for e in rw["edits"]:
        on, new, note = audit.edit_on(e, decisions)
        warn = [w for w in ["원문에서 위치 확인 필요" if not e.get("located", True) else "",
                            ("빠짐: " + ", ".join(e["missing"])) if e.get("missing") else ""] if w]
        out.append({"id": e["id"], "on": bool(on), "part": part_of.get(e["para"], ""), "page": pages[e["para"] - 1],
                    "para": e["para"], "old": e["old_text"], "new": new, "p": e["p"],
                    "code": f"{e['code']} {codes.get(e['code'], {}).get('name', '')}", "reason": e["reason"],
                    "note": note, "warn": " / ".join(warn)})
    return sorted(out, key=lambda r: (r["page"] or 0, r["para"]))


ERRATA_CSS = """body{font:13px/1.55 "Malgun Gothic",malgun,sans-serif;margin:20px;color:#222}
table{border-collapse:collapse;width:100%}th,td{border:1px solid #bbb;padding:4px 6px;vertical-align:top;text-align:left}
th{background:#f1ece3;font-size:12px}td.c{text-align:center;white-space:nowrap}.s{color:#666;font-size:11px}
del{color:#a3271b}ins{color:#1e5b2a;text-decoration:none;font-weight:bold}.memo{min-width:120px}"""


def _where(r):
    return f"{r['part'][:14]}<br>{('p.' + str(r['page'])) if r['page'] else ''} · 문단 {r['para']}"


def errata_board(title, rows):
    """게시판에 올릴 정오표 (반영한 것만)."""
    esc = html.escape
    body = "".join(f"<tr><td>{esc(r['part'][:20])}</td><td class=c>{r['page'] or ''}</td><td class=c>{r['para']}</td>"
                   f"<td><del>{esc(r['old'])}</del></td><td><ins>{esc(r['new']) or '(삭제)'}</ins></td>"
                   f"<td>{esc(r['note'])}</td></tr>" for r in rows)
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{esc(title)} 정오표</title>
<style>{ERRATA_CSS}</style></head><body>
<h2>{esc(title)} — 정오표</h2><p>{datetime.now():%Y-%m-%d} · {len(rows)}건</p>
<table><thead><tr><th>절</th><th>쪽</th><th>문단</th><th>기존</th><th>수정</th><th>비고</th></tr></thead><tbody>{body}</tbody></table>
</body></html>"""


def errata_review(did, title, rows):
    """검토용 정오표 (후보 전부). 브라우저에서 체크 · 수정 문구 · 메모를 고치고 [체크 결과 저장] → JSON 을 콘솔 「체크본 가져오기」로.
    인쇄(PDF로 저장)하면 회의용 종이 — 체크칸과 메모칸이 그대로 나온다."""
    esc = html.escape
    body = "".join(
        f"<tr data-id='{esc(r['id'])}'><td class=c><input type=checkbox {'checked' if r['on'] else ''}></td>"
        f"<td class=c>{esc(r['id'])}</td><td class=s>{_where(r)}</td>"
        f"<td><del>{esc(r['old'])}</del></td><td><ins contenteditable class=new>{esc(r['new'])}</ins>{'' if r['new'] else '<span class=s>(삭제)</span>'}</td>"
        f"<td class=c>{r['p']:.0%}</td><td class=s>{esc(r['code'])}<br>{esc(r['reason'])}{('<br><b>⚠ ' + esc(r['warn']) + '</b>') if r['warn'] else ''}</td>"
        f"<td class=memo contenteditable>{esc(r['note'])}</td></tr>" for r in rows)
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{esc(title)} 정오표 검토</title>
<style>{ERRATA_CSS}
.bar{{position:sticky;top:0;background:#fff;padding:8px 0;border-bottom:1px solid #ddd;margin-bottom:8px}}
button{{font:inherit;padding:4px 10px;margin-right:4px}}@media print{{.bar{{display:none}}}}</style></head><body>
<h2>{esc(title)} — 정오표 검토</h2>
<p class=s>{datetime.now():%Y-%m-%d %H:%M} · 후보 {len(rows)}건 · 체크 = 반영. 수정 문구와 메모 칸은 눌러서 고칠 수 있습니다.
 다 고치면 [체크 결과 저장] → 받은 파일을 윤문 콘솔 글 화면의 「체크본 가져오기」로 올리세요. 종이로 회의하려면 인쇄(PDF로 저장).</p>
<div class=bar><button onclick="save()">체크 결과 저장 (JSON)</button><button onclick="print()">인쇄 · PDF로 저장</button>
 <label><input type=checkbox id=only onchange="only()"> 체크한 것만 보기</label> <span id=n class=s></span></div>
<table><thead><tr><th>반영</th><th>ID</th><th>절 · 쪽 · 문단</th><th>기존 문구</th><th>수정 문구</th><th>확률</th><th>분류 · 이유</th><th>메모</th></tr></thead>
<tbody>{body}</tbody></table>
<script>
const rows=[...document.querySelectorAll('tr[data-id]')];
function count(){{document.getElementById('n').textContent='체크 '+rows.filter(r=>r.querySelector('input').checked).length+' / '+rows.length}}
document.addEventListener('change',count);count();
function only(){{const on=document.getElementById('only').checked;rows.forEach(r=>r.style.display=on&&!r.querySelector('input').checked?'none':'')}}
function save(){{const items=rows.map(r=>({{id:r.dataset.id,apply:r.querySelector('input').checked,
 new_text:r.querySelector('.new').innerText.trim(),note:r.querySelector('.memo').innerText.trim()}}));
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify({{doc:{json.dumps(did)},items}},null,1)],{{type:'application/json'}}));
 a.download='체크본_'+{json.dumps(title)}+'.json';a.click()}}
</script></body></html>"""


MEMO_PAD = "&#160;" * 40  # PDF 표는 내용 길이로 열 너비를 정한다 — 빈 메모칸도 손글씨 쓸 만큼 넓게


def errata_pdf(path, title, rows):
    """회의용 종이 정오표 (후보 전부, 체크칸 ☐/☑ · 빈 메모칸). 스캔해서 돌려주면 ID 로 맞춰 반영한다."""
    import pymupdf
    esc = html.escape
    body = "".join(f"<tr><td class=c>{'☑' if r['on'] else '☐'}</td><td class=c>{esc(r['id'])}</td><td class=s>{_where(r)}</td>"
                   f"<td><del>{esc(r['old'])}</del></td><td><ins>{esc(r['new']) or '(삭제)'}</ins></td>"
                   f"<td class=c>{r['p']:.0%}</td><td class=s>{esc(r['code'].split()[0])}</td><td>{esc(r['note']) or MEMO_PAD}</td></tr>"
                   for r in rows)
    doc = (f"<h3>{esc(title)} — 정오표 검토</h3><p class=s>{datetime.now():%Y-%m-%d} · 후보 {len(rows)}건 · ☑ = 반영(지금 체크). "
           f"고칠 것은 체크칸에 표시하고 메모칸에 적어 주세요.</p>"
           "<table><tr><th width=4%>반영</th><th width=5%>ID</th><th width=11%>절 · 쪽 · 문단</th><th width=26%>기존 문구</th>"
           f"<th width=26%>수정 문구</th><th width=5%>확률</th><th width=8%>분류</th><th width=15%>메모</th></tr>{body}</table>")
    css = ("@font-face{font-family:malgun;src:url(malgun.ttf);}\n*{font-family:malgun}\n"
           "body{margin:0;font-size:9pt;color:#222}table{border-collapse:collapse;width:100%}"
           "th,td{border:1px solid #999;padding:3px 4px;vertical-align:top;text-align:left;font-size:9pt}"
           "th{background:#f1ece3}td.c{text-align:center}.s{color:#555;font-size:8pt}"
           "del{color:#a3271b;text-decoration:none}ins{color:#1e5b2a;text-decoration:none;font-weight:bold}")
    story = pymupdf.Story(html=doc, user_css=css, archive=pymupdf.Archive("C:/Windows/Fonts"))
    page = pymupdf.paper_rect("a4-l")
    w = pymupdf.DocumentWriter(str(path))
    more = 1
    while more:
        dev = w.begin_page(page)
        more, _ = story.place(page + (28, 28, -28, -28))
        story.draw(dev)
        w.end_page()
    w.close()


def import_review(did, data, filename):
    """체크본(검토 HTML 에서 저장한 JSON · 정오표.xlsx 의 「전체 후보」 시트) → decisions.json 에 합친다."""
    rw = config.load(did, "03_윤문", "rewrite.json") or {}
    ids = {e["id"] for e in rw.get("edits", [])}
    got = []
    if filename.lower().endswith(".json"):
        got = json.loads(data.decode("utf-8-sig")).get("items", [])
    elif filename.lower().endswith(".xlsx"):
        import io
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(data), read_only=True)["전체 후보"]
        head, *body = [list(r) for r in ws.iter_rows(values_only=True)]
        col = {h: k for k, h in enumerate(head)}
        for r in body:
            got.append({"id": str(r[col["ID"]]), "apply": str(r[col["반영"]] or "").strip().upper() in ("O", "○", "V", "Y", "1", "TRUE"),
                        "new_text": "" if r[col["수정 문구"]] == "(삭제)" else str(r[col["수정 문구"]] or ""),
                        "note": str(r[col["메모"]] or "")})
    else:
        raise ValueError("체크본은 검토 HTML 에서 저장한 .json 이나 정오표 .xlsx 를 올려 주세요.")
    edits = {e["id"]: e for e in rw.get("edits", [])}
    dec = config.load(did, "04_검토", "decisions.json") or {}
    n = 0
    for it in got:
        if it.get("id") not in ids:
            continue
        cur = {"apply": bool(it.get("apply"))}
        if it.get("new_text") is not None and it["new_text"] != edits[it["id"]]["new_text"]:
            cur["new_text"] = it["new_text"]
        if it.get("note"):
            cur["note"] = it["note"]
        dec["e:" + it["id"]] = cur
        n += 1
    config.save(did, "04_검토", "decisions.json", dec)
    print(f"[체크본] {filename} → {n}건 반영 (못 맞춘 줄 {len(got) - n}건)")
    return n, len(got) - n


def run(did, decisions=None, part=None):
    d = config.doc(did)
    paras = config.load(did, "01_원문", "paragraphs.json")
    det = config.load(did, "02_탐지", "detect.json")
    if not det:
        raise ValueError("먼저 [AI 흔적 진단]을 실행하세요.")
    rw = config.load(did, "03_윤문", "rewrite.json")
    if decisions is not None:
        config.save(did, "04_검토", "decisions.json", decisions)
    decisions = config.load(did, "04_검토", "decisions.json") or {}
    name = config.slug(d["title"])
    target = d.get("target")
    if part is not None:  # 그 절의 문단만 (목표 글자수는 글 전체 기준이라 절에는 적용하지 않는다)
        pt = humanize.parts(paras)[part]
        lo, hi = pt["start"], pt["end"]
        paras = paras[lo - 1:hi]
        det = dict(det, items=[x for x in det["items"] if lo <= x["para"] <= hi])
        rw = rw and dict(rw, paras=[p for p in rw["paras"] if lo <= p["i"] <= hi])
        name += "_" + config.slug(pt["title"])[:20]
        target = None
    s = audit.summary(paras, det, rw, decisions, target)
    out = config.stage(did, "05_반영본")
    made = []

    if rw:
        fin = audit.final_paras(rw, decisions)
        text = "\n\n".join(t for _, t, _ in fin if t)
        (out / f"{name}_윤문본.txt").write_text(text, encoding="utf-8")
        import docx
        doc = docx.Document()
        for _, t, _ in fin:
            if t:
                doc.add_paragraph(t)
        doc.save(out / f"{name}_윤문본.docx")
        made += [f"{name}_윤문본.docx", f"{name}_윤문본.txt"]

    wb = Workbook()
    ws = wb.active
    ws.title = "요약"
    rows = [["글", d["title"]], ["장르", d.get("genre", "")], ["만든 때", datetime.now().strftime("%Y-%m-%d %H:%M")],
            ["찾은 흔적", "S1 {S1} · S2 {S2} · S3 {S3}".format(**s["found"])],
            ["원문 글자수", "공백 포함 {with_space} · 제외 {no_space}".format(**s["orig_chars"])]]
    if rw:
        rows += [["등급", f"{s['grade']} — {s['advice']}"], ["변경률", f"{s['rate']:.0%}"],
                 ["채택 문단", f"{s['adopted']} / {s['n_paras']}"],
                 ["남은 흔적", "S1 {S1} · S2 {S2} · S3 {S3}".format(**s["residual"])],
                 ["윤문본 글자수", "공백 포함 {with_space} · 제외 {no_space}".format(**s["final_chars"])]]
        if s.get("target"):
            t = s["target"]
            rows.append(["목표 글자수", f"{t['chars']}자 ({'공백 제외' if t.get('no_space') else '공백 포함'}) → {t['actual']}자 "
                                    + ("✓ ±5% 안" if t["ok"] else "✗ 벗어남")])
        rows += [[f"자가검증 · {k}", "통과" if v else "확인 필요"] for k, v in s["selfcheck"].items()]
        if s["lost"]:
            rows.append(["빠진 숫자·인용", ", ".join(s["lost"])])
    _sheet(ws, [("항목", 22), ("내용", 90)], rows)

    if rw and "edits" in rw:  # 수정 항목 방식: 정오표 (체크한 것) + 전체 후보
        er = errata_rows(did, rw, decisions)
        cols = [("ID", 8), ("절", 18), ("쪽", 6), ("문단", 6), ("기존 문구", 46), ("수정 문구", 46), ("수정확률", 8),
                ("분류", 18), ("이유", 40), ("메모", 24), ("확인", 16)]
        pick = lambda r: [r["id"], r["part"], r["page"] or "", r["para"], r["old"], r["new"] or "(삭제)", f"{r['p']:.0%}",
                          r["code"], r["reason"], r["note"], r["warn"]]
        on = [r for r in er if r["on"]]
        _sheet(wb.create_sheet("정오표", 0), cols, [pick(r) for r in on])
        # 「전체 후보」의 반영(O) · 수정 문구 · 메모를 고쳐 콘솔 「체크본 가져오기」로 올리면 그대로 반영된다
        _sheet(wb.create_sheet("전체 후보", 1), [("반영", 6)] + cols, [["O" if r["on"] else ""] + pick(r) for r in er])
        (out / f"{name}_정오표.txt").write_text(
            "\n".join(f"[{r['part'][:12]} · {('p.' + str(r['page'])) if r['page'] else '문단 ' + str(r['para'])}] "
                      f"'{r['old']}' → '{r['new'] or '(삭제)'}'" for r in on), encoding="utf-8")
        (out / f"{name}_정오표_게시판.html").write_text(errata_board(d["title"], on), encoding="utf-8")
        (out / f"{name}_정오표_검토.html").write_text(errata_review(did, d["title"], er), encoding="utf-8")
        made += [f"{name}_정오표_검토.html", f"{name}_정오표_게시판.html", f"{name}_정오표.txt"]
        try:
            errata_pdf(out / f"{name}_정오표_검토.pdf", d["title"], er)
            made.append(f"{name}_정오표_검토.pdf")
        except Exception as e:  # 글꼴이 없는 PC 등 — 검토 HTML 을 인쇄하면 같은 종이가 나온다
            print(f"  [반영] PDF는 만들지 못했습니다 ({e}). 검토 HTML 을 열어 인쇄 → PDF로 저장하세요.")
    elif rw:
        ws = wb.create_sheet("문단 대조")
        dd = {p["i"]: decisions.get(str(p["i"]), {}) for p in rw["paras"]}
        _sheet(ws, [("문단", 6), ("원문", 60), ("윤문", 60), ("변경률", 8), ("채택", 7), ("비고", 30)],
               [[p["i"], p["orig"], dd[p["i"]].get("text") or p["text"] or "(문단 삭제)", f"{p['rate']:.0%}",
                 "채택" if dd[p["i"]].get("adopt", audit.default_adopt(p)) else "원문 유지",
                 " / ".join(filter(None, ["직접 고침" if dd[p["i"]].get("text") else "",
                                          "변경률 50% 초과" if p["rate"] > audit.STOP else "",
                                          "변경률 30% 초과" if audit.WARN < p["rate"] <= audit.STOP else "",
                                          ("빠짐: " + ", ".join(p["missing"])) if p["missing"] else ""]))]
                for p in rw["paras"]])
        _sheet(wb.create_sheet("남은 흔적"), ITEM_COLS, _items_rows([x for p in rw["paras"] for x in p.get("residual", [])]))
    if rw and "edits" in rw:
        _sheet(wb.create_sheet("남은 흔적"), ITEM_COLS, _items_rows([x for p in rw["paras"] for x in p.get("residual", [])]))
    _sheet(wb.create_sheet("AI 흔적(원문)"), ITEM_COLS, _items_rows(det["items"]))
    book = f"{name}_정오표.xlsx" if rw and "edits" in rw else f"{name}_진단표.xlsx"
    wb.save(out / book)
    made.append(book)
    if part is None:
        d["exported"] = datetime.now().strftime("%m-%d %H:%M")
    else:
        d.setdefault("exported_parts", {})[str(part)] = datetime.now().strftime("%m-%d %H:%M")
    config.save_doc(d)
    print(f"[반영] {', '.join(made)} → 05_반영본/")
    return {"files": made, "summary": s}
