#!/usr/bin/env python3
"""
Touch-friendly GUI for sdmult. Serves a small web page from the Pi.

    sudo ./sdmult_gui.py                      # open the printed URL on the Pi
    sudo ./sdmult_gui.py --host 0.0.0.0       # also reachable from a phone/laptop on the LAN

Every action goes through the same safety checks as the command line tool.
Requests must carry the random access key printed at startup, so other
devices on the network cannot start a job without it.
"""

import argparse
import json
import os
import re
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import sdmult as S

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "sdmult_gui.html")


class Job:
    """One read / write / clone run. Fields are read by the HTTP thread."""

    def __init__(self, mode, source, targets, image):
        self.mode, self.source, self.targets, self.image = mode, source, targets, image
        self.phase = "starting"
        self.message = ""
        self.error = None
        self.read_prog = None
        self.progs = {}
        self.started = time.time()
        self.finished = None

    def card_state(self, path):
        if self.read_prog is not None and path == self.source:
            p = self.read_prog
        else:
            p = self.progs.get(path)
        if p is None:
            return {"phase": "queued", "pct": 0, "rate": 0, "error": None}
        el = max(time.time() - p.phase_started, 1e-6)
        return {"phase": p.phase,
                "pct": round(100.0 * p.done / p.total, 1) if p.total else 100.0,
                "rate": round(p.done / el / 1e6, 1) if p.phase not in ("done", "failed") else 0,
                "error": p.error}

    def to_json(self):
        cards = {t: self.card_state(t) for t in self.targets}
        if self.source:
            cards[self.source] = self.card_state(self.source)
        return {"mode": self.mode, "phase": self.phase, "message": self.message,
                "error": self.error, "source": self.source, "targets": self.targets,
                "image": os.path.basename(self.image) if self.image else None,
                "cards": cards, "elapsed": round((self.finished or time.time()) - self.started)}


class App:
    def __init__(self, image_dir, allow_fixed=False):
        self.image_dir = image_dir
        self.allow_fixed = allow_fixed
        self.job = None
        self.lock = threading.Lock()
        os.makedirs(image_dir, exist_ok=True)

    # ---- state -------------------------------------------------------
    def images(self):
        out = []
        for n in sorted(os.listdir(self.image_dir)):
            p = os.path.join(self.image_dir, n)
            if n.endswith(".img") and os.path.isfile(p) and not n.startswith("_"):
                st = os.stat(p)
                out.append({"name": n, "size": st.st_size, "mtime": int(st.st_mtime)})
        return out

    def state(self):
        disks = []
        for d in S.get_disks():
            usable = S.is_candidate_card(d) or (self.allow_fixed and not d["protected"]
                                                and not d["readonly"] and d["size"] > 0)
            if not (usable or d["protected"]):
                continue                     # hide fixed disks unless --allow-fixed
            disks.append({"path": d["path"], "name": d["name"], "size": d["size"],
                          "model": d["model"], "bus": d["tran"],
                          "system": d["protected"], "mounted": bool(d["mounts"])})
        st = os.statvfs(self.image_dir)
        return {"disks": disks, "images": self.images(),
                "free": st.f_bavail * st.f_frsize,
                "job": self.job.to_json() if self.job else None,
                "busy": self.busy()}

    def busy(self):
        return self.job is not None and self.job.finished is None

    # ---- starting jobs (all validation happens here, before any thread) ----
    def start(self, req):
        mode = req.get("mode")
        verify = bool(req.get("verify", True))
        with self.lock:
            if self.busy():
                raise S.SafetyError("A job is already running.")
            disks = S.get_disks()
            if mode == "write":
                image = self._image_path(req.get("image"))
                length = os.path.getsize(image)
                S.check_targets(disks, req.get("targets") or [], length, None, self.allow_fixed)
                job = Job("write", None, list(req["targets"]), image)
                run = lambda: self._run_write(job, image, verify)
            elif mode in ("clone", "read"):
                disks, src, length = S.plan_read(req.get("source") or "", False)
                if mode == "clone":
                    S.check_targets(disks, req.get("targets") or [], length, src["path"],
                                    self.allow_fixed)
                    image = os.path.join(self.image_dir, f"_clone-{int(time.time())}.img")
                    targets = list(req["targets"])
                else:
                    name = re.sub(r"[^A-Za-z0-9._-]+", "-", (req.get("name") or "").strip())
                    name = name.strip(".-") or time.strftime("master-%Y%m%d-%H%M")
                    image = os.path.join(self.image_dir, name.removesuffix(".img") + ".img")
                    if os.path.exists(image):
                        raise S.SafetyError(f"An image called {os.path.basename(image)} "
                                            "already exists. Pick another name.")
                    targets = []
                st = os.statvfs(self.image_dir)
                if st.f_bavail * st.f_frsize < length + 64 * 1024 * 1024:
                    raise S.SafetyError(f"Not enough free space for a {S.human(length)} image.")
                job = Job(mode, src["path"], targets, image)
                run = lambda: self._run_read_then_write(job, src, length, verify)
            else:
                raise S.SafetyError("Unknown mode.")
            self.job = job
        threading.Thread(target=self._guard, args=(job, run), daemon=True).start()
        return job.to_json()

    def _image_path(self, name):
        if not name or "/" in name or name.startswith((".", "_")):
            raise S.SafetyError("Pick an image.")
        p = os.path.join(self.image_dir, name)
        if not os.path.isfile(p):
            raise S.SafetyError(f"Image {name} not found.")
        return p

    # ---- job bodies ----------------------------------------------------
    def _guard(self, job, run):
        try:
            run()
        except Exception as e:
            job.error = str(e)
            job.phase = "failed"
            traceback.print_exc()
        finally:
            job.finished = time.time()

    def _run_read_then_write(self, job, src, length, verify):
        job.phase = "reading"
        job.message = f"Reading master card ({S.human(length)})"
        p = S.Progress(src["name"], length)
        p.set_phase("reading")
        job.read_prog = p
        try:
            S.read_device_to_image(src["path"], job.image, length, p)
            p.phase, p.done = "done", length
        except Exception as e:
            p.error, p.phase = str(e), "failed"
            raise
        if job.mode == "read":
            job.phase = "done"
            job.message = f"Saved {os.path.basename(job.image)} ({S.human(length)})"
            return
        try:
            self._run_write(job, job.image, verify, source=src["path"])
        finally:
            for x in (job.image, job.image + ".sha256", job.image + ".partial"):
                try:
                    os.remove(x)
                except OSError:
                    pass

    def _run_write(self, job, image, verify, source=None):
        length = os.path.getsize(image)
        # re-check right before writing: cards may have been swapped meanwhile
        disks = S.get_disks()
        chosen = S.check_targets(disks, job.targets, length, source, self.allow_fixed)
        job.phase = "hashing"
        job.message = "Checking the image"
        digest = S.image_digest(image, length)
        for d in chosen:
            S.unmount_disk(d)
        job.phase = "writing"
        job.message = f"Writing {len(chosen)} cards"
        flush = lambda t: S.subprocess.run(["blockdev", "--flushbufs", t], capture_output=True)
        results, progs = S.duplicate(image, job.targets, digest, verify=verify,
                                     after_write=flush, out=None, progs=job.progs)
        for d in chosen:
            S.subprocess.run(["blockdev", "--rereadpt", d["path"]], capture_output=True)
        ok = sum(results.values())
        bad = len(results) - ok
        job.phase = "done" if not bad else "failed"
        job.message = (f"{ok} of {len(results)} cards " + ("written and verified" if verify
                       else "written (not verified)") + (f", {bad} failed" if bad else ""))
        if bad:
            job.error = f"{bad} card(s) failed"

    def dismiss(self):
        with self.lock:
            if self.busy():
                raise S.SafetyError("Job still running.")
            self.job = None


def make_handler(app, key):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authed(self):
            q = parse_qs(urlparse(self.path).query)
            given = self.headers.get("X-Key") or (q.get("key") or [""])[0]
            return secrets.compare_digest(given, key)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                with open(PAGE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if not self._authed():
                return self._send(403, {"error": "Wrong or missing access key."})
            if path == "/api/state":
                return self._send(200, app.state())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                return self._send(403, {"error": "Wrong or missing access key."})
            path = urlparse(self.path).path
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                if path == "/api/start":
                    return self._send(200, app.start(req))
                if path == "/api/dismiss":
                    app.dismiss()
                    return self._send(200, {"ok": True})
                self._send(404, {"error": "not found"})
            except S.SafetyError as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                self._send(500, {"error": str(e)})
    return H


def main(argv=None):
    ap = argparse.ArgumentParser(description="Web GUI for sdmult")
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on (0.0.0.0 = whole network)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--images", default="/var/lib/sdmult",
                    help="folder for saved master images")
    ap.add_argument("--key", default=os.environ.get("SDMULT_KEY"),
                    help="access key (default: $SDMULT_KEY, else random each start)")
    ap.add_argument("--allow-fixed", action="store_true",
                    help="offer non-removable disks too (testing only)")
    args = ap.parse_args(argv)
    if os.geteuid() != 0:
        print("Run with sudo (raw disk access needs root).")
        return 1
    key = args.key or secrets.token_urlsafe(9)
    app = App(args.images, args.allow_fixed)
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(app, key))
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    if os.environ.get("SDMULT_KEY") and key == os.environ["SDMULT_KEY"]:
        print(f"SD Duplicator running on http://{shown}:{args.port}/ (key from SDMULT_KEY)")
    else:
        print(f"SD Duplicator running. Open:  http://{shown}:{args.port}/#{key}")
    if args.host == "0.0.0.0":
        print("From another device use the Pi's IP address with the same #key ending.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
