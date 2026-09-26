#!/usr/bin/env python3
# Find candidate LTE carriers in a band with hackrf_sweep.
# Prints one line per detected carrier with the 3 EARFCNs (100 kHz raster) closest to
# the estimated centre, best first: the estimate can be off by ~100 kHz.
# Replacement for cell_search on HackRF, where PSS search at 1.92 MSPS is unreliable.
import argparse
import math
import sqlite3
import subprocess
import sys

BIN_HZ = 25000
MARGIN_MHZ = 5
# occupied bandwidth (MHz) of the standard LTE channel bandwidths
OCCUPIED = {1.4: 1.08, 3: 2.7, 5: 4.5, 10: 9.0, 15: 13.5, 20: 18.0}

parser = argparse.ArgumentParser()
parser.add_argument("-b", "--band", type=int, required=True)
parser.add_argument("-l", "--lna", type=int, default=32, help="LNA gain 0-40 (8 dB steps)")
parser.add_argument("-g", "--vga", type=int, default=20, help="VGA gain 0-62 (2 dB steps)")
parser.add_argument("-N", "--sweeps", type=int, default=20)
parser.add_argument("-p", "--ppm", type=float, default=0.0,
                    help="frequency correction in ppm, same meaning as sib-scan.sh -p")
parser.add_argument("-t", "--threshold", type=float, default=6.0, help="dB above noise floor")
parser.add_argument("-d", "--database", default="/vol/helpers/lte_bands.sqlite3")
parser.add_argument("-v", "--verbose", action="store_true", help="print carriers to stderr")
args = parser.parse_args()

conn = sqlite3.connect(args.database)
row = conn.execute(
    "SELECT start_freq, end_freq, start_earfcn FROM lte WHERE band = ?;", (args.band,)
).fetchone()
if row is None:
    sys.stderr.write("unknown band %d\n" % args.band)
    exit(1)
start_mhz, end_mhz, start_earfcn = row

### sweep ###
cmd = [
    "hackrf_sweep",
    # sweep past the band edges so the noise floor is visible even in a full band
    "-f", "%d:%d" % (math.floor(start_mhz) - MARGIN_MHZ, math.ceil(end_mhz) + MARGIN_MHZ),
    "-w", str(BIN_HZ),
    "-l", str(args.lna),
    "-g", str(args.vga),
    "-N", str(args.sweeps),
]
out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout

acc = {}  # bin index -> [sum of linear power, count]
for line in out.splitlines():
    f = [x.strip() for x in line.split(",")]
    if len(f) < 7:
        continue
    hz_low, width = float(f[2]), float(f[4])
    for i, db in enumerate(f[6:]):
        k = round((hz_low + (i + 0.5) * width) / BIN_HZ)
        a = acc.setdefault(k, [0.0, 0])
        a[0] += 10 ** (float(db) / 10)
        a[1] += 1

keys = sorted(acc)
if not keys:
    sys.stderr.write("no sweep data\n")
    exit(1)
db = [10 * math.log10(acc[k][0] / acc[k][1]) for k in keys]

# 5-bin (125 kHz) moving average for detection
smooth = [sum(db[max(0, i - 2):i + 3]) / len(db[max(0, i - 2):i + 3]) for i in range(len(db))]
noise = sorted(smooth)[len(smooth) // 20]

### find occupied runs ###
runs, i = [], 0
while i < len(smooth):
    if smooth[i] > noise + args.threshold:
        j = i
        while j + 1 < len(smooth) and smooth[j + 1] > noise + args.threshold:
            j += 1
        if (j - i + 1) * BIN_HZ >= 1e6:
            runs.append((i, j))
        i = j + 1
    else:
        i += 1


def crossing(a, b, level):
    """frequency (Hz) where the raw spectrum crosses level between bins a and b"""
    t = (level - db[a]) / (db[b] - db[a]) if db[b] != db[a] else 0.5
    return (keys[a] + min(max(t, 0.0), 1.0) * (keys[b] - keys[a])) * BIN_HZ


### refine edges at half level between noise and plateau, on the raw spectrum ###
found = []
for i, j in runs:
    plateau = sorted(db[i:j + 1])[(j - i + 1) // 2]
    half = (plateau + noise) / 2
    lo, hi = i + 2, j - 2
    while lo > 0 and db[lo - 1] > half:
        lo -= 1
    while hi < len(db) - 1 and db[hi + 1] > half:
        hi += 1
    f_lo = crossing(max(lo - 1, 0), lo, half)
    f_hi = crossing(hi, min(hi + 1, len(db) - 1), half)
    # the SDR reports a signal at f_true * (1 + ppm): undo it
    centre = (f_lo + f_hi) / 2 / (1 + args.ppm * 1e-6)
    width = (f_hi - f_lo) / 1e6
    bw = min(OCCUPIED, key=lambda b: abs(OCCUPIED[b] - width))
    pos = (centre / 1e6 - start_mhz) * 10  # fractional EARFCN offset
    earfcn = start_earfcn + round(pos)
    if not start_mhz <= centre / 1e6 <= end_mhz or earfcn in found:
        continue
    found.append(earfcn)
    nearest = sorted(range(math.floor(pos) - 1, math.floor(pos) + 3), key=lambda n: abs(n - pos))[:3]
    if args.verbose:
        sys.stderr.write(
            "carrier %.1f MHz width %.2f MHz (~%s MHz) %.1f dB above noise -> EARFCN %d\n"
            % (centre / 1e6, width, bw, plateau - noise, earfcn)
        )
    print(" ".join(str(start_earfcn + n) for n in nearest))
