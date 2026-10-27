#!/usr/bin/env python3
"""Tapedeck: save YouTube audio and video on your own computer.

Run:  python3 tapedeck.py        (opens http://127.0.0.1:8765)
Needs Python 3.9+ and ffmpeg; Deno is recommended for YouTube.
Tapedeck keeps its own copy of yt-dlp and updates it daily, so YouTube
changes don't break downloads. It listens on 127.0.0.1 only.
"""
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

VERSION = "0.2.0"
HOST = "127.0.0.1"
PORT = int(os.environ.get("TAPEDECK_PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))
if sys.platform == "darwin":
    DATA = os.path.expanduser("~/Library/Application Support/Tapedeck")
else:
    DATA = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "tapedeck")
VENV = os.path.join(DATA, "yt-dlp-env")
OWN_YTDLP = os.path.join(VENV, "bin", "yt-dlp")
UPDATE_STAMP = os.path.join(VENV, ".last-update")
UPDATE_EVERY = 24 * 3600
WORK = tempfile.mkdtemp(prefix="tapedeck-")
MAX_ITEMS = 500
PARALLEL = 3  # playlist tracks downloaded at the same time
UNAVAILABLE_TITLES = ("[Private video]", "[Deleted video]")

FORMATS = {
    "mp3": {"label": "MP3 · best quality", "kind": "audio",
            "args": ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "0"]},
    # Keeps YouTube's own audio stream (usually Opus), so nothing is lost to re-encoding.
    "original": {"label": "Original audio · no conversion", "kind": "audio",
                 "args": ["-f", "ba/b", "-x"]},
    "m4a": {"label": "M4A · for Apple Music", "kind": "audio",
            "args": ["-f", "ba[ext=m4a]/ba/b", "-x", "--audio-format", "m4a"]},
    "mp4": {"label": "Video MP4 · up to 1080p, plays anywhere", "kind": "video",
            "args": ["-f", "bv*+ba/b", "-S", "res:1080,vcodec:h264,acodec:aac",
                     "--merge-output-format", "mp4", "--embed-chapters"]},
    "best": {"label": "Video · best quality, up to 4K (MKV)", "kind": "video",
             "args": ["-f", "bv*+ba/b", "-S", "res:2160",
                      "--merge-output-format", "mkv", "--embed-chapters"]},
}
SPONSORBLOCK = ["--sponsorblock-remove", "sponsor,selfpromo,interaction,music_offtopic"]
mimetypes.add_type("audio/ogg", ".opus")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("video/x-matroska", ".mkv")

TOKEN = None
JOBS = {}
JOBS_LOCK = threading.Lock()
ENGINE = {"state": "checking", "path": None, "version": None, "error": None}
ENGINE_LOCK = threading.Lock()
ENGINE_READY = threading.Event()


# ---- yt-dlp engine: Tapedeck's own copy, updated daily ----

def set_engine(**fields):
    with ENGINE_LOCK:
        ENGINE.update(fields)
        if ENGINE["path"]:
            ENGINE_READY.set()


def ytdlp_version(path):
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=60)
        return out.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def maintain_engine():
    if not ENGINE["path"]:
        current = OWN_YTDLP if os.path.isfile(OWN_YTDLP) else shutil.which("yt-dlp")
        if current:
            set_engine(path=current, version=ytdlp_version(current))
    try:
        fresh = time.time() - os.path.getmtime(UPDATE_STAMP) < UPDATE_EVERY
    except OSError:
        fresh = False
    if fresh and os.path.isfile(OWN_YTDLP):
        set_engine(state="ready")
        return
    set_engine(state="updating")
    try:
        python = os.path.join(VENV, "bin", "python")
        if not os.path.isfile(python):
            subprocess.run([sys.executable, "-m", "venv", "--clear", VENV],
                           check=True, capture_output=True, text=True, timeout=300)
        subprocess.run([python, "-m", "pip", "install", "--quiet", "--upgrade",
                        "--disable-pip-version-check", "yt-dlp[default]"],
                       check=True, capture_output=True, text=True, timeout=900)
        with open(UPDATE_STAMP, "w"):
            pass
        set_engine(state="ready", path=OWN_YTDLP, version=ytdlp_version(OWN_YTDLP), error=None)
    except (OSError, subprocess.SubprocessError) as e:
        detail = (getattr(e, "stderr", None) or str(e)).strip().splitlines()
        set_engine(state="ready" if ENGINE["path"] else "error",
                   error="Couldn't update yt-dlp: %s" % (detail[-1][:200] if detail else "unknown error"))


def engine_loop():
    while True:
        maintain_engine()
        time.sleep(6 * 3600)


def engine_path():
    if not ENGINE_READY.wait(900):
        raise ValueError(ENGINE["error"] or "yt-dlp isn't ready yet. Check your internet connection.")
    with ENGINE_LOCK:
        return ENGINE["path"]


# ---- Links and playlists ----

def valid_url(url):
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def may_be_playlist(url):
    """Plain YouTube video links skip the (slow) inspect step; anything else gets inspected."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    if host == "youtu.be" or host.endswith("youtube.com"):
        return "list" in parse_qs(parsed.query) or parsed.path.startswith(
            ("/playlist", "/@", "/channel/", "/c/", "/user/"))
    return True


def inspect(url):
    """Return {"type": "video"} or {"type": "playlist", "title", "entries", "truncated"}."""
    if not may_be_playlist(url):
        return {"type": "video"}
    proc = subprocess.run(
        [engine_path(), "--flat-playlist", "--yes-playlist", "-J", "--color", "never",
         "--playlist-end", str(MAX_ITEMS), "--", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    if proc.returncode != 0:
        errors = [l[6:].strip() for l in proc.stderr.splitlines() if l.startswith("ERROR:")]
        raise ValueError(errors[-1] if errors else "Couldn't read that link.")
    data = json.loads(proc.stdout)
    if data.get("_type") not in ("playlist", "multi_video"):
        return {"type": "video"}
    entries = []
    for e in data.get("entries") or []:
        url = e and (e.get("url") or e.get("webpage_url"))
        # Channel pages list their tabs (Videos, Shorts…) as nested playlists; skip those.
        if not url or e.get("ie_key") == "YoutubeTab":
            continue
        if not url.startswith("http") and e.get("ie_key") == "Youtube":
            url = "https://www.youtube.com/watch?v=" + e["id"]
        title = e.get("title") or url
        entries.append({"url": url, "title": title, "duration": e.get("duration"),
                        "available": title not in UNAVAILABLE_TITLES})
    if not entries:
        raise ValueError("No videos found at that link. For a channel, open its Videos tab and paste that link.")
    return {"type": "playlist", "title": data.get("title") or "Playlist",
            "entries": entries, "truncated": len(entries) >= MAX_ITEMS}


# ---- Jobs ----

def job_set(job_id, **fields):
    with JOBS_LOCK:
        JOBS[job_id].update(fields)


def item_set(job_id, index, **fields):
    with JOBS_LOCK:
        JOBS[job_id]["active"].setdefault(index, {}).update(fields)


def download_one(job_id, index, url, out_dir, prefix, fmt, sponsorblock):
    """Download one URL in the chosen format. Returns (path, None) or (None, error)."""
    spec = FORMATS[fmt]
    streams = 2 if spec["kind"] == "video" else 1  # video = separate picture and sound downloads
    cmd = [
        engine_path(), "--no-playlist", "--newline", "--color", "never",
        "--no-simulate", "--progress", "--no-update",
        "--concurrent-fragments", "4",
        "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg",
        *spec["args"], *(SPONSORBLOCK if sponsorblock else []),
        "--progress-template",
        "download:PROGRESS %(progress.status)s %(progress.downloaded_bytes)s "
        "%(progress.total_bytes)s %(progress.total_bytes_estimate)s",
        "--print", "before_dl:TITLE %(title)s",
        "--print", "after_move:FILE %(filepath)s",
        "-o", os.path.join(out_dir, prefix + "%(title).150B.%(ext)s"),
        "--", url,
    ]
    errors, path, finished = [], None, 0
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        return None, "Couldn't start yt-dlp: %s" % e

    for line in proc.stdout:
        line = line.rstrip("\n")
        if line.startswith("TITLE "):
            item_set(job_id, index, title=line[6:])
            if JOBS[job_id]["single"]:
                job_set(job_id, title=line[6:])
        elif line.startswith("FILE "):
            path = line[5:]
        elif line.startswith("PROGRESS "):
            status, done, size, estimate = (line.split() + ["NA"] * 5)[1:5]
            if status == "finished":
                finished += 1
                if finished >= streams:
                    item_set(job_id, index, phase="converting", fraction=1)
                continue
            size = size if size != "NA" else estimate
            if done != "NA" and size not in ("NA", "0"):
                part = min(1, float(done) / float(size))
                item_set(job_id, index, phase="downloading",
                         fraction=min(1, (finished + part) / streams))
        elif line.startswith("ERROR:"):
            errors.append(line[6:].strip())
    proc.wait()

    if proc.returncode == 0 and path and os.path.isfile(path):
        return path, None
    return None, errors[-1] if errors else "yt-dlp exit code %s" % proc.returncode


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")[:150] or "playlist"


def run_job(job_id, items, playlist_title, fmt, sponsorblock):
    out_dir = os.path.join(WORK, job_id)
    total = len(items)
    width = len(str(total))

    def work(pair):
        i, item = pair
        item_dir = os.path.join(out_dir, str(i))
        os.makedirs(item_dir)
        item_set(job_id, i, title=item.get("title") or "", phase="starting", fraction=0)
        prefix = "%0*d - " % (width, i + 1) if playlist_title else ""
        try:
            path, error = download_one(job_id, i, item["url"], item_dir, prefix, fmt, sponsorblock)
        except ValueError as e:
            path, error = None, str(e)
        with JOBS_LOCK:
            job = JOBS[job_id]
            job["active"].pop(i, None)
            job["completed"] += 1
            if error:
                job["failed"].append({"title": item.get("title") or item["url"], "error": error})
        return path

    with ThreadPoolExecutor(max_workers=min(PARALLEL, total)) as pool:
        files = [p for p in pool.map(work, enumerate(items)) if p]

    if not files:
        failed = JOBS[job_id]["failed"]
        job_set(job_id, state="error", error=failed[-1]["error"] if total == 1 else
                "None of the selected tracks could be downloaded.")
    elif playlist_title is None:
        job_set(job_id, state="done", path=files[0], name=os.path.basename(files[0]))
    else:
        job_set(job_id, state="zipping")
        zip_path = os.path.join(out_dir, "playlist.zip")
        name = safe_name(playlist_title)
        # Audio and video are already compressed, so store them as-is.
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
            for f in files:
                z.write(f, os.path.join(name, os.path.basename(f)))
        job_set(job_id, state="done", path=zip_path, name=name + ".zip")


def job_status(job):
    active = [dict(a) for _, a in sorted(job["active"].items())]
    progress = job["completed"] + sum(a.get("fraction", 0) for a in active)
    return {
        "state": job["state"], "title": job["title"], "kind": FORMATS[job["format"]]["kind"],
        "total": job["total"], "completed": job["completed"],
        "percent": 100 if job["state"] == "done" else min(100, round(progress * 100 / job["total"])),
        "active": [{"title": a.get("title"), "phase": a.get("phase"),
                    "percent": round(a.get("fraction", 0) * 100)} for a in active],
        "failed": job["failed"], "error": job["error"],
    }


# ---- HTTP ----

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def host_ok(self):
        # Blocks DNS-rebinding: only answer requests addressed to this machine.
        if self.headers.get("Host") in ("%s:%d" % (HOST, PORT), "localhost:%d" % PORT):
            return True
        self.send_json(403, {"error": "Forbidden host."})
        return False

    def read_json(self):
        # Requiring JSON forces a CORS preflight, so other websites can't trigger jobs.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self.send_json(415, {"error": "Expected JSON."})
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(length, 2_000_000)))
            if isinstance(body, dict):
                return body
        except ValueError:
            pass
        self.send_json(400, {"error": "Invalid request."})
        return None

    def do_GET(self):
        if not self.host_ok():
            return
        parts = self.path.split("?")[0].strip("/").split("/")
        if parts == [""]:
            with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if parts == ["api", "status"]:
            # Other websites can't read this response (no CORS), so the token stays private.
            with ENGINE_LOCK:
                engine = {k: ENGINE[k] for k in ("state", "version", "error")}
            self.send_json(200, {"version": VERSION, "engine": engine, "token": TOKEN,
                                 "formats": [{"id": k, "label": v["label"], "kind": v["kind"]}
                                             for k, v in FORMATS.items()]})
            return
        if len(parts) in (3, 4) and parts[:2] == ["api", "jobs"]:
            with JOBS_LOCK:
                job = JOBS.get(parts[2])
                status = job_status(job) if job else None
                path, name = (job.get("path"), job.get("name")) if job else (None, None)
            if not job:
                self.send_json(404, {"error": "Unknown job."})
            elif len(parts) == 3:
                self.send_json(200, status)
            elif parts[3] == "file" and status["state"] == "done":
                self.send_file(path, name)
            else:
                self.send_json(404, {"error": "Not found."})
            return
        self.send_json(404, {"error": "Not found."})

    def send_file(self, path, name):
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        fallback = "download" + os.path.splitext(name)[1]
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header("Content-Disposition",
                         "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (fallback, quote(name)))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def do_POST(self):
        if not self.host_ok():
            return
        if self.path not in ("/api/inspect", "/api/jobs"):
            self.send_json(404, {"error": "Not found."})
            return
        body = self.read_json()
        if body is None:
            return

        if self.path == "/api/inspect":
            url = str(body.get("url", "")).strip()
            if not valid_url(url):
                self.send_json(400, {"error": "That doesn't look like a link. Paste a full https:// URL."})
                return
            try:
                self.send_json(200, inspect(url))
            except (ValueError, subprocess.TimeoutExpired) as e:
                self.send_json(422, {"error": str(e) or "Timed out reading that link."})
            return

        # Either {"url": "..."} for one video, or {"title": "...", "items": [{"url", "title"}]}.
        fmt = body.get("format", "mp3")
        if fmt not in FORMATS:
            self.send_json(400, {"error": "Unknown format."})
            return
        if "items" in body:
            items = body.get("items")
            title = str(body.get("title") or "Playlist")
        else:
            items = [{"url": str(body.get("url", "")).strip()}]
            title = None
        if (not isinstance(items, list) or not 0 < len(items) <= MAX_ITEMS
                or not all(isinstance(i, dict) and valid_url(str(i.get("url", ""))) for i in items)):
            self.send_json(400, {"error": "That doesn't look like a link. Paste a full https:// URL."
                                 if title is None else "Pick at least one track."})
            return
        job_id = uuid.uuid4().hex
        with JOBS_LOCK:
            JOBS[job_id] = {"state": "working", "title": title, "single": title is None,
                            "format": fmt, "total": len(items), "completed": 0,
                            "active": {}, "failed": [], "error": None}
        threading.Thread(target=run_job, daemon=True,
                         args=(job_id, items, title, fmt, bool(body.get("sponsorblock")))).start()
        self.send_json(202, {"id": job_id})


def load_token():
    """A secret baked into the one-click bookmark, so only it can start downloads automatically."""
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, "token")
    try:
        with open(path) as f:
            token = f.read().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(18)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(token)
    return token


def main():
    global TOKEN
    if "--version" in sys.argv:
        print("Tapedeck", VERSION)
        return
    url = "http://%s:%d" % (HOST, PORT)
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found. Install it with: brew install ffmpeg")
    if not shutil.which("deno"):
        print("Tip: install Deno (brew install deno). YouTube needs it for some videos.")
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print("Tapedeck is already running at %s" % url)
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        return
    TOKEN = load_token()
    threading.Thread(target=engine_loop, daemon=True).start()
    # Treat `kill` like Ctrl+C so the temp folder is still removed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("Tapedeck %s running at %s  (Ctrl+C to stop)" % (VERSION, url))
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
