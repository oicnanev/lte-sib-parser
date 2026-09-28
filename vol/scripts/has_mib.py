#!/usr/bin/env python3
# Exit 0 if a MIB was decoded for the given EARFCN.
#   has_mib.py -R <readings.sqlite> -I <scan_id> [--sib1] <earfcn>
#       in this scan (readings database); --sib1: SIB1 too, i.e. the cell identity
#   has_mib.py -d <cells.sqlite> <earfcn>                    ever (legacy per-EARFCN table)
import sqlite3
import sys

try:
    earfcn = int(sys.argv[-1])
except ValueError:
    print("usage: ./has_mib.py [-R <readings.sqlite> -I <scan_id> | -d <cells.sqlite>] <earfcn>")
    exit(2)


def arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


try:
    if "-R" in sys.argv:
        conn = sqlite3.connect(arg("-R"))
        col = "sib1" if "--sib1" in sys.argv else "mib"
        row = conn.execute("SELECT %s FROM readings WHERE scan_id = ? AND earfcn = ? AND %s IS NOT NULL"
                           % (col, col), (int(arg("-I", "0")), earfcn)).fetchone()
    else:
        conn = sqlite3.connect(arg("-d", "/vol/output/cells.sqlite"))
        row = conn.execute("SELECT mib FROM cells WHERE earfcn = ?;", (earfcn,)).fetchone()
except sqlite3.Error:
    exit(1)
exit(0 if row and row[0] else 1)
