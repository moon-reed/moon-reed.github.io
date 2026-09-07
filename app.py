#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
徽标设计匿名投票系统
- 纯 Python 标准库，零 pip 依赖
- 支持上传徽标设计图（前端 canvas 压缩后以 base64 JSON 上传，避免 multipart 解析）
- 匿名多选投票：每台设备最多选 N 个（默认 Top 3），同一设备可随时修改投票
- 结果默认「投票后可见」（公平起见），管理员始终可见并管理

运行：python app.py   然后浏览器打开 http://localhost:8001
"""
import base64
import json
import mimetypes
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DESIGNS_FILE = os.path.join(DATA_DIR, "designs.json")
VOTES_FILE = os.path.join(DATA_DIR, "votes.json")
INDEX_FILE = os.path.join(BASE_DIR, "index.html")

PORT = 8001
MAX_CHOICES = 3                 # 每人最多可选数量（Top 3）
SHOW_RESULTS_ALWAYS = False     # True=实时显示结果；False=投票后可见（更公平）
ADMIN_PASSWORD = "admin"        # 管理员密码（用于删除/清空/重置）
MAX_IMAGE_BYTES = 6 * 1024 * 1024  # base64 解码后图片上限 6MB

_lock = threading.Lock()
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_designs():
    return load_json(DESIGNS_FILE, [])


def load_votes():
    return load_json(VOTES_FILE, {})


# ---------------- 业务逻辑 ----------------

def handle_upload(data):
    name = (data.get("name") or "").strip()[:100]
    image_b64 = data.get("image") or ""
    mime = data.get("mime") or "image/png"
    if "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(image_b64, validate=True)
    except Exception:
        return 400, {"error": "图片数据无效"}
    if not raw:
        return 400, {"error": "图片为空"}
    if len(raw) > MAX_IMAGE_BYTES:
        return 400, {"error": "图片过大（>6MB）"}
    if mime not in MIME_EXT:
        return 400, {"error": "不支持的图片格式（请用 PNG/JPG/WebP）"}

    filename = uuid.uuid4().hex + MIME_EXT[mime]
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(os.path.join(UPLOAD_DIR, filename), "wb") as f:
        f.write(raw)

    design = {
        "id": uuid.uuid4().hex,
        "name": name or "未命名设计",
        "file": filename,
        "mime": mime,
        "created": int(time.time()),
    }
    designs = load_designs()
    designs.append(design)
    save_json(DESIGNS_FILE, designs)
    return 200, {"ok": True, "design": design}


def handle_vote(data):
    voter_id = (data.get("voter_id") or "").strip()
    choices = data.get("choices") or []
    if not voter_id or not re.fullmatch(r"[A-Za-z0-9\-]{8,64}", voter_id):
        return 400, {"error": "投票者标识无效"}

    ids = {d["id"] for d in load_designs()}
    clean = []
    for c in choices:
        if c in ids and c not in clean:
            clean.append(c)
    if not clean:
        return 400, {"error": "请至少选择一个设计"}
    if len(clean) > MAX_CHOICES:
        clean = clean[:MAX_CHOICES]

    votes = load_votes()
    votes[voter_id] = {"choices": clean, "time": int(time.time())}
    save_json(VOTES_FILE, votes)
    return 200, {"ok": True, "choices": clean}


def compute_results():
    designs = load_designs()
    votes = load_votes()
    counts = {}
    for v in votes.values():
        for cid in v.get("choices", []):
            counts[cid] = counts.get(cid, 0) + 1
    results = [
        {"id": d["id"], "name": d["name"], "file": d["file"],
         "mime": d["mime"], "votes": counts.get(d["id"], 0),
         "created": d.get("created", 0)}
        for d in designs
    ]
    results.sort(key=lambda x: (-x["votes"], x["created"]))
    return results, len(votes)


def handle_bootstrap(query):
    voter_id = query.get("voter_id", [""])[0]
    designs = load_designs()
    votes = load_votes()
    my_votes = votes.get(voter_id, {}).get("choices", [])
    has_voted = voter_id in votes
    show = SHOW_RESULTS_ALWAYS or has_voted
    if show:
        results, total = compute_results()
    else:
        results, total = None, len(votes)
    return {
        "designs": [
            {"id": d["id"], "name": d["name"], "file": d["file"], "mime": d["mime"]}
            for d in designs
        ],
        "max_choices": MAX_CHOICES,
        "has_voted": has_voted,
        "my_votes": my_votes,
        "show_results": show,
        "results": results,
        "total_voters": total if show else None,
    }


def handle_admin(action, data):
    pw = data.get("password") or ""
    if pw != ADMIN_PASSWORD:
        return 403, {"error": "管理员密码错误"}

    if action == "delete":
        did = data.get("id")
        designs = load_designs()
        target = next((d for d in designs if d["id"] == did), None)
        if not target:
            return 404, {"error": "设计不存在"}
        designs = [d for d in designs if d["id"] != did]
        save_json(DESIGNS_FILE, designs)
        try:
            os.remove(os.path.join(UPLOAD_DIR, target["file"]))
        except OSError:
            pass
        votes = load_votes()
        for v in votes.values():
            v["choices"] = [c for c in v.get("choices", []) if c != did]
        save_json(VOTES_FILE, votes)
        return 200, {"ok": True}

    if action == "reset":
        save_json(VOTES_FILE, {})
        return 200, {"ok": True}

    if action == "clear_designs":
        for d in load_designs():
            try:
                os.remove(os.path.join(UPLOAD_DIR, d["file"]))
            except OSError:
                pass
        save_json(DESIGNS_FILE, [])
        save_json(VOTES_FILE, {})
        return 200, {"ok": True}

    if action == "list":
        results, total = compute_results()
        return 200, {"ok": True, "designs": results, "total_voters": total}

    return 400, {"error": "未知操作"}


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "LogoVote/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 20 * 1024 * 1024:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_FILE, "rb") as f:
                    html = f.read()
            except OSError:
                html = b"<h1>index.html not found</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if path == "/api/bootstrap":
            self._send(200, handle_bootstrap(parse_qs(parsed.query)))
            return
        if path.startswith("/uploads/"):
            fname = os.path.basename(path[len("/uploads/"):])
            fpath = os.path.join(UPLOAD_DIR, fname)
            if not os.path.isfile(fpath):
                self._send(404, {"error": "not found"})
                return
            ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
            with open(fpath, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(content)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read_body()
        with _lock:
            if path == "/api/upload":
                code, resp = handle_upload(data)
            elif path == "/api/vote":
                code, resp = handle_vote(data)
            elif path == "/api/admin":
                code, resp = handle_admin(data.get("action"), data)
            else:
                code, resp = 404, {"error": "not found"}
        self._send(code, resp)


def main():
    print("=" * 52)
    print("  徽标设计匿名投票系统")
    print("  访问地址: http://localhost:%d" % PORT)
    print("  手机访问: http://<本机IP>:%d" % PORT)
    print("  管理员密码: %s" % ADMIN_PASSWORD)
    print("  每人最多选 %d 个，结果%s可见" % (
        MAX_CHOICES, "实时" if SHOW_RESULTS_ALWAYS else "投票后"))
    print("=" * 52)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
