#!/usr/bin/env python3
import sqlite3
import sys

try:
    earfcn = int(sys.argv[-1])
except:
    print("usage: ./earfcn_to_freq.py -d <./lte_bands.sqlite3> <earfcn>")
    exit()

database = "/vol/helpers/lte_bands.sqlite3"
if "-d" in sys.argv:
    database = sys.argv[sys.argv.index("-d") + 1]

conn = sqlite3.connect(database)
cursor = conn.cursor()
cursor.execute(
    "SELECT start_freq, start_earfcn FROM lte where ? >= start_earfcn and ? <= end_earfcn;",
    (earfcn, earfcn),
)

start_freq, start_earfcn = cursor.fetchone()
# downlink frequency in Hz
print(int((start_freq + 0.1 * (earfcn - start_earfcn)) * 1e6))
