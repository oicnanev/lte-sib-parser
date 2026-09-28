#!/usr/bin/env python3
# Exit 0 if a MIB was decoded for the given EARFCN.
#   has_mib.py -R <readings.sqlite> -I <scan_id> <earfcn>   in this scan (readings database)
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
        row = conn.execute("SELECT mib FROM readings WHERE scan_id = ? AND earfcn = ? AND mib IS NOT NULL",
                           (int(arg("-I", "0")), earfcn)).fetchone()
    else:
        conn = sqlite3.connect(arg("-d", "/vol/output/cells.sqlite"))
        row = conn.execute("SELECT mib FROM cells WHERE earfcn = ?;", (earfcn,)).fetchone()
except sqlite3.Error:
    exit(1)
exit(0 if row and row[0] else 1)
