#!/usr/bin/env python3
# Measure the HackRF clock error in ppm from real LTE cells.
# Finds carriers with sweep_candidates.py, measures each carrier's frequency
# offset with PSS/SSS (lte_pss.py) and prints the median ppm, in the sign
# convention of sib-scan.sh -p (positive tunes higher).
# The carrier centre is only known to +-100 kHz, so for each carrier the raster
# error (multiple of 100 kHz) giving the smallest |ppm| is assumed: reliable
# while |ppm| * f < 50 kHz, i.e. up to ~60 ppm at 800 MHz, ~25 ppm at 1.9 GHz.
# Calibrate on a low band (B20/B8) when possible.
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lte_pss  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("-b", "--band", type=int, default=20)
parser.add_argument("-l", "--lna", type=int, default=32)
parser.add_argument("-g", "--vga", type=int, default=20)
parser.add_argument("-m", "--max-carriers", type=int, default=3)
parser.add_argument("--min-sss", type=float, default=0.4, help="minimum SSS score to trust a cell")
args = parser.parse_args()

here = os.path.dirname(os.path.abspath(__file__))
out = subprocess.run(
    [sys.executable, os.path.join(here, "sweep_candidates.py"), "-b", str(args.band),
     "-l", str(args.lna), "-g", str(args.vga), "--centres"],
    stdout=subprocess.PIPE, text=True, check=True,
).stdout
centres = [float(line.split()[0]) for line in out.splitlines() if line.strip()]
if not centres:
    sys.stderr.write("no carriers found in band %d\n" % args.band)
    exit(1)

ppms = []
for f in centres[: args.max_carriers]:
    tuned = round(f / 1e5) * 1e5  # nearest raster point
    r = lte_pss.measure(tuned, args.lna, args.vga, min_sss=args.min_sss)
    if r["sss_score"] < args.min_sss:
        sys.stderr.write("%.1f MHz: no cell (SSS score %.2f)\n" % (tuned / 1e6, r["sss_score"]))
        continue
    # cfo = raster error + tuned * ppm: pick the raster error with the smallest |ppm|
    ppm = min(((r["cfo_hz"] - k * 1e5) / tuned * 1e6 for k in (-1, 0, 1)), key=abs)
    sys.stderr.write("%.1f MHz: PCI %d, offset %+.1f kHz, SSS %.2f -> %+.2f ppm\n"
                     % (tuned / 1e6, r["pci"], r["cfo_hz"] / 1e3, r["sss_score"], ppm))
    ppms.append(ppm)

if not ppms:
    sys.stderr.write("calibration failed\n")
    exit(1)
ppms.sort()
print("%.2f" % ppms[len(ppms) // 2])
