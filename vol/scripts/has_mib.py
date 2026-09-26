#!/usr/bin/env python3
# Exit 0 if the results database has a decoded MIB for the given EARFCN.
import sqlite3
import sys

try:
    earfcn = int(sys.argv[-1])
except:
    print("usage: ./has_mib.py -d <cells.sqlite> <earfcn>")
    exit(2)

database = "/vol/output/cells.sqlite"
if "-d" in sys.argv:
    database = sys.argv[sys.argv.index("-d") + 1]

try:
    conn = sqlite3.connect(database)
    row = conn.execute("SELECT mib FROM cells WHERE earfcn = ?;", (earfcn,)).fetchone()
except sqlite3.Error:
    exit(1)
exit(0 if row and row[0] else 1)
