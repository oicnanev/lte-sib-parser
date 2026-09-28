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
# EARFCNs learned from readings (read, or advertised in SIB5): kept apart from the
# readings so that deleting them does not forget where carriers are
LEARNED = os.path.join(VOL, "output", "earfcns_learned.json")
BANDS_DB = os.path.join(VOL, "helpers", "lte_bands.sqlite3")
DEVICES = {"soapy", "UHD", "bladeRF", ""}
GPS_STALE_S = 5
# bands swept by the "Portugal" preset: the FDD bands Portuguese operators use for
# LTE (B38/TDD is left out: lte_pss.py assumes FDD)
# order matters: automatic ppm calibration runs on the first band, and a band
# overlapping an earlier one (B28 includes 791-803 MHz of B20) skips carriers
# already read
PRESETS = {
    "pt_known": {"label": "Portugal (known EARFCNs, fast)", "earfcns_file": "portugal.txt"},
    "pt": {"label": "Portugal sweep: B20, B8, B28, B3, B1, B7", "bands": [20, 8, 28, 3, 1, 7]},
}
EARFCN_LISTS = os.path.join(VOL, "helpers", "earfcns")
LOG_LINES = 500

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class Hub:
    """shared state and SSE fan-out"""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []
        self.log = []
        self.proc = None
        self.status = {"running": False, "scan_id": None, "task": None, "earfcn": None,
                       "ppm": None, "band": None, "bands": [], "step": None,
                       "started": None, "band_started": None, "finished": None,
                       "exit_code": None}
        self.stop_requested = False
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
                "tac, eci, enb_id, cell_id, cgi, rsrp, bandwidth_mhz, detection, lat, lon, accuracy_m, location_source, "
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
    (re.compile(r"^retrying (\d+)"), lambda m: {"task": "srsue retry", "earfcn": int(m.group(1))}),
    (re.compile(r"^frequency correction: ([-\d.]+) ppm"), lambda m: {"ppm": float(m.group(1))}),
    (re.compile(r"^calibrating"), lambda m: {"task": "calibrating"}),
    (re.compile(r"^sweeping band"), lambda m: {"task": "sweeping"}),
    (re.compile(r"^checking EARFCNs"), lambda m: {"task": "checking EARFCNs"}),
    (re.compile(r"EARFCN (\d+) Freq\. .* looking for PSS"), lambda m: {"task": "cell_search", "earfcn": int(m.group(1))}),
]
NOISE = re.compile(r"^\s*$|^\.+$|^(earfcn|start_earfcn|scanned earfcns|queue to scan earfcn)")


def run_proc(proc):
    """follow one sib-scan.sh run until it exits; returns its exit code"""
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
    return code


def job_thread(steps, ppm, env=None):
    """run the steps (one sib-scan.sh call per band) one after another"""
    code = None
    done_mhz = []  # carriers read so far: overlapping bands (e.g. B28/B20) must not read them again
    for i, (band, args) in enumerate(steps):
        if hub.stop_requested:
            break
        a = list(args) + (["-p", ppm] if ppm else [])
        if done_mhz and "-S" in a:
            a += ["-x", " ".join("%.1f" % f for f in sorted(set(done_mhz)))]
        hub.set_status(step="%d/%d" % (i + 1, len(steps)) if len(steps) > 1 else None,
                       band=band, scan_id=None, task="starting", earfcn=None,
                       band_started=readings_db.now())
        hub.add_log("[webapp] ./sib-scan.sh " + " ".join(a))
        proc = subprocess.Popen(["bash", SIB_SCAN] + a, cwd=VOL, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                start_new_session=True)
        hub.proc = proc
        code = run_proc(proc)
        hub.proc = None
        sid = hub.status.get("scan_id")
        if sid:
            with db() as conn:
                done_mhz += [r[0] for r in conn.execute(
                    "SELECT dl_freq_mhz FROM readings WHERE scan_id = ? "
                    "AND (mib IS NOT NULL OR detection = 'pss')", (sid,))
                    if r[0] is not None]
        hub.add_log("[webapp] band %s finished (exit code %s)" % (band, code) if band
                    else "[webapp] scan finished (exit code %s)" % code)
        # calibrate once, on the lowest band, and reuse it: calibration on high
        # bands is ambiguous for large clock errors
        if ppm == "auto" and hub.status.get("ppm") is not None:
            ppm = "%.2f" % hub.status["ppm"]
            if i + 1 < len(steps):
                hub.add_log("[webapp] reusing %s ppm for the next bands" % ppm)
    if hub.stop_requested:
        hub.add_log("[webapp] stopped")
    try:
        update_learned()
    except (OSError, sqlite3.Error) as e:
        hub.add_log("[webapp] cannot update learned EARFCNs: %s" % e)
    hub.set_status(running=False, task=None, earfcn=None, step=None, band_started=None,
                   finished=readings_db.now(), exit_code=code)


class BadRequest(Exception):
    pass


def bands_table():
    with sqlite3.connect(BANDS_DB) as conn:
        return {r[0]: {"name": r[1], "mode": r[2], "start_mhz": r[3], "end_mhz": r[4]}
                for r in conn.execute("SELECT band, name, mode, start_freq, end_freq FROM lte")}


def number(p, key, default=None):
    v = p.get(key)
    if v in (None, ""):
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        raise BadRequest("%s must be a number" % key)


def build_job(p):
    """(steps, ppm) from the web form, validated (no shell involved).

    steps: [(band or None, sib-scan.sh args without -p)]; ppm: "auto", a number or "".
    """
    mode = p.get("mode", "sweep")
    table = bands_table()

    band = str(p.get("band", "")).strip()
    if band in PRESETS and "earfcns_file" in PRESETS[band]:
        return known_job(p, PRESETS[band]["earfcns_file"])
    if band in PRESETS:
        bands = PRESETS[band]["bands"]
    elif band == "custom":
        bands = [b for b in re.split(r"[\s,]+", str(p.get("bands", "")).strip()) if b]
        if not bands or not all(b.isdigit() for b in bands):
            raise BadRequest("band list must be numbers separated by spaces or commas")
        bands = [int(b) for b in bands]
    elif band.isdigit():
        bands = [int(band)]
    else:
        bands = []
    unknown = [b for b in bands if b not in table]
    if unknown:
        raise BadRequest("unknown band(s): %s" % " ".join(map(str, unknown)))
    # keep the given order (automatic calibration runs on the first band, so put a
    # low band first) and drop repeats
    bands = list(dict.fromkeys(bands))

    common = []
    device = p.get("device", "soapy")
    if device not in DEVICES:
        raise BadRequest("unknown device")
    if device:
        common += ["-d", device]
    dev_args = str(p.get("device_args", ""))
    if len(dev_args) > 200 or "\n" in dev_args:
        raise BadRequest("device args too long")
    if dev_args:
        common += ["-a", dev_args]
    for key, flag in (("t", "-t"), ("T", "-T")):
        v = number(p, key)
        if v is not None:
            common += [flag, str(v)]
    if not p.get("recursive", False):
        common.append("-n")
    common += ["-R", READINGS_DB, "-D", CELLS_DB, "-L", location.LOCATION_FILE]

    gain = number(p, "gain")
    gain_high = number(p, "gain_high", gain)

    def gain_args(b):
        g = gain_high if b is not None and table[b]["start_mhz"] >= 1000 else gain
        return ["-g", str(g)] if g is not None else []

    if mode in ("sweep", "cell_search"):
        if not bands:
            raise BadRequest("%s needs a band" % mode)
        steps = [(b, (["-S"] if mode == "sweep" else []) + ["-b", str(b)] + gain_args(b) + common)
                 for b in bands]
    elif mode == "list":
        earfcns = re.split(r"[\s,]+", str(p.get("earfcns", "")).strip())
        if not earfcns or not all(e.isdigit() for e in earfcns):
            raise BadRequest("EARFCN list must be numbers separated by spaces or commas")
        steps = [(None, ["-q", " ".join(earfcns)] + gain_args(None) + common)]
    else:
        raise BadRequest("unknown mode")

    ppm = str(p.get("ppm", "")).strip()
    if ppm == "auto":
        if mode == "list":
            raise BadRequest("automatic ppm needs a band")
    elif ppm:
        try:
            ppm = str(float(ppm))
        except ValueError:
            raise BadRequest("ppm must be a number or auto")
    return steps, ppm


def seed_earfcns(filename):
    """EARFCNs of a preset's list file (numbers at the start of a line)"""
    out = []
    with open(os.path.join(EARFCN_LISTS, filename)) as f:
        for line in f:
            word = line.split("#", 1)[0].strip()
            if word.isdigit():
                out.append(int(word))
    return out


def load_learned():
    try:
        with open(LEARNED) as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError):
        return {}


def update_learned():
    """merge the readings database into the learned-EARFCN file and return it.

    Per EARFCN: first_seen / last_seen (a reading on it), last_advertised (in a
    SIB5), bandwidth_mhz (widest known), plmns (operators seen on or advertising it)."""
    learned = load_learned()

    def entry(e):
        return learned.setdefault(e, {"first_seen": None, "last_seen": None,
                                      "last_advertised": None, "bandwidth_mhz": None, "plmns": []})

    def later(a, b):
        return max(x for x in (a, b) if x) if (a or b) else None

    def add_plmns(d, plmns):
        for p in (plmns or "").split():
            if p not in d["plmns"]:
                d["plmns"].append(p)

    with db() as conn:
        rows = conn.execute("SELECT earfcn, time, bandwidth_mhz, plmns, sib5 FROM readings").fetchall()
    for e, t, bw, plmns, sib5 in rows:
        d = entry(e)
        d["first_seen"] = min(x for x in (d["first_seen"], t) if x)
        d["last_seen"] = later(d["last_seen"], t)
        if bw is not None:
            d["bandwidth_mhz"] = max(bw, d["bandwidth_mhz"] or 0)
        add_plmns(d, plmns)
        if sib5:
            try:
                carriers = json.loads(sib5).get("interFreqCarrierFreqList", [])
            except ValueError:
                carriers = []
            for c in carriers:
                a = entry(c["dl-CarrierFreq"])
                a["last_advertised"] = later(a["last_advertised"], t)
                add_plmns(a, plmns)
    tmp = LEARNED + ".tmp"
    with open(tmp, "w") as f:
        json.dump({str(k): v for k, v in sorted(learned.items())}, f, indent=1)
    os.replace(tmp, LEARNED)
    return learned


def known_earfcns(filename):
    """(earfcns to check, EARFCNs known to be 20 MHz wide): the preset's file plus
    every learned EARFCN (read, or advertised in a SIB5, in any earlier scan)"""
    learned = update_learned()
    earfcns = seed_earfcns(filename) + sorted(learned)
    wide = sorted(e for e, d in learned.items() if (d.get("bandwidth_mhz") or 0) >= 20)
    return list(dict.fromkeys(earfcns)), wide


def earfcn_table(filename="portugal.txt"):
    """rows for the web app's Known EARFCNs panel"""
    seed = set(seed_earfcns(filename))
    learned = update_learned()
    with sqlite3.connect(BANDS_DB) as conn:
        ranges = conn.execute("SELECT band, start_freq, start_earfcn, end_earfcn FROM lte").fetchall()
    rows = []
    for e in sorted(seed | set(learned)):
        band, freq = None, None
        for b, f0, e0, e1 in ranges:
            if e0 <= e <= e1:
                band, freq = b, round(f0 + 0.1 * (e - e0), 1)
                break
        d = learned.get(e, {})
        rows.append({"earfcn": e, "band": band, "dl_freq_mhz": freq, "in_list_file": e in seed,
                     "first_seen": d.get("first_seen"), "last_seen": d.get("last_seen"),
                     "last_advertised": d.get("last_advertised"),
                     "bandwidth_mhz": d.get("bandwidth_mhz"), "plmns": d.get("plmns", [])})
    rows.sort(key=lambda r: (r["dl_freq_mhz"] is None, r["dl_freq_mhz"] or 0))
    return rows


def known_job(p, filename):
    """one sib-scan.sh -K run over the known EARFCNs"""
    earfcns, wide = known_earfcns(filename)
    args = ["-K", " ".join(map(str, earfcns))]
    # 20 MHz cells only need skipping on SDRs that cannot follow them (HackRF);
    # a bladeRF (61.44 MSPS) decodes them
    if wide and p.get("device") != "bladeRF":
        args += ["-W", " ".join(map(str, wide))]
    gain, gain_high = number(p, "gain"), number(p, "gain_high")
    if gain is not None:
        args += ["-g", str(gain)]
    if gain_high is not None:
        args += ["-G", str(gain_high)]
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
    for key, flag in (("t", "-t"), ("T", "-T")):
        v = number(p, key)
        if v is not None:
            args += [flag, str(v)]
    args += ["-R", READINGS_DB, "-D", CELLS_DB, "-L", location.LOCATION_FILE]
    ppm = str(p.get("ppm", "")).strip()
    if ppm and ppm != "auto":
        try:
            ppm = str(float(ppm))
        except ValueError:
            raise BadRequest("ppm must be a number or auto")
    return [(None, args)], ppm or "auto"


def scan_env(p):
    """environment for sib-scan.sh: HackRF capture gain below 1 GHz (lna,vga)"""
    env = dict(os.environ)
    low = str(p.get("hackrf_low_gain", "")).replace(" ", "")
    if low:
        if not re.fullmatch(r"\d{1,2},\d{1,2}", low):
            raise BadRequest("capture gain < 1 GHz must be lna,vga, e.g. 24,16")
        env["LTE_HACKRF_LOW_GAIN"] = low
    return env


def start_scan(params):
    if hub.status.get("running"):
        raise BadRequest("a scan is already running")
    steps, ppm = build_job(params)
    env = scan_env(params)
    with hub.lock:
        hub.log.clear()
    hub.stop_requested = False
    hub.set_status(running=True, scan_id=None, task="starting", earfcn=None, ppm=None,
                   band=None, step=None, started=readings_db.now(), band_started=None,
                   finished=None, exit_code=None,
                   bands=[b for b, _ in steps if b is not None])
    threading.Thread(target=job_thread, args=(steps, ppm, env), daemon=True).start()


def stop_scan():
    if not hub.status.get("running"):
        return False
    hub.stop_requested = True
    proc = hub.proc
    hub.add_log("[webapp] stopping scan")
    if not proc or proc.poll() is not None:
        return True
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
        elif path == "/favicon.ico":  # browsers ask for it at the root
            path = "/static/favicon.ico"
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
            bands = [dict(band=b, **v) for b, v in sorted(bands_table().items())]
            presets = [dict(id=k, **v) for k, v in PRESETS.items()]
            return self.reply(200, {"bands": bands, "presets": presets})
        if path == "/api/earfcns":
            return self.reply(200, earfcn_table())
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
    global READINGS_DB, LEARNED
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", help="readings database (default %s)" % READINGS_DB)
    ap.add_argument("--learned", help="learned EARFCNs file (default %s)" % LEARNED)
    a = ap.parse_args()
    if a.db:
        READINGS_DB = os.path.abspath(a.db)
    if a.learned:
        LEARNED = os.path.abspath(a.learned)
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
