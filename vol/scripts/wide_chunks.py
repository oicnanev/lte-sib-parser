#!/usr/bin/env python3
"""Split an LTE band's downlink into wide captures for lte_sib_decoder's "scan".

Prints one line per capture: "<centre_hz> <earfcn_lo> <earfcn_hi>". The EARFCN
ranges do not overlap, so a cell is looked for (and decoded) in one capture only.
Each range fits in the part of the capture the decoder uses: 0.4 x the sample
rate minus 0.6 MHz each side of the centre (the AD9361's filter is 0.8 x the
rate; the PSS/SSS/PBCH need the centre 1.08 MHz of a carrier).

usage: wide_chunks.py -b <band> [-r <sample rate, default 30.72e6>] [-d <bands db>]
"""
import argparse
import math
import sqlite3

p = argparse.ArgumentParser()
p.add_argument("-b", "--band", type=int, required=True)
p.add_argument("-r", "--rate", type=float, default=30.72e6)
p.add_argument("-d", "--db", default="/vol/helpers/lte_bands.sqlite3")
a = p.parse_args()

row = sqlite3.connect(a.db).execute(
    "SELECT start_freq, start_earfcn, end_earfcn FROM lte WHERE band = ?", (a.band,)).fetchone()
if not row:
    raise SystemExit("unknown band %d" % a.band)
f0, e_lo, e_hi = row  # f0 in MHz at e_lo, 100 kHz raster

half = 0.4 * a.rate / 1e6 - 0.6          # MHz usable each side of the centre
per = int(2 * half * 10)                 # EARFCNs per capture
n = math.ceil((e_hi - e_lo + 1) / per)
per = math.ceil((e_hi - e_lo + 1) / n)   # spread evenly
for k in range(n):
    lo = e_lo + k * per
    hi = min(e_hi, lo + per - 1)
    centre = f0 + 0.1 * ((lo + hi) / 2 - e_lo)
    print("%d %d %d" % (round(centre * 1e6), lo, hi))
