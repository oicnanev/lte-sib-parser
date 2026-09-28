#!/usr/bin/env python3
# Readings database: one row per cell reading (EARFCN + PCI at a place and time),
# with location, cell global identity (CGI) and signal strength, plus every
# field of the legacy per-EARFCN "cells" table (band, time, rsrp, mib, sib1-13).
#
# CLI:
#   readings_db.py -d <db> new-scan [--band B] [--ppm P] [--args "..."]   -> prints scan id
#   readings_db.py -d <db> set-ppm <scan_id> <ppm>
#   readings_db.py -d <db> end-scan <scan_id>
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone

SIBS = ["sib%d" % i for i in range(1, 14)]

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    started TEXT NOT NULL,
    finished TEXT,
    band INTEGER,
    ppm REAL,
    args TEXT
);
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY,
    scan_id INTEGER REFERENCES scans(id),
    time TEXT NOT NULL,             -- first decoded message, UTC ISO 8601
    updated TEXT NOT NULL,          -- last update, UTC ISO 8601
    earfcn INTEGER NOT NULL,
    band TEXT,
    dl_freq_mhz REAL,
    pci INTEGER,
    mcc TEXT,
    mnc TEXT,
    plmns TEXT,                     -- all PLMNs in SIB1, e.g. "268-01 268-03"
    tac INTEGER,
    eci INTEGER,                    -- 28-bit E-UTRAN cell identity
    enb_id INTEGER,                 -- eci >> 8
    cell_id INTEGER,                -- eci & 0xff
    cgi TEXT,                       -- MCC-MNC-ECI of the first PLMN
    rsrp REAL,                      -- dBm
    bandwidth_mhz REAL,             -- from the MIB, or estimated by the sweep
    detection TEXT,                 -- srsue or decoder (decoded), pss (sync signals only)
    lat REAL,
    lon REAL,
    accuracy_m REAL,
    location_source TEXT,           -- gpsd, browser or manual
    location_time TEXT,
    mib TEXT,
    %s
);
CREATE INDEX IF NOT EXISTS readings_scan ON readings(scan_id);
""" % ",\n    ".join("%s TEXT" % s for s in SIBS)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# columns added after the first release: added to older databases on connect
MIGRATIONS = [("readings", "bandwidth_mhz", "REAL"), ("readings", "detection", "TEXT")]

# MIB dl-Bandwidth -> channel bandwidth in MHz
MIB_BANDWIDTH = {"n6": 1.4, "n15": 3, "n25": 5, "n50": 10, "n75": 15, "n100": 20}


def connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
        if col not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
    conn.commit()
    return conn


def new_scan(conn, band=None, ppm=None, args=None):
    cur = conn.execute(
        "INSERT INTO scans (started, band, ppm, args) VALUES (?, ?, ?, ?)",
        (now(), band, ppm, args),
    )
    conn.commit()
    return cur.lastrowid


def end_scan(conn, scan_id):
    conn.execute("UPDATE scans SET finished = ? WHERE id = ?", (now(), scan_id))
    conn.commit()


def sib1_identity(sib1):
    """dict with mcc, mnc, plmns, tac, eci, enb_id, cell_id, cgi from a SIB1 JSON dict"""
    info = sib1["cellAccessRelatedInfo"]
    plmns = []
    for p in info["plmn-IdentityList"]:
        ident = p["plmn-Identity"]
        mcc = "".join(str(d) for d in ident.get("mcc", []))
        mnc = "".join(str(d) for d in ident["mnc"])
        plmns.append((mcc, mnc))
    # a PLMN without MCC inherits the previous one (TS 36.331)
    for i in range(1, len(plmns)):
        if not plmns[i][0]:
            plmns[i] = (plmns[i - 1][0], plmns[i][1])
    eci = int(info["cellIdentity"], 2)
    mcc, mnc = plmns[0]
    return {
        "mcc": mcc,
        "mnc": mnc,
        "plmns": " ".join("%s-%s" % p for p in plmns),
        "tac": int(info["trackingAreaCode"], 2),
        "eci": eci,
        "enb_id": eci >> 8,
        "cell_id": eci & 0xFF,
        "cgi": "%s-%s-%d" % (mcc, mnc, eci),
    }


def create_reading(conn, scan_id, earfcn, band=None, dl_freq_mhz=None, location=None,
                   detection="srsue"):
    t = now()
    loc = location or {}
    cur = conn.execute(
        """INSERT INTO readings (scan_id, time, updated, earfcn, band, dl_freq_mhz,
               lat, lon, accuracy_m, location_source, location_time, detection)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (scan_id, t, t, earfcn, band, dl_freq_mhz, loc.get("lat"), loc.get("lon"),
         loc.get("accuracy"), loc.get("source"), loc.get("time"), detection),
    )
    conn.commit()
    return cur.lastrowid


def update_reading(conn, reading_id, **fields):
    if not fields:
        return
    fields["updated"] = now()
    cols = ", ".join("%s = ?" % k for k in fields)
    conn.execute("UPDATE readings SET %s WHERE id = ?" % cols, (*fields.values(), reading_id))
    conn.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--database", default="/vol/output/readings.sqlite")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new-scan")
    p.add_argument("--band", type=int)
    p.add_argument("--ppm", type=float)
    p.add_argument("--args")
    p = sub.add_parser("set-ppm")
    p.add_argument("scan_id", type=int)
    p.add_argument("ppm", type=float)
    p = sub.add_parser("end-scan")
    p.add_argument("scan_id", type=int)
    a = parser.parse_args()
    conn = connect(a.database)
    if a.cmd == "new-scan":
        print(new_scan(conn, a.band, a.ppm, a.args))
    elif a.cmd == "set-ppm":
        conn.execute("UPDATE scans SET ppm = ? WHERE id = ?", (a.ppm, a.scan_id))
        conn.commit()
    else:
        end_scan(conn, a.scan_id)
