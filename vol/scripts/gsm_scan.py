#!/usr/bin/env python3
"""2G (GSM) scan: wide captures of the GSM-900 / DCS-1800 downlink, every BCCH
in them decoded by gsm_decode.py, one reading per cell in the readings
database (rat 'GSM': ARFCN in earfcn, BSIC in pci, LAC in tac, CI in cell_id,
CGI MCC-MNC-LAC-CI, system information as JSON in gsm).

  gsm_scan.py [--sdr bladerf|hackrf] [--bands 900 1800] [-R readings.sqlite]
              [-L location.json] [--ppm P] [--gain-low G] [--gain-high G]

bladeRF: 56 MSPS, 50 MHz analog bandwidth: GSM-900 in one capture, DCS-1800 in
two, all recorded in one bladeRF-cli session. HackRF: 20 MSPS, 3 + 5 captures.
With a persistent lte_sib_decoder running (sib-scan.sh -P) the bladeRF is not
opened again: the decoder records at its 30.72 MSPS (23 MHz usable: 2 + 4
captures), which avoids the USB resets that opening the board causes in a VM.
Each capture is 1.2 s (every SI3 slot of the 51-multiframe cycle comes at
least once), decoded by gsm_decoder (C++) while the next ones are recorded.
"""
import argparse
import json
import select
import threading
import math
import os
import shutil
import signal
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gsm_decode  # noqa: E402
import location  # noqa: E402
import readings_db  # noqa: E402

BANDS = {900: ("GSM900", gsm_decode.band_arfcns(900)), 1800: ("DCS1800", gsm_decode.band_arfcns(1800))}

# rate, analog bandwidth, sample type, full scale; gains per band (bladeRF dB;
# HackRF "lna,vga") measured with a Cisco 4G-LTE-ANTM-D antenna: bladeRF gain
# 40 clipped at 1.8 GHz next to strong LTE carriers, 30 did not
SDRS = {
    "bladerf": {"rate": 56e6, "bw": 50e6, "dtype": np.int16, "full": 2048, "gain": {900: "15", 1800: "30"}},
    # same board through the open lte_sib_decoder: its fixed rate, no new open
    "bladerf-dec": {"rate": 30.72e6, "bw": 23e6, "dtype": np.int16, "full": 2048, "gain": {900: "15", 1800: "30"}},
    "hackrf": {"rate": 20e6, "bw": 15e6, "dtype": np.int8, "full": 128,
               "gain": {900: os.environ.get("LTE_HACKRF_LOW_GAIN") or "24,16", 1800: "32,20"}},
}


DEC_PREFIX = "/tmp/lte_decoder"  # FIFOs of sib-scan.sh -P (lte_sib_decoder -D)


def decoder_pid():
    """pid of the persistent lte_sib_decoder, or None"""
    try:
        pid = int(open(DEC_PREFIX + ".pid").read())
        os.kill(pid, 0)
        os.stat(DEC_PREFIX + ".in")
        os.stat(DEC_PREFIX + ".out")
        return pid
    except (OSError, ValueError):
        return None


def plan(band, sdr):
    """[(centre Hz, [arfcns])]: contiguous ARFCN groups, each inside the usable bandwidth"""
    arfcns = sorted(BANDS[band][1], key=gsm_decode.arfcn_freq)
    span = SDRS[sdr]["bw"] - 300e3
    width = gsm_decode.arfcn_freq(arfcns[-1]) - gsm_decode.arfcn_freq(arfcns[0])
    n = max(1, math.ceil(width / span))
    per = math.ceil(len(arfcns) / n)
    out = []
    for i in range(0, len(arfcns), per):
        g = arfcns[i:i + per]
        centre = (gsm_decode.arfcn_freq(g[0]) + gsm_decode.arfcn_freq(g[-1])) / 2
        out.append((round(centre / 1e3) * 1e3, g))
    return out


class Recorder:
    """records the captures in order; wait(i) returns once capture i is
    complete. bladeRF: one bladeRF-cli session for all of them (opening the
    board costs ~2 s each time: 3 captures in 6.1 s instead of ~10 s), a file
    is complete when it has its full size. HackRF: one hackrf_transfer per
    capture, the next one started as soon as the previous one ends. Captures
    are tuned ppm higher so that the samples are centred on the nominal
    frequency."""

    def __init__(self, sdr, jobs, files, secs, gains, ppm):
        self.sdr, self.jobs, self.files, self.gains, self.ppm = sdr, jobs, files, gains, ppm
        self.c = SDRS[sdr]
        self.secs = secs
        self.n = int(self.c["rate"] * (secs + 0.05))
        self.bytes = self.n * 2 * np.dtype(self.c["dtype"]).itemsize
        self.proc = None

    def _tuned(self, i):
        return int(round(self.jobs[i][1] * (1 + self.ppm / 1e6)))

    def _start(self, i):
        """record captures i.. (bladeRF) or capture i (HackRF)"""
        self.stop()
        for f in self.files[i:]:
            try:
                os.unlink(f)
            except OSError:
                pass
        c = self.c
        if self.sdr == "bladerf":
            # AGC is on by default on the bladeRF 2.0 and blocks manual gain
            cmds = ["set samplerate rx %d" % c["rate"], "set bandwidth rx %d" % c["bw"], "set agc rx off"]
            for j in range(i, len(self.jobs)):
                cmds += ["set frequency rx %d" % self._tuned(j), "set gain rx %s" % self.gains[self.jobs[j][0]],
                         "rx config file=%s format=bin n=%d" % (self.files[j], self.n), "rx start", "rx wait"]
            cmd = ["bladeRF-cli", "-e", "; ".join(cmds)]
        else:
            lna, vga = self.gains[self.jobs[i][0]].split(",")
            cmd = ["hackrf_transfer", "-r", self.files[i], "-f", str(self._tuned(i)), "-s", str(int(c["rate"])),
                   "-n", str(self.n), "-l", lna, "-g", vga]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def start(self):
        self._start(0)

    def _complete(self, i):
        if self.sdr == "hackrf":
            return self.proc.poll() == 0
        try:
            return os.path.getsize(self.files[i]) == self.bytes
        except OSError:
            return False

    def wait(self, i):
        for attempt in range(2):
            # generous: opening the board ~2 s, each capture before this one ~1.5 s
            deadline = time.time() + 15 + 2 * (self.secs + 0.5) * (i + 1)
            while not self._complete(i):
                code = self.proc.poll()
                if (code is not None and not self._complete(i)) or time.time() > deadline:
                    break
                time.sleep(0.02)
            if self._complete(i):
                if self.sdr == "hackrf" and i + 1 < len(self.jobs):
                    self._start(i + 1)
                return True
            # seen once with bladeRF-cli: a capture stopped after a few ms and
            # never returned; record from this one again
            print("[gsm] capture at %.1f MHz hung or failed, recording it again" % (self.jobs[i][1] / 1e6),
                  flush=True)
            self._start(i)
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            # SIGINT lets bladeRF-cli close the device; kill -9 makes QEMU drop it
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


class DecoderRecorder:
    """same interface as Recorder, the captures recorded by the open
    lte_sib_decoder ("rec" commands over its FIFOs), one after the other while
    the earlier ones are decoded"""

    def __init__(self, jobs, files, secs, gains, ppm):
        self.jobs, self.files, self.gains, self.ppm, self.secs = jobs, files, gains, ppm, secs
        self.c = SDRS["bladerf-dec"]
        self.ok = [False] * len(jobs)
        self.done = [threading.Event() for _ in jobs]
        self.fin = self.fout = None
        self.thread = None

    def _line(self, timeout):
        buf = b""
        end = time.time() + timeout
        while not buf.endswith(b"\n"):
            left = end - time.time()
            if left <= 0 or not select.select([self.fout], [], [], left)[0]:
                return None
            ch = os.read(self.fout, 1)
            if not ch:
                return None
            buf += ch
        return buf.decode(errors="replace").strip()

    def _run(self):
        try:
            self.fin = os.open(DEC_PREFIX + ".in", os.O_RDWR)
            self.fout = os.open(DEC_PREFIX + ".out", os.O_RDWR)
            # discard what a previous client left, up to our hello
            os.write(self.fin, b"hello\n")
            while True:
                line = self._line(120)
                if line is None:
                    return
                if line == "hello":
                    break
            for i, (band, fc, _) in enumerate(self.jobs):
                tuned = int(round(fc * (1 + self.ppm / 1e6)))
                os.write(self.fin, ("rec %s %d %.3f %s\n" % (self.files[i], tuned, self.secs + 0.05,
                                                              self.gains[band])).encode())
                while True:
                    line = self._line(60)
                    if line is None or line == "rec failed":
                        return
                    if line.startswith("recorded"):
                        break
                self.ok[i] = True
                self.done[i].set()
        except OSError:
            pass
        finally:
            for e in self.done:
                e.set()   # a failed capture must not leave wait() hanging

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def wait(self, i):
        self.done[i].wait(15 + 3 * (self.secs + 0.5) * (i + 1))
        return self.ok[i]

    def stop(self):
        pass


def kill_decoder():
    """the SDR does not answer: close the decoder so that a replugged board can be opened again"""
    pid = decoder_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGINT)
        except OSError:
            pass
    for ext in (".in", ".out", ".pid", ".cfg"):
        try:
            os.unlink(DEC_PREFIX + ext)
        except OSError:
            pass


def clipping(path, sdr):
    c = SDRS[sdr]
    raw = np.memmap(path, dtype=c["dtype"], mode="r")
    part = np.asarray(raw[:min(len(raw), 4_000_000)]).astype(np.int32)
    return float(np.mean(np.abs(part) >= c["full"] - 2)) if len(part) else 0.0


def tmp_dir(need):
    """/dev/shm when it has room (docker-compose gives the container 2 GB), else /tmp"""
    for d in ("/dev/shm", "/tmp"):
        try:
            if shutil.disk_usage(d).free > need:
                return d
        except OSError:
            pass
    return "/tmp"


def save(conn, scan_id, band_label, r, loc, gain):
    si = r.get("si", {})
    lai = si.get("si3") or si.get("si4") or {}
    freq = round(gsm_decode.arfcn_freq(r["arfcn"]) / 1e6, 1)
    rid = readings_db.create_reading(conn, scan_id, r["arfcn"], band_label, freq, loc, detection="gsm")
    fields = {"rat": "GSM", "pci": r["bsic"], "rsrp": readings_db.gsm_rssi(r["level_dbfs"], gain),
              "bandwidth_mhz": readings_db.GSM_BANDWIDTH_MHZ,
              "gsm": json.dumps({"bsic": r["bsic"], "level_dbfs": r["level_dbfs"], "df_hz": r["df_hz"], "si": si})}
    if lai:
        fields.update(mcc=lai["mcc"], mnc=lai["mnc"], plmns="%s-%s" % (lai["mcc"], lai["mnc"]), tac=lai["lac"])
    if "ci" in si.get("si3", {}):
        fields.update(cell_id=si["si3"]["ci"], cgi="%s-%s-%d-%d" % (lai["mcc"], lai["mnc"], lai["lac"], si["si3"]["ci"]))
    readings_db.update_reading(conn, rid, **fields)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sdr", choices=list(SDRS), default="bladerf")
    p.add_argument("--bands", type=int, nargs="+", default=[900, 1800], choices=list(BANDS))
    p.add_argument("-R", "--readings-db", default="/vol/output/readings.sqlite")
    p.add_argument("-L", "--location-file", default=location.LOCATION_FILE)
    p.add_argument("--ppm", type=float, default=0.0, help="SDR clock error (positive tunes higher)")
    p.add_argument("--gain-low", help="GSM-900 gain (bladeRF dB, HackRF lna,vga)")
    p.add_argument("--gain-high", help="DCS-1800 gain")
    p.add_argument("--secs", type=float, default=1.2)
    p.add_argument("-j", "--jobs", type=int, default=os.cpu_count())
    a = p.parse_args()
    sdr = a.sdr
    if sdr == "bladerf" and decoder_pid():
        sdr = "bladerf-dec"
        print("[gsm] using the open lte_sib_decoder (no new open of the bladeRF)", flush=True)
    c = SDRS[sdr]
    gains = dict(c["gain"])
    if a.gain_low:
        gains[900] = a.gain_low
    if a.gain_high:
        gains[1800] = a.gain_high
    # the carrier error searched around each FCCH: clock error at 1.9 GHz plus margin
    gsm_decode.FCCH_MAX_HZ = 25e3 if (a.ppm or a.sdr == "bladerf") else 45e3

    conn = readings_db.connect(a.readings_db)
    args = "gsm_scan --sdr %s --bands %s" % (a.sdr, " ".join(map(str, a.bands)))
    scan_id = readings_db.new_scan(conn, None, a.ppm or None, args)
    print("scan id: %d" % scan_id, flush=True)

    jobs = [(b, fc, arfcns) for b in a.bands for fc, arfcns in plan(b, sdr)]
    size = int(c["rate"] * (a.secs + 0.05)) * 2 * np.dtype(c["dtype"]).itemsize
    d = tmp_dir((len(jobs) + 1) * size)   # a bladeRF records every capture while the first is decoded
    files = [os.path.join(d, "gsm.%d.%d.iq" % (os.getpid(), i)) for i in range(len(jobs))]

    main_pid = os.getpid()

    def cleanup(*_):
        # the decoder's worker processes inherit this handler and get SIGTERM
        # when their pool closes: only the main process may delete the captures
        if os.getpid() != main_pid:
            os._exit(0)
        for f in files:
            try:
                os.unlink(f)
            except OSError:
                pass
        if _:
            sys.exit(1)
    signal.signal(signal.SIGTERM, cleanup)

    t0 = time.time()
    cells = 0
    if sdr == "bladerf-dec":
        rec = DecoderRecorder(jobs, files, a.secs, gains, a.ppm)
    else:
        rec = Recorder(sdr, jobs, files, a.secs, gains, a.ppm)
    try:
        print("task: gsm_capture", flush=True)
        print("[gsm] capturing %s MHz" % ", ".join("%.1f" % (fc / 1e6) for _, fc, _ in jobs), flush=True)
        rec.start()
        for i, (b, fc, arfcns) in enumerate(jobs):
            if not rec.wait(i):
                if sdr == "bladerf-dec":
                    kill_decoder()
                print("[gsm] capture at %.1f MHz failed (is the SDR connected and free?)" % (fc / 1e6), flush=True)
                print("ERROR: the SDR stopped answering: unplug and replug it (in a VM also re-attach it)", flush=True)
                return 3
            clip = clipping(files[i], sdr)
            if clip > 0.002:
                print("[gsm] %.1f%% of the samples clipped at %.1f MHz: lower the gain" % (100 * clip, fc / 1e6),
                      flush=True)
            print("task: gsm_decode", flush=True)
            res = gsm_decode.decode_capture(
                files[i], c["rate"], fc, arfcns, bw=c["bw"], jobs=a.jobs, secs=a.secs,
                dtype=c["dtype"], full_scale=c["full"], log=lambda m: print("[gsm] " + m, flush=True))
            os.unlink(files[i])
            loc = location.current(a.location_file)
            for r in sorted(res, key=lambda r: gsm_decode.arfcn_freq(r["arfcn"])):
                if r.get("bsic") is None:
                    continue
                save(conn, scan_id, BANDS[b][0], r, loc, gains[b])
                cells += 1
                si = r.get("si", {})
                lai = si.get("si3") or si.get("si4")
                print("[gsm] ARFCN %d (%s %.1f MHz): BSIC %d%s%s, %s" % (
                    r["arfcn"], BANDS[b][0], gsm_decode.arfcn_freq(r["arfcn"]) / 1e6, r["bsic"],
                    ", %s-%s LAC %d" % (lai["mcc"], lai["mnc"], lai["lac"]) if lai else "",
                    " CI %d" % si["si3"]["ci"] if "si3" in si else "",
                    ("SI " + " ".join(sorted(k[2:] for k in si))) if si else "no SI decoded"), flush=True)
    finally:
        rec.stop()
        cleanup()
        readings_db.end_scan(conn, scan_id)
    print("[gsm] %d cells in %.0f s" % (cells, time.time() - t0), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
