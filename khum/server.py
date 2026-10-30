"""한국어 AI 윤문 에이전트 — 웹 콘솔 서버 (이 PC 안에서만, 표준 라이브러리만 사용).

주소는 반드시 http://localhost:8767/ 로 안내한다.
(http://127.0.0.1:8767/ 로는 안 되는 자리가 있다 — 그래서 127.0.0.1 과 ::1 양쪽에 다 열어 두고, 안내·브라우저 열기는 localhost 로만.)

  GET  /                        콘솔 화면 (khum/console.html)
  GET  /api/state               글 목록 · 보관함 · 작업 상태 · Codex · Claude 상태 · 설정 · 이어서 진행 상태
  GET  /api/doc?id=01           글 상세 (원문 문단 · 탐지 · 윤문 · 체크 · 요약 · 반영본 파일)
  POST /api/new                 글 넣기: 파일(X-Filename) 또는 붙여넣기 JSON {title, text}
  POST /api/doc/meta?id=        제목 · 장르 · 목표 글자수 · 문체 샘플 저장
  POST /api/detect?id=          AI 흔적 진단 (백그라운드)
  POST /api/rewrite?id=         윤문 + 감사 (백그라운드, 진단이 없으면 진단부터)
  POST /api/decide?id=          문단별 채택/원문 유지 · 직접 고친 문장 저장 → 요약 다시 계산
  POST /api/export?id=          [반영] → 05_반영본 (정오표 xlsx · 검토 pdf/html · 게시판 html · txt + 참고용 윤문본)
  POST /api/import-review?id=   체크본 가져오기: 검토 HTML 에서 저장한 .json 또는 정오표 .xlsx (X-Filename)
  POST /api/second?id=          2차 윤문: 지금 윤문본을 새 글로 넣는다
  POST /api/open?id=&stage=     탐색기로 폴더 열기
  POST /api/archive?id=         보관 (backup/ 으로 옮김) · POST /api/restore {name}
  POST /api/batch               대시보드 단계 버튼: 체크한 글·절 {items: [{id, parts}], steps: [detect·rewrite·audit·export]} (한도면 기다림)
  POST /api/stop                지금 작업 멈춤 (끝난 구간은 캐시에 남음)
  POST /api/settings            엔진(auto · codex · claude) · 한도 대기 시간
  POST /api/codex/refresh|login|logout · /api/claude/refresh|login|logout
  GET  /files/<id>/<path>[?dl=1]
"""
import json
import mimetypes
import os
import socket
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import audit, batch, claude_auth, codex_auth, config, export, extract, humanize, llm, taxonomy

PORT = 8767
CONSOLE = Path(__file__).with_name("console.html")


class Job:
    """한 번에 하나의 백그라운드 작업. print 출력을 로그로 모은다. (TS 속독 에이전트와 같음)"""

    def __init__(self):
        self.lock = threading.Lock()
        self.name, self.uid, self.running, self.error, self.error_kind = None, None, False, None, None
        self.log, self.started, self.ended = [], None, None

    def start(self, name, uid, fn):
        with self.lock:
            if self.running:
                return False
            self.name, self.uid, self.running, self.error, self.error_kind = name, uid, True, None, None
            self.log, self.started, self.ended = [], datetime.now(), None
            llm.CANCEL.clear()
        threading.Thread(target=self._run, args=(fn,), daemon=True).start()
        return True

    def _run(self, fn):
        real, job = sys.stdout, self

        class Tee:
            buf = ""

            def write(self, s):
                real.write(s)
                self.buf += s
                *lines, self.buf = self.buf.split("\n")
                job.log.extend(l for l in lines if l.strip())
                return len(s)

            def flush(self):
                real.flush()

        sys.stdout = Tee()
        try:
            fn()
            print(f"[완료] {self.name}")
        except SystemExit as e:
            self.error = str(e)
            print(f"[중단] {e}")
        except Exception as e:
            self.error, self.error_kind = str(e), getattr(e, "kind", None)
            if self.error_kind == "auth":
                codex_auth.mark_expired()
            if self.error_kind == "claude_auth":
                claude_auth.mark_expired()
            print(f"[오류] {e}")
            real.write(traceback.format_exc())
        finally:
            sys.stdout = real
            self.running, self.ended = False, datetime.now()

    def state(self):
        return {"name": self.name, "uid": self.uid, "running": self.running, "error": self.error,
                "error_kind": self.error_kind, "log": self.log[-300:],
                "started": self.started.strftime("%H:%M:%S") if self.started else None,
                "ended": self.ended.strftime("%H:%M:%S") if self.ended else None}


JOB = Job()


def _mtime(p):
    return datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")


def brief(d):
    out = {k: d.get(k) for k in ("id", "title", "genre", "status", "created", "exported")}
    try:
        paras = config.load(d["id"], "01_원문", "paragraphs.json") or []
        det = config.load(d["id"], "02_탐지", "detect.json")
        rw = config.load(d["id"], "03_윤문", "rewrite.json")
        out["chars"] = sum(len(p) for p in paras)
        out["todo"] = batch.todo(d["id"]) if paras else []
        out["parts"] = batch.part_status(d["id"]) if paras else []
        if det:
            out["failed"] = sum(bool(c.get("failed")) for c in det["chunks"]) + sum(bool(n.get("failed") and ("edits" not in rw or n.get("fmt"))) for n in (rw or {}).get("notes", []))
        if det:
            s = audit.summary(paras, det, rw, config.load(d["id"], "04_검토", "decisions.json") or {}, d.get("target"))
            out.update(found=s["found"], grade=s.get("grade"))
    except Exception:
        pass
    return out


def app_state():
    return {"workspace": str(config.WS), "docs": [brief(d) for d in config.docs()], "backups": config.backups(),
            "genres": config.GENRES, "job": JOB.state(), "codex": codex_auth.state(), "claude": claude_auth.state(),
            "settings": config.settings(), "engines": config.ENGINES, "batch": batch.state(), "chunk": humanize.CHUNK}


def doc_state(did):
    d = config.doc(did)
    paras = config.load(did, "01_원문", "paragraphs.json") or []
    det = config.load(did, "02_탐지", "detect.json")
    rw = config.load(did, "03_윤문", "rewrite.json")
    dec = config.load(did, "04_검토", "decisions.json") or {}
    if rw and "edits" in rw:  # 수정 항목 방식: 문단 diff 대신 항목마다 기본 체크
        for e in rw["edits"]:
            e["on_default"] = audit.edit_default(e)
    elif rw:
        for p in rw["paras"]:
            p["diff"] = audit.diff_html(p["orig"], p["text"])
            p["adopt_default"] = audit.default_adopt(p)
    out_dir = config.doc_dir(did) / "05_반영본"
    files = [{"name": f.name, "path": f"05_반영본/{f.name}", "time": _mtime(f)}
             for f in sorted(out_dir.glob("*")) if f.is_file()] if out_dir.exists() else []
    return {"doc": d, "paras": paras, "detect": det, "rewrite": rw, "decisions": dec,
            "summary": audit.summary(paras, det, rw, dec, d.get("target")) if det else None,
            "codes": taxonomy.codes(), "groups": taxonomy.GROUPS, "files": files, "dir": str(config.doc_dir(did)),
            "pages": config.pages(did) if paras else [], "parts": humanize.parts(paras) if paras else [],
            "job": JOB.state()}


# ---------- 동작 ----------
def new_doc(filename, data, ctype):
    if "json" in ctype:
        body = json.loads(data)
        text = body.get("text", "")
        paras = extract.split_text(text)
        title = (body.get("title") or "").strip() or (paras[0][:24] if paras else "붙여넣은 글")
        src_name, raw = "붙여넣기.txt", text.encode("utf-8")
    else:
        ext = Path(filename).suffix.lower()
        if ext not in extract.SUPPORTED + (".hwp", ".hwpx"):
            raise ValueError(f"이 형식은 아직 못 읽습니다: {ext} (txt · md · docx · pdf 또는 붙여넣기)")
        title, src_name, raw, paras = Path(filename).stem, Path(filename).name, data, None
    if paras is None:
        tmp = config.WS / f"_upload{Path(filename).suffix.lower()}"
        config.WS.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(raw)
        try:
            paras = extract.paragraphs(tmp)
        finally:
            tmp.unlink(missing_ok=True)
    if not paras:
        raise ValueError("글에서 문장을 찾지 못했습니다. (스캔 PDF라면 글자를 고를 수 있는 PDF나 워드로 올려 주세요)")
    d = config.new_doc(title, genre="단행본 원고", status="새 글", source=src_name)
    (config.stage(d["id"], "01_원문") / src_name).write_bytes(raw)
    config.save(d["id"], "01_원문", "paragraphs.json", paras)
    print(f"[글 넣기] {d['id']} {title} — 문단 {len(paras)}개 · {sum(map(len, paras))}자")
    return d


def doc_meta(did, body):
    d = config.doc(did)
    if body.get("title", "").strip():
        d["title"] = body["title"].strip()
    if body.get("genre") in config.GENRES:
        d["genre"] = body["genre"]
    if "voice" in body:
        d["voice"] = str(body["voice"]).strip()
    if "target" in body:
        t = body["target"] or {}
        n = int(t.get("chars") or 0)
        d["target"] = {"chars": n, "no_space": bool(t.get("no_space")), "tol": 0.02 if t.get("strict") else 0.05} if n > 0 else None
    return config.save_doc(d)


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _q(self):
        u = urllib.parse.urlparse(self.path)
        return urllib.parse.unquote(u.path), {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

    def _file(self, f, dl):
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if f.suffix in (".md", ".csv", ".txt", ".json"):
            ctype = "text/plain; charset=utf-8"
        extra = {"Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(f.name)} if dl else {}
        return self._send(200, f.read_bytes(), ctype, extra)

    def do_GET(self):
        path, q = self._q()
        try:
            if path in ("/", "/index.html"):
                return self._send(200, CONSOLE.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send(200, app_state())
            if path == "/api/doc":
                return self._send(200, doc_state(q["id"]))
            if path.startswith("/files/"):
                _, _, did, rel = path.split("/", 3)
                base = config.doc_dir(did).resolve()
                f = (base / rel).resolve()
                if base in f.parents and f.is_file():
                    return self._file(f, q.get("dl") or f.suffix in (".xlsx", ".docx"))
            self._send(404, "not found", "text/plain; charset=utf-8")
        except SystemExit as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        path, q = self._q()
        did = q.get("id")
        body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        js = lambda: json.loads(body) if body else {}
        busy = {"ok": False, "error": "다른 작업이 실행 중입니다. 끝난 뒤 다시 누르세요."}

        def job(name, fn):
            ok = JOB.start(name, did, fn)
            return self._send(200 if ok else 409, {"ok": True} if ok else busy)

        try:
            if path in ("/api/archive", "/api/restore") and JOB.running:
                return self._send(409, busy)
            if path == "/api/new":
                fname = urllib.parse.unquote(self.headers.get("X-Filename", "upload.txt"))
                return self._send(200, {"ok": True, "doc": new_doc(fname, body, self.headers.get("Content-Type", ""))})
            if path == "/api/doc/meta":
                return self._send(200, {"ok": True, "doc": doc_meta(did, js())})
            if path == "/api/detect":
                return job("AI 흔적 진단", lambda: humanize.detect(did))
            if path == "/api/rewrite":
                return job("윤문", lambda: humanize.rewrite(did, do_audit=js().get("audit", True)))
            if path == "/api/decide":
                d = config.doc(did)
                dec = js()["decisions"]
                config.save(did, "04_검토", "decisions.json", dec)
                s = audit.summary(config.load(did, "01_원문", "paragraphs.json"), config.load(did, "02_탐지", "detect.json"),
                                  config.load(did, "03_윤문", "rewrite.json"), dec, d.get("target"))
                return self._send(200, {"ok": True, "summary": s})
            if path == "/api/import-review":
                fname = urllib.parse.unquote(self.headers.get("X-Filename", "체크본.json"))
                n, miss = export.import_review(did, body, fname)
                d = config.doc(did)
                dec = config.load(did, "04_검토", "decisions.json") or {}
                s = audit.summary(config.load(did, "01_원문", "paragraphs.json"), config.load(did, "02_탐지", "detect.json"),
                                  config.load(did, "03_윤문", "rewrite.json"), dec, d.get("target"))
                return self._send(200, {"ok": True, "n": n, "miss": miss, "decisions": dec, "summary": s})
            if path == "/api/export":
                return self._send(200, {"ok": True, **export.run(did, js().get("decisions"))})
            if path == "/api/second":  # 2차 윤문: 지금 윤문본(체크 반영)을 새 글로
                d = config.doc(did)
                dec = js().get("decisions") or config.load(did, "04_검토", "decisions.json") or {}
                fin = [t for _, t, _ in audit.final_paras(config.load(did, "03_윤문", "rewrite.json"), dec) if t]
                nd = new_doc("", json.dumps({"title": f"{d['title']} (2차)", "text": "\n\n".join(fin)}).encode("utf-8"), "json")
                return self._send(200, {"ok": True, "doc": doc_meta(nd["id"], {k: d.get(k) for k in ("genre", "voice", "target")})})
            if path == "/api/open":
                p = config.doc_dir(did) / q.get("stage", "")
                os.startfile(str(p if p.exists() else config.doc_dir(did)))
                return self._send(200, {"ok": True})
            if path == "/api/archive":
                return self._send(200, {"ok": True, "backup": config.archive(did).name})
            if path == "/api/restore":
                return self._send(200, {"ok": True, "doc": config.restore(js()["name"])})
            if path == "/api/batch":
                b = js()
                have = {d["id"] for d in config.docs()}
                items = [(it["id"], sorted(it["parts"]) if it.get("parts") is not None else None)
                         for it in b.get("items", []) if it.get("id") in have]
                if not items:
                    raise ValueError("진행할 글을 체크하세요.")
                steps = [s for s in b.get("steps") or ["detect", "rewrite", "audit"] if s in batch.STEPS]
                ok = JOB.start(" → ".join(batch.STEPS[s] for s in steps), None, lambda: batch.run(items, steps))
                return self._send(200 if ok else 409, {"ok": True} if ok else busy)
            if path == "/api/stop":
                llm.CANCEL.set()
                return self._send(200, {"ok": True})
            if path == "/api/settings":
                return self._send(200, {"ok": True, "settings": config.save_settings(js())})
            if path == "/api/claude/refresh":
                claude_auth.check_async(deep=True)
                return self._send(200, {"ok": True})
            if path == "/api/claude/login":
                claude_auth.login()
                return self._send(200, {"ok": True})
            if path == "/api/claude/logout":
                return self._send(200, {"ok": True, "out": claude_auth.logout()})
            if path == "/api/codex/refresh":
                codex_auth.check_async(deep=True)
                return self._send(200, {"ok": True})
            if path == "/api/codex/login":
                codex_auth.login(relogin=bool(js().get("relogin")))
                return self._send(200, {"ok": True})
            if path == "/api/codex/logout":
                return self._send(200, {"ok": True, "out": codex_auth.logout()})
            self._send(404, {"error": "not found"})
        except (ValueError, KeyError, SystemExit) as e:
            self._send(400, {"ok": False, "error": str(e)})
        except PermissionError as e:
            self._send(409, {"ok": False, "error": f"파일이 다른 프로그램(워드·엑셀)에서 열려 있습니다. 닫고 다시 누르세요: {e.filename}"})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def log_message(self, *a):
        pass


class Server6(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def serve(port=PORT, open_browser=True):
    url = f"http://localhost:{port}/"
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    except OSError:
        print(f"[server] 이미 실행 중인 것 같습니다 → {url}")
        if open_browser:
            webbrowser.open(url)
        return
    try:  # 'localhost' 가 IPv6(::1)로 풀리는 PC를 위해 ::1 에도 연다
        httpd6 = Server6(("::1", port), H)
        threading.Thread(target=httpd6.serve_forever, daemon=True).start()
    except OSError:
        pass
    config.WS.mkdir(parents=True, exist_ok=True)
    codex_auth.check_async(deep=False)
    claude_auth.check_async(deep=False)
    print(f"[한국어 AI 윤문] {url}")
    print(f"[한국어 AI 윤문] 워크스페이스: {config.WS}")
    print(f"브라우저가 안 열리면 주소창에 {url} 을 직접 입력하세요. (127.0.0.1 말고 localhost)")
    print("이 창을 닫으면 콘솔도 멈춥니다. 최소화해 두세요. (종료: Ctrl+C)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else PORT)
