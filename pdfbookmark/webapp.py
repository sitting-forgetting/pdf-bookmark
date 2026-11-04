"""本地网页界面：从访达把 PDF 拖进浏览器，识别 → 校对 → 生成。

只用标准库：http.server + 一个内联的 HTML 页面。所有文件都在本机，不上传外网。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
OUT_DIR = ROOT / "out" / "web"
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "pdfbookmark_uploads")

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


# ---------------------------------------------------------------- 工具

def _new_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(os.urandom(8)).hexdigest()[:6]


def parse_multipart(body: bytes, boundary: bytes) -> dict[str, tuple[str | None, bytes]]:
    """极简 multipart/form-data 解析，够用即可（浏览器只传一个文件字段）。"""
    delim = b"--" + boundary
    out: dict[str, tuple[str | None, bytes]] = {}
    for part in body.split(delim):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        part = part.lstrip(b"\r\n")
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        name, filename = None, None
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-disposition"):
                s = line.decode("utf-8", "replace")
                m = re.search(r'name="([^"]*)"', s)
                name = m.group(1) if m else None
                m = re.search(r'filename="([^"]*)"', s)
                filename = m.group(1) if m else None
        if name:
            out[name] = (filename, data)
    return out


def save_upload(data: bytes, filename: str) -> str:
    """按内容哈希落盘：同一个文件重复拖进来能命中页码扫描缓存。"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    digest = hashlib.sha1(data).hexdigest()[:16]
    safe = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", Path(filename or "book.pdf").name)
    path = os.path.join(UPLOAD_DIR, f"{digest}_{safe}")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return path


def _json(handler: BaseHTTPRequestHandler, obj: dict, status: int = 200) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


# ---------------------------------------------------------------- 路由

class Handler(BaseHTTPRequestHandler):
    server_version = "pdfbookmark/1.0"

    def log_message(self, fmt, *args):        # 安静一点
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    # ---------- GET ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            return self._serve_index()
        if parsed.path == "/api/ping":
            from .ocr import backend_name
            return _json(self, {"ok": True, "ocr": backend_name(),
                                "jobs": len(_JOBS)})
        if parsed.path == "/api/download":
            qs = urllib.parse.parse_qs(parsed.query)
            job_id = (qs.get("id") or [""])[0]
            return self._serve_download(job_id)
        self.send_error(404)

    def _serve_index(self):
        path = WEB_DIR / "index.html"
        if not path.exists():
            body = ("<meta charset='utf-8'><h2>界面文件还没生成</h2>"
                    "<p>缺少 web/index.html</p>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_download(self, job_id: str):
        with _LOCK:
            job = _JOBS.get(job_id)
        if not job or not job.get("out_pdf") or not os.path.exists(job["out_pdf"]):
            self.send_error(404, "no such job")
            return
        path = job["out_pdf"]
        size = os.path.getsize(path)
        name = os.path.basename(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(size))
        quoted = urllib.parse.quote(name)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # ---------- POST ----------
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/analyze":
                return self._api_analyze()
            if parsed.path == "/api/build":
                return self._api_build()
            self.send_error(404)
        except Exception as e:                     # 兜底，别让服务挂掉
            import traceback
            traceback.print_exc()
            _json(self, {"ok": False, "error": f"服务器出错：{type(e).__name__}: {e}"})

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        chunks = []
        remain = length
        while remain > 0:
            chunk = self.rfile.read(min(remain, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remain -= len(chunk)
        return b"".join(chunks)

    def _api_analyze(self):
        from .pipeline import BookError, SkipBook, analyze

        ctype = self.headers.get("Content-Type", "")
        m = re.search(r'boundary="?([^";]+)"?', ctype)
        if not m:
            return _json(self, {"ok": False, "error": "请求格式不对（缺少 multipart 边界）"})
        body = self._read_body()
        fields = parse_multipart(body, m.group(1).encode())
        if "file" not in fields:
            return _json(self, {"ok": False, "error": "没有收到文件"})
        filename, data = fields["file"]
        if not data:
            return _json(self, {"ok": False, "error": "文件是空的"})
        if not (filename or "").lower().endswith(".pdf") and not data[:5] == b"%PDF-":
            return _json(self, {"ok": False, "error": "这不是 PDF 文件"})

        path = save_upload(data, filename or "book.pdf")
        t0 = time.time()
        try:
            an = analyze(path, workers=6)
        except SkipBook as e:
            # 已有书签：网页端允许继续，用 force 重来
            try:
                an = analyze(path, workers=6, force=True)
            except Exception as e2:
                return _json(self, {"ok": False, "error": f"{e2}"})
        except BookError as e:
            return _json(self, {"ok": False, "error": str(e)})

        job_id = _new_job_id()
        with _LOCK:
            _JOBS[job_id] = {"path": path, "name": filename or Path(path).name,
                             "out_pdf": None, "items": an.items}
        sys.stderr.write(f"  ✔ {filename}：{len(an.items)} 条，"
                         f"{time.time() - t0:.1f}s\n")
        return _json(self, {
            "ok": True,
            "job": {
                "id": job_id,
                "name": filename or Path(path).name,
                "pages": an.pages,
                "kind": an.kind,
                "kind_cn": an.kind_cn,
                "toc_pages": an.toc_pages,
                "offset": an.offset,
                "elapsed": round(an.elapsed, 1),
                "items": an.items,
                "checks": an.checks,
            },
        })

    def _api_build(self):
        from . import build

        body = self._read_body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return _json(self, {"ok": False, "error": "请求体不是合法 JSON"})
        job_id = payload.get("id", "")
        items = payload.get("items") or []
        with _LOCK:
            job = _JOBS.get(job_id)
        if not job:
            return _json(self, {"ok": False, "error": "找不到这个任务，请重新分析一次"})
        if not items:
            return _json(self, {"ok": False, "error": "目录是空的，没有可写入的书签"})

        clean = []
        for it in items:
            try:
                lvl = int(it.get("level", 1))
                page = int(it.get("page", 0))
            except (TypeError, ValueError):
                continue
            title = str(it.get("title", "")).strip()
            if not title or not (1 <= page):
                continue
            clean.append({"level": max(1, min(lvl, 6)), "title": title, "page": page})
        if not clean:
            return _json(self, {"ok": False, "error": "没有有效的目录条目"})

        os.makedirs(OUT_DIR, exist_ok=True)
        stem = Path(job["name"]).stem
        out_pdf = os.path.join(OUT_DIR, f"{stem}.bookmarked.pdf")
        try:
            build.write_pdf(job["path"], out_pdf, build.to_toc(clean))
        except Exception as e:
            return _json(self, {"ok": False, "error": f"写入 PDF 失败：{e}"})

        with _LOCK:
            _JOBS[job_id]["out_pdf"] = out_pdf
        return _json(self, {
            "ok": True,
            "out_name": os.path.basename(out_pdf),
            "download_url": f"/api/download?id={job_id}",
            "count": len(clean),
            "out_path": out_pdf,
        })


# ---------------------------------------------------------------- 启动

def serve(port: int = 8765, open_browser: bool = True, host: str = "127.0.0.1") -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    httpd = None
    for candidate in range(port, port + 12):
        try:
            httpd = ThreadingHTTPServer((host, candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if httpd is None:
        print("❌ 找不到可用端口")
        return 1

    url = f"http://{host}:{port}/"
    print("=" * 52)
    print("  PDF 自动编目 · 本地界面")
    print(f"  地址：{url}")
    print("  把 PDF 从访达拖进页面即可；关闭窗口后按 Ctrl-C 停止")
    print("=" * 52)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0
