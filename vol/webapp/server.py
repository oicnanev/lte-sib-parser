#!/usr/bin/env python3
# Local web app for sib-scan.sh: start/stop scans, follow them live, browse the
# readings database on a map. Python standard library only.
#
#   python3 /vol/webapp/server.py [--host 127.0.0.1] [--port 8080]
#
# Live updates use Server-Sent Events (/api/events). Location: gpsd when it has
# a fix, otherwise the browser's geolocation or a position set on the map; the
# effective position is written to the location file read by parse_save_sib.py.
import argparse
import json
import os
import queue
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
VOL = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(VOL, "scripts"))
import location  # noqa: E402
import readings_db  # noqa: E402

STATIC = os.path.join(HERE, "static")
SIB_SCAN = os.path.join(VOL, "sib-scan.sh")
READINGS_DB = os.path.join(VOL, "output", "readings.sqlite")
CELLS_DB = os.path.join(VOL, "output", "cells.sqlite")
BANDS_DB = os.path.join(VOL, "helpers", "lte_bands.sqlite3")
DEVICES = {"soapy", "UHD", "bladeRF", ""}
GPS_STALE_S = 5
LOG_LINES = 500

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


class Hub:
    """shared state and SSE fan-out"""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []
        self.log = []
        self.proc = None
        self.status = {"running": False, "scan_id": None, "task": None, "earfcn": None,
                       "ppm": None, "started": None, "args": None, "exit_code": None}
        self.gps = None  # last gpsd fix
        self.client_loc = None  # browser or manual
        self.location = None  # effective

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def send(self, event, data):
        msg = "event: %s\ndata: %s\n\n" % (event, json.dumps(data))
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    self.clients.remove(q)

    def add_log(self, line):
        with self.lock:
            self.log.append(line)
            del self.log[:-LOG_LINES]
        self.send("log", line)

    def set_status(self, **kw):
        with self.lock:
            self.status.update(kw)
            st = dict(self.status)
        self.send("status", st)

    # --- location ---

    def update_location(self):
        gps_fresh = self.gps and time.time() - self.gps["_t"] < GPS_STALE_S
        loc = self.gps if gps_fresh else self.client_loc
        loc = {k: v for k, v in loc.items() if not k.startswith("_")} if loc else None
        if loc != self.location:
            self.location = loc
            write_location_file(loc)
            self.send("location", {"location": loc, "gpsd": bool(gps_fresh)})


hub = Hub()


def write_location_file(loc):
    path = location.LOCATION_FILE
    try:
        if loc is None:
            if os.path.exists(path):
                os.unlink(path)
            return
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(loc, f)
        os.replace(tmp, path)
    except OSError as e:
        hub.add_log("[webapp] cannot write location file: %s" % e)


def gpsd_thread():
    """keep hub.gps up to date while gpsd is reachable; retry every 10 s otherwise"""
    while True:
        try:
            s = socket.create_connection(location.GPSD, timeout=5)
        except OSError:
            time.sleep(10)
            continue
        hub.add_log("[webapp] connected to gpsd")
        try:
            s.settimeout(10)
            s.sendall(b'?WATCH={"enable":true,"json":true}\n')
            buf = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("class") == "TPV" and msg.get("mode", 0) >= 2 and "lat" in msg:
                        acc = msg.get("eph") or max(msg.get("epx", 0), msg.get("epy", 0)) or None
                        hub.gps = {"lat": msg["lat"], "lon": msg["lon"], "accuracy": acc,
                                   "source": "gpsd", "time": msg.get("time"), "_t": time.time()}
                        hub.update_location()
        except OSError:
            pass
        finally:
            s.close()
        hub.add_log("[webapp] lost gpsd")
        hub.gps = None
        hub.update_location()
        time.sleep(5)


def gps_staleness_thread():
    """fall back to the browser/map position when gpsd stops reporting a fix"""
    while True:
        time.sleep(2)
        hub.update_location()


# --- readings database ---

SUMMARY_COLS = ("id, scan_id, time, updated, earfcn, band, dl_freq_mhz, pci, mcc, mnc, plmns, "
                "tac, eci, enb_id, cell_id, cgi, rsrp, lat, lon, accuracy_m, location_source, "
                "location_time, mib IS NOT NULL AS has_mib, "
                + ", ".join("%s IS NOT NULL AS has_%s" % (s, s) for s in readings_db.SIBS))


def summarize(row):
    d = dict(row)
    d["sibs"] = [int(s[3:]) for s in readings_db.SIBS if d.pop("has_" + s)]
    return d


def db():
    conn = readings_db.connect(READINGS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def list_readings(scan_id=None):
    with db() as conn:
        if scan_id:
            rows = conn.execute("SELECT %s FROM readings WHERE scan_id = ? ORDER BY id DESC"
                                % SUMMARY_COLS, (scan_id,))
        else:
            rows = conn.execute("SELECT %s FROM readings ORDER BY id DESC" % SUMMARY_COLS)
        return [summarize(r) for r in rows]


def get_reading(rid):
    with db() as conn:
        row = conn.execute("SELECT * FROM readings WHERE id = ?", (rid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ["mib"] + readings_db.SIBS:
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def list_scans():
    with db() as conn:
        rows = conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM readings r WHERE r.scan_id = s.id) AS readings "
            "FROM scans s ORDER BY s.id DESC")
        return [dict(r) for r in rows]


def db_watch_thread():
    """push readings created/updated since the last check"""
    last = ""
    sent = {}  # id -> summary last pushed; timestamps have 1 s resolution, so compare content
    while True:
        time.sleep(1)
        try:
            with db() as conn:
                rows = conn.execute("SELECT %s FROM readings WHERE updated >= ? ORDER BY updated"
                                    % SUMMARY_COLS, (last,)).fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            d = summarize(r)
            last = max(last, d["updated"])
            if sent.get(d["id"]) != d:
                sent[d["id"]] = d
                hub.send("reading", d)


# --- scan process ---

STATUS_RES = [
    (re.compile(r"^scan id: (\d+)"), lambda m: {"scan_id": int(m.group(1))}),
    (re.compile(r"^task: (\S+)"), lambda m: {"task": m.group(1)}),
    (re.compile(r"^\[srsue\] connecting to (\d+)"), lambda m: {"task": "srsue", "earfcn": int(m.group(1))}),
    (re.compile(r"^frequency correction: ([-\d.]+) ppm"), lambda m: {"ppm": float(m.group(1))}),
    (re.compile(r"^calibrating"), lambda m: {"task": "calibrating"}),
    (re.compile(r"^sweeping band"), lambda m: {"task": "sweeping"}),
    (re.compile(r"EARFCN (\d+) Freq\. .* looking for PSS"), lambda m: {"task": "cell_search", "earfcn": int(m.group(1))}),
]
NOISE = re.compile(r"^\s*$|^\.+$|^(earfcn|start_earfcn|scanned earfcns|queue to scan earfcn)")


def reader_thread(proc):
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        for rx, fn in STATUS_RES:
            m = rx.search(line)
            if m:
                hub.set_status(**fn(m))
        if not NOISE.search(line):
            hub.add_log(line)
    code = proc.wait()
    sid = hub.status.get("scan_id")
    if sid:
        with db() as conn:
            if not conn.execute("SELECT finished FROM scans WHERE id = ?", (sid,)).fetchone()[0]:
                readings_db.end_scan(conn, sid)
    hub.add_log("[webapp] scan finished (exit code %s)" % code)
    hub.set_status(running=False, task=None, earfcn=None, exit_code=code)
    hub.proc = None


class BadRequest(Exception):
    pass


def build_args(p):
    """sib-scan.sh arguments from the web form, validated (no shell involved)"""
    args = []
    mode = p.get("mode", "sweep")
    band = p.get("band")
    if band not in (None, ""):
        try:
            band = int(band)
        except ValueError:
            raise BadRequest("band must be a number")
    if mode == "sweep":
        if not band:
            raise BadRequest("sweep mode needs a band")
        args += ["-S", "-b", str(band)]
    elif mode == "cell_search":
        if not band:
            raise BadRequest("cell_search needs a band")
        args += ["-b", str(band)]
    elif mode == "list":
        earfcns = re.split(r"[\s,]+", str(p.get("earfcns", "")).strip())
        if not earfcns or not all(e.isdigit() for e in earfcns):
            raise BadRequest("EARFCN list must be numbers separated by spaces or commas")
        args += ["-q", " ".join(earfcns)]
    else:
        raise BadRequest("unknown mode")

    device = p.get("device", "soapy")
    if device not in DEVICES:
        raise BadRequest("unknown device")
    if device:
        args += ["-d", device]
    dev_args = str(p.get("device_args", ""))
    if len(dev_args) > 200 or "\n" in dev_args:
        raise BadRequest("device args too long")
    if dev_args:
        args += ["-a", dev_args]

    for key, flag in (("gain", "-g"), ("t", "-t"), ("T", "-T")):
        v = p.get(key)
        if v not in (None, ""):
            try:
                float(v)
            except ValueError:
                raise BadRequest("%s must be a number" % key)
            args += [flag, str(int(float(v)))]

    ppm = str(p.get("ppm", "")).strip()
    if ppm == "auto":
        if not band:
            raise BadRequest("automatic ppm needs a band")
        args += ["-p", "auto"]
    elif ppm:
        try:
            args += ["-p", str(float(ppm))]
        except ValueError:
            raise BadRequest("ppm must be a number or auto")

    if not p.get("recursive", False):
        args.append("-n")
    args += ["-R", READINGS_DB, "-D", CELLS_DB, "-L", location.LOCATION_FILE]
    return args


def start_scan(params):
    if hub.proc and hub.proc.poll() is None:
        raise BadRequest("a scan is already running")
    args = build_args(params)
    with hub.lock:
        hub.log.clear()
    hub.add_log("[webapp] ./sib-scan.sh " + " ".join(args))
    proc = subprocess.Popen(["bash", SIB_SCAN] + args, cwd=VOL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            start_new_session=True)
    hub.proc = proc
    hub.set_status(running=True, scan_id=None, task="starting", earfcn=None, ppm=None,
                   started=readings_db.now(), args=args, exit_code=None)
    threading.Thread(target=reader_thread, args=(proc,), daemon=True).start()


def stop_scan():
    proc = hub.proc
    if not proc or proc.poll() is not None:
        return False
    hub.add_log("[webapp] stopping scan")
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True

    def kill_later():
        time.sleep(4)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    threading.Thread(target=kill_later, daemon=True).start()
    return True


# --- HTTP ---

class Handler(BaseHTTPRequestHandler):
    server_version = "lte-sib-parser"

    def log_message(self, fmt, *a):
        pass

    def host_ok(self):
        # refuse DNS rebinding: only accept requests addressed to this machine by name/IP
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("localhost", "127.0.0.1", "::1")

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.host_ok():
            return self.reply(403, {"error": "forbidden host"})
        url = urlparse(self.path)
        path = url.path
        if path == "/":
            path = "/static/index.html"
        if path.startswith("/static/"):
            name = os.path.normpath(path[len("/static/"):])
            full = os.path.join(STATIC, name)
            if name.startswith("..") or not os.path.isfile(full):
                return self.reply(404, {"error": "not found"})
            with open(full, "rb") as f:
                return self.reply(200, f.read(), STATIC_TYPES.get(os.path.splitext(full)[1],
                                                                  "application/octet-stream"))
        if path == "/api/events":
            return self.events()
        if path == "/api/status":
            return self.reply(200, hub.status)
        if path == "/api/location":
            return self.reply(200, {"location": hub.location, "gpsd": hub.location is not None
                                    and hub.location.get("source") == "gpsd"})
        if path == "/api/bands":
            with sqlite3.connect(BANDS_DB) as conn:
                rows = conn.execute("SELECT band, name, mode, start_freq, end_freq FROM lte ORDER BY band")
                return self.reply(200, [dict(zip(("band", "name", "mode", "start_mhz", "end_mhz"), r))
                                        for r in rows])
        if path == "/api/scans":
            return self.reply(200, list_scans())
        if path == "/api/readings":
            sid = parse_qs(url.query).get("scan_id", [None])[0]
            return self.reply(200, list_readings(int(sid) if sid and sid.isdigit() else None))
        m = re.fullmatch(r"/api/readings/(\d+)", path)
        if m:
            r = get_reading(int(m.group(1)))
            return self.reply(200, r) if r else self.reply(404, {"error": "not found"})
        return self.reply(404, {"error": "not found"})

    def do_POST(self):
        if not self.host_ok():
            return self.reply(403, {"error": "forbidden host"})
        # JSON only: cross-site forms cannot send it without a CORS preflight we never allow
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.reply(415, {"error": "JSON only"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
        except ValueError:
            return self.reply(400, {"error": "bad JSON"})
        path = urlparse(self.path).path
        try:
            if path == "/api/scan":
                start_scan(body)
                return self.reply(200, {"ok": True})
            if path == "/api/stop":
                return self.reply(200, {"ok": stop_scan()})
            if path == "/api/location":
                return self.set_location(body)
        except BadRequest as e:
            return self.reply(400, {"error": str(e)})
        return self.reply(404, {"error": "not found"})

    def set_location(self, body):
        if body.get("clear"):
            hub.client_loc = None
        else:
            try:
                lat, lon = float(body["lat"]), float(body["lon"])
                acc = float(body["accuracy"]) if body.get("accuracy") is not None else None
            except (KeyError, TypeError, ValueError):
                raise BadRequest("lat and lon are required numbers")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise BadRequest("coordinates out of range")
            source = body.get("source")
            if source not in ("browser", "manual"):
                raise BadRequest("source must be browser or manual")
            # a manual correction wins over later browser updates until cleared
            if source == "browser" and hub.client_loc and hub.client_loc["source"] == "manual":
                return self.reply(200, {"ignored": "manual position set"})
            hub.client_loc = {"lat": lat, "lon": lon, "accuracy": acc, "source": source,
                              "time": readings_db.now()}
        hub.update_location()
        return self.reply(200, {"location": hub.location})

    def events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = hub.subscribe()
        try:
            with hub.lock:
                backlog = list(hub.log)
                st = dict(hub.status)
            self.wfile.write(("event: status\ndata: %s\n\n" % json.dumps(st)).encode())
            self.wfile.write(("event: location\ndata: %s\n\n" % json.dumps(
                {"location": hub.location, "gpsd": bool(hub.location and hub.location.get("source") == "gpsd")})).encode())
            self.wfile.write(("event: backlog\ndata: %s\n\n" % json.dumps(backlog)).encode())
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = ": keepalive\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe(q)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    os.makedirs(os.path.dirname(READINGS_DB), exist_ok=True)
    readings_db.connect(READINGS_DB).close()
    # start from the browser/map (none yet) or gpsd; drop a stale file from an old run
    write_location_file(None)
    for target in (gpsd_thread, gps_staleness_thread, db_watch_thread):
        threading.Thread(target=target, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print("lte-sib-parser web app on http://%s:%d" % (a.host, a.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop_scan()


if __name__ == "__main__":
    main()
