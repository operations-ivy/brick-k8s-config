#!/usr/bin/env python3
"""Hands out the 480p backlog to convert-worker.sh on other nodes (media/convert-workers.yaml).

Runs on brick2000 beside the files. Standard library only.

    POST /claim        the next video to convert: 200 with a claim id, or 204 when none
    GET  /file/<id>    the claimed original
    PUT  /done/<id>    the finished 480p copy: written as a dotted .part, then renamed
    POST /fail/<id>    ffmpeg couldn't convert it: a .failed marker, like convert-480p.sh's
    GET  /health

A claim is a file in $DST/.claims named by the sha1 of the video's path under
$SRC; convert-480p.sh skips claimed videos too, so no video is converted twice.
Workers take the backlog from the end, the CronJob from the start. A claim
older than CLAIM_HOURS is a worker that died: the video is handed out again.
"""

import hashlib
import os
import re
import shutil
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SRC = Path(os.environ.get("SRC", "/movies/library"))
DST = Path(os.environ.get("DST", "/movies/480p"))
SETTLE = int(os.environ.get("SETTLE", "600"))
CLAIM_HOURS = float(os.environ.get("CLAIM_HOURS", "6"))
EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".wmv"}
CHUNK = 1 << 20
CLAIM_ID = re.compile(r"[0-9a-f]{40}")


def claim_id(rel: str) -> str:
    return hashlib.sha1(rel.encode()).hexdigest()


def output(rel: str) -> Path:
    return DST / Path(rel).with_suffix(".mp4")


def claimed(rel: str, now: float) -> bool:
    claim = DST / ".claims" / claim_id(rel)
    return claim.exists() and now - claim.stat().st_mtime < CLAIM_HOURS * 3600


def pending(now: float) -> list[str]:
    """Videos with no 480p copy, no .failed marker, no claim and not being
    converted by the CronJob (its dotted .part), newest-name first."""
    todo = []
    for path in SRC.rglob("*"):
        if path.name.startswith(".") or path.suffix.lower() not in EXTS or not path.is_file():
            continue
        rel = str(path.relative_to(SRC))
        out = output(rel)
        if (out.exists() or out.with_name(out.name + ".failed").exists()
                or out.with_name(f".{out.name}.part").exists() or claimed(rel, now)
                or now - path.stat().st_mtime < SETTLE):
            continue
        todo.append(rel)
    return sorted(todo, reverse=True)


def claim(now: float) -> str | None:
    """Claim the next video; its id, or None. O_EXCL: two workers can't take the same one."""
    (DST / ".claims").mkdir(parents=True, exist_ok=True)
    for rel in pending(now):
        path = DST / ".claims" / claim_id(rel)
        # pending() leaves out live claims, so a claim file here is a stale one.
        path.unlink(missing_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as f:
            f.write(rel)
        return path.name
    return None


def claimed_rel(cid: str) -> str | None:
    path = DST / ".claims" / cid
    if not CLAIM_ID.fullmatch(cid) or not path.is_file():
        return None
    return path.read_text()


def release(cid: str) -> None:
    (DST / ".claims" / cid).unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def reply(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def route(self) -> tuple[str, str]:
        parts = self.path.strip("/").split("/")
        return parts[0], parts[1] if len(parts) > 1 else ""

    def do_GET(self) -> None:
        what, cid = self.route()
        if what == "health":
            self.reply(200, b"ok")
            return
        rel = claimed_rel(cid) if what == "file" else None
        src = SRC / rel if rel else None
        if not src or not src.is_file():  # unknown claim, or the original was removed
            self.reply(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(src.stat().st_size))
        self.end_headers()
        with src.open("rb") as f:
            shutil.copyfileobj(f, self.wfile, CHUNK)

    def do_POST(self) -> None:
        what, cid = self.route()
        if what == "claim":
            got = claim(time.time())
            if got:
                self.reply(200, got.encode())
            else:
                self.reply(204)
            return
        rel = claimed_rel(cid) if what == "fail" else None
        if not rel:
            self.reply(404)
            return
        out = output(rel)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.with_name(out.name + ".failed").touch()
        release(cid)
        print(f"failed: {rel}", flush=True)
        self.reply(200)

    def do_PUT(self) -> None:
        what, cid = self.route()
        rel = claimed_rel(cid) if what == "done" else None
        length = int(self.headers.get("Content-Length", "0"))
        if not rel:
            self.reply(404)
            return
        if length <= 0:
            self.reply(400)
            return
        out = output(rel)
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_name(f".{out.name}.remote.part")
        left = length
        with part.open("wb") as f:
            while left:
                block = self.rfile.read(min(CHUNK, left))
                if not block:
                    break
                f.write(block)
                left -= len(block)
        if left:  # the upload stopped short
            part.unlink(missing_ok=True)
            self.reply(400)
            return
        part.rename(out)
        release(cid)
        print(f"done: {rel} ({length >> 20} MB)", flush=True)
        self.reply(200)


def main() -> None:
    server = ThreadingHTTPServer(("", int(os.environ.get("PORT", "8080"))), Handler)
    print(f"serving {SRC} -> {DST}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
