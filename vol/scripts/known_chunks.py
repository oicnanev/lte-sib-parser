#!/usr/bin/env python3
"""Group known EARFCNs into wide captures for lte_sib_decoder's "wide" command.

Prints one line per capture: "<centre_hz> <earfcn> <earfcn> ...". All carriers of
a line fit in the part of the capture the decoder uses (0.4 x the sample rate
minus 0.6 MHz each side of the centre, as in wide_chunks.py) and are on the same
side of 1 GHz (the gain differs there).

usage: known_chunks.py [-r <sample rate, default 30.72e6>] [-d <bands db>] <earfcn>...
"""
import argparse
import sqlite3

p = argparse.ArgumentParser()
p.add_argument("-r", "--rate", type=float, default=30.72e6)
p.add_argument("-d", "--db", default="/vol/helpers/lte_bands.sqlite3")
p.add_argument("earfcns", nargs="+", type=int)
a = p.parse_args()

conn = sqlite3.connect(a.db)


def dl_hz(earfcn):
    row = conn.execute("SELECT start_freq, start_earfcn FROM lte WHERE ? >= start_earfcn "
                       "AND ? <= end_earfcn", (earfcn, earfcn)).fetchone()
    if not row:
        raise SystemExit("unknown EARFCN %d" % earfcn)
    return int((row[0] + 0.1 * (earfcn - row[1])) * 1e6)


span = 2 * (0.4 * a.rate - 0.6e6)  # widest group, Hz
freqs = sorted((dl_hz(e), e) for e in dict.fromkeys(a.earfcns))
group = []
for f, e in freqs + [(None, None)]:
    if group and (f is None or f - group[0][0] > span or (f >= 1e9) != (group[0][0] >= 1e9)):
        centre = (group[0][0] + group[-1][0]) / 2
        print(round(centre), *[g[1] for g in group])
        group = []
    if f is not None:
        group.append((f, e))
