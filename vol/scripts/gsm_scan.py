#!/usr/bin/env python3
"""2G (GSM) scan: wide captures of the GSM-900 / DCS-1800 downlink, every BCCH
in them decoded by gsm_decode.py, one reading per cell in the readings
database (rat 'GSM': ARFCN in earfcn, BSIC in pci, LAC in tac, CI in cell_id,
CGI MCC-MNC-LAC-CI, system information as JSON in gsm).

  gsm_scan.py [--sdr bladerf|hackrf] [--bands 900 1800] [-R readings.sqlite]
              [-L location.json] [--ppm P] [--gain-low G] [--gain-high G]

bladeRF: 56 MSPS, 50 MHz analog bandwidth: GSM-900 in one capture, DCS-1800 in
two. HackRF: 20 MSPS, 3 + 5 captures. Each capture is 1.2 s (every SI3 slot
of the 51-multiframe cycle comes at least once); the next capture is recorded
while the previous one is decoded.
"""
import argparse
import json
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
    "hackrf": {"rate": 20e6, "bw": 15e6, "dtype": np.int8, "full": 128,
               "gain": {900: os.environ.get("LTE_HACKRF_LOW_GAIN") or "24,16", 1800: "32,20"}},
}


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


def start_capture(sdr, freq, path, secs, gain, ppm):
    """start recording (returns the process): tuned ppm higher so that the
    samples are centred on the nominal freq"""
    c = SDRS[sdr]
    tuned = int(round(freq * (1 + ppm / 1e6)))
    n = int(c["rate"] * (secs + 0.05))
    if sdr == "bladerf":
        # AGC is on by default on the bladeRF 2.0 and blocks manual gain
        cmd = ["bladeRF-cli", "-e",
               "set frequency rx %d; set samplerate rx %d; set bandwidth rx %d; set agc rx off; "
               "set gain rx %s; rx config file=%s format=bin n=%d; rx start; rx wait"
               % (tuned, c["rate"], c["bw"], gain, path, n)]
    else:
        lna, vga = gain.split(",")
        cmd = ["hackrf_transfer", "-r", path, "-f", str(tuned), "-s", str(int(c["rate"])),
               "-n", str(n), "-l", lna, "-g", vga]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def finished(proc, secs):
    """capture ended well within a generous time (device setup is ~2 s)"""
    try:
        code = proc.wait(timeout=secs + 15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return False
    return code == 0


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


def save(conn, scan_id, band_label, r, loc):
    si = r.get("si", {})
    lai = si.get("si3") or si.get("si4") or {}
    freq = round(gsm_decode.arfcn_freq(r["arfcn"]) / 1e6, 1)
    rid = readings_db.create_reading(conn, scan_id, r["arfcn"], band_label, freq, loc, detection="gsm")
    fields = {"rat": "GSM", "pci": r["bsic"],
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
    c = SDRS[a.sdr]
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

    jobs = [(b, fc, arfcns) for b in a.bands for fc, arfcns in plan(b, a.sdr)]
    size = int(c["rate"] * (a.secs + 0.05)) * 2 * np.dtype(c["dtype"]).itemsize
    d = tmp_dir(3 * size)
    files = [os.path.join(d, "gsm.%d.%d.iq" % (os.getpid(), i)) for i in range(len(jobs))]

    def cleanup(*_):
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
    try:
        print("task: gsm_capture", flush=True)
        b, fc, _ = jobs[0]
        print("[gsm] capturing %.1f MHz (%s)" % (fc / 1e6, BANDS[b][0]), flush=True)
        proc = start_capture(a.sdr, fc, files[0], a.secs, gains[b], a.ppm)
        for i, (b, fc, arfcns) in enumerate(jobs):
            if not finished(proc, a.secs):
                # seen once with bladeRF-cli: a capture started during a decode
                # stopped after a few ms and never returned; record it again
                print("[gsm] capture at %.1f MHz hung, recording it again" % (fc / 1e6), flush=True)
                proc = start_capture(a.sdr, fc, files[i], a.secs, gains[b], a.ppm)
                if not finished(proc, a.secs):
                    print("[gsm] capture at %.1f MHz failed (is the SDR connected and free?)" % (fc / 1e6),
                          flush=True)
                    return 1
            clip = clipping(files[i], a.sdr)
            if clip > 0.002:
                print("[gsm] %.1f%% of the samples clipped at %.1f MHz: lower the gain" % (100 * clip, fc / 1e6),
                      flush=True)
            # record the next capture while this one is decoded
            if i + 1 < len(jobs):
                nb, nfc, _ = jobs[i + 1]
                print("[gsm] capturing %.1f MHz (%s)" % (nfc / 1e6, BANDS[nb][0]), flush=True)
                proc = start_capture(a.sdr, nfc, files[i + 1], a.secs, gains[nb], a.ppm)
            print("task: gsm_decode", flush=True)
            res = gsm_decode.decode_capture(
                files[i], c["rate"], fc, arfcns, bw=c["bw"], jobs=a.jobs, secs=a.secs,
                dtype=c["dtype"], full_scale=c["full"], log=lambda m: print("[gsm] " + m, flush=True))
            os.unlink(files[i])
            loc = location.current(a.location_file)
            for r in sorted(res, key=lambda r: gsm_decode.arfcn_freq(r["arfcn"])):
                if r.get("bsic") is None:
                    continue
                save(conn, scan_id, BANDS[b][0], r, loc)
                cells += 1
                si = r.get("si", {})
                lai = si.get("si3") or si.get("si4")
                print("[gsm] ARFCN %d (%s %.1f MHz): BSIC %d%s%s, %s" % (
                    r["arfcn"], BANDS[b][0], gsm_decode.arfcn_freq(r["arfcn"]) / 1e6, r["bsic"],
                    ", %s-%s LAC %d" % (lai["mcc"], lai["mnc"], lai["lac"]) if lai else "",
                    " CI %d" % si["si3"]["ci"] if "si3" in si else "",
                    ("SI " + " ".join(sorted(k[2:] for k in si))) if si else "no SI decoded"), flush=True)
            if i + 1 < len(jobs):
                print("task: gsm_capture", flush=True)
    finally:
        cleanup()
        readings_db.end_scan(conn, scan_id)
    print("[gsm] %d cells in %.0f s" % (cells, time.time() - t0), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
