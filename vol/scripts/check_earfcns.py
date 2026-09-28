#!/usr/bin/env python3
# Check a list of EARFCNs for LTE cells with PSS/SSS (lte_pss.py), without a sweep.
# Prints on stdout:
#   ppm <value>        first line: the clock correction used (measured with -p auto)
#   <earfcn>           one per line: EARFCNs with a cell, for srsue
# EARFCNs listed with --wide (known to be too wide for the SDR) that have a cell
# are saved as detection-only readings instead of printed.
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lte_pss  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("-e", "--earfcns", required=True, help="EARFCNs separated by spaces or commas")
parser.add_argument("-p", "--ppm", default="auto",
                    help="clock correction in ppm (positive tunes higher), or auto to measure it")
parser.add_argument("--wide", default="", help="EARFCNs known to be too wide for the SDR")
parser.add_argument("--sdr", choices=["hackrf", "bladerf"], default="hackrf",
                    help="SDR used for the captures")
parser.add_argument("--gain", type=int, default=30, help="bladeRF RX gain in dB")
parser.add_argument("-l", "--lna", type=int, default=32)
parser.add_argument("-g", "--vga", type=int, default=20)
parser.add_argument("-d", "--database", default="/vol/helpers/lte_bands.sqlite3")
parser.add_argument("--readings-db", help="readings database for detection-only readings")
parser.add_argument("--scan-id", type=int)
parser.add_argument("--location-file")
parser.add_argument("-v", "--verbose", action="store_true")
args = parser.parse_args()
lte_pss.configure(args.sdr, args.gain)


def numbers(text):
    return [int(e) for e in text.replace(",", " ").split() if e.isdigit()]


earfcns = list(dict.fromkeys(numbers(args.earfcns)))
wide = set(numbers(args.wide))
bands = sqlite3.connect(args.database)


def band_freq(earfcn):
    row = bands.execute(
        "SELECT band, start_freq, start_earfcn FROM lte WHERE ? BETWEEN start_earfcn AND end_earfcn",
        (earfcn,)).fetchone()
    return (row[0], row[1] + 0.1 * (earfcn - row[2])) if row else (None, None)


known = [(e, *band_freq(e)) for e in earfcns]
for e, b, f in known:
    if b is None and args.verbose:
        sys.stderr.write("EARFCN %d: not in the band table, skipped\n" % e)
known = [k for k in known if k[1] is not None]

# the EARFCN is exact, so the offset measured on a cell is the clock error alone
# (no raster ambiguity as in calibrate_ppm.py): the search covers +-150 kHz,
# i.e. clock errors up to ~57 ppm at 2.6 GHz
results = lte_pss.measure_many([f * 1e6 for _, _, f in known], args.lna, args.vga)

if args.ppm == "auto":
    ppms = sorted(r["cfo_hz"] / (f * 1e6) * 1e6 for (_, _, f), r in zip(known, results) if r["locked"])
    ppm = ppms[len(ppms) // 2] if ppms else 0.0
    if args.verbose:
        sys.stderr.write("clock: %s\n" % ("%+.2f ppm (median of %d cells)" % (ppm, len(ppms))
                                          if ppms else "no cell found, 0 ppm"))
else:
    ppm = float(args.ppm)
print("ppm %.2f" % ppm, flush=True)

conn = None
for (e, b, f), r in zip(known, results):
    # a lock far from the expected offset belongs to another carrier nearby
    expected = f * 1e6 * ppm * 1e-6
    if r["locked"] and abs(r["cfo_hz"] - expected) > 10e3:
        r = dict(r, locked=False)
    if not r["locked"]:
        if args.verbose:
            sys.stderr.write("EARFCN %d (B%s %.1f MHz): no cell\n" % (e, b, f))
        continue
    note = "EARFCN %d (B%s %.1f MHz): PCI %d (SSS %.2f)" % (e, b, f, r["pci"], r["sss_score"])
    if e in wide:
        if args.readings_db:
            import location
            import readings_db
            conn = conn or readings_db.connect(args.readings_db)
            loc = location.current(args.location_file) if args.location_file else location.current()
            rid = readings_db.create_reading(conn, args.scan_id, e, str(b), round(f, 1), loc,
                                             detection="pss")
            readings_db.update_reading(conn, rid, pci=r["pci"], bandwidth_mhz=20)
        if args.verbose:
            sys.stderr.write(note + ": too wide for srsue on this SDR, saved as detected only\n")
        continue
    if args.verbose:
        sys.stderr.write(note + "\n")
    print(e, flush=True)
