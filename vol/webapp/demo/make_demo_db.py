#!/usr/bin/env python3
# Build a readings database with FICTITIOUS data, for web app screenshots and demos.
# Cells use the 3GPP test network PLMN 001-01; PCIs, TACs and cell IDs are made up;
# positions follow a walk around a public square in Lisbon.
#
#   python3 make_demo_db.py /vol/output/demo.sqlite
#   python3 /vol/webapp/server.py --port 8081 --db /vol/output/demo.sqlite \
#       --learned /vol/output/demo_learned.json
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
import readings_db  # noqa: E402

path = sys.argv[1] if len(sys.argv) > 1 else "demo.sqlite"
if os.path.exists(path):
    os.unlink(path)
random.seed(7)

# (earfcn, band, DL MHz, bandwidth MHz, decoded?)
CARRIERS = [(6200, "20", 796.0, 10, True), (6300, "20", 806.0, 10, True), (6400, "20", 816.0, 10, True),
            (3475, "8", 927.5, 5, True), (1700, "3", 1855.0, 20, False), (1875, "3", 1872.5, 10, True),
            (500, "1", 2160.0, 20, False), (2800, "7", 2625.0, 10, True)]
# a walk around Praça do Comércio, Lisbon (public place)
ROUTE = [(38.70755, -9.13660), (38.70815, -9.13590), (38.70870, -9.13700),
         (38.70800, -9.13780), (38.70720, -9.13720)]
MIB_BW = {1.4: "n6", 3: "n15", 5: "n25", 10: "n50", 15: "n75", 20: "n100"}

conn = readings_db.connect(path)
t0 = datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc)
for n, (lat, lon) in enumerate(ROUTE):
    start = t0 + timedelta(minutes=12 * n)
    scan = readings_db.new_scan(conn, None, 20.4, "-K (demo)")
    conn.execute("UPDATE scans SET started = ?, finished = ? WHERE id = ?",
                 (start.isoformat(timespec="seconds"),
                  (start + timedelta(seconds=300 + 20 * n)).isoformat(timespec="seconds"), scan))
    for k, (e, band, freq, bw, decoded) in enumerate(CARRIERS):
        if random.random() < 0.2:
            continue  # not every carrier is heard everywhere
        t = (start + timedelta(seconds=40 + 30 * k)).isoformat(timespec="seconds")
        loc = {"lat": lat + random.uniform(-4e-5, 4e-5), "lon": lon + random.uniform(-4e-5, 4e-5),
               "accuracy": 5.0, "source": "gpsd", "time": t}
        rid = readings_db.create_reading(conn, scan, e, band, freq, loc,
                                         detection="srsue" if decoded else "pss")
        pci = 3 * (40 + 7 * k) + (n % 3)
        fields = {"pci": pci, "bandwidth_mhz": bw, "time": t, "updated": t}
        if decoded:
            eci = (1000 + 11 * k) << 8 | (10 + n)
            sib1 = {"cellAccessRelatedInfo": {
                "plmn-IdentityList": [{"plmn-Identity": {"mcc": [0, 0, 1], "mnc": [0, 1]},
                                       "cellReservedForOperatorUse": "notReserved"}],
                "trackingAreaCode": format(100 + k, "016b"), "cellIdentity": format(eci, "028b"),
                "cellBarred": "notBarred"}, "note": "fictitious demo data"}
            fields.update(readings_db.sib1_identity(sib1))
            fields.update({
                "rsrp": round(random.uniform(-112, -78), 1),
                "mib": json.dumps({"dl-Bandwidth": MIB_BW[bw], "note": "fictitious demo data"}),
                "sib1": json.dumps(sib1),
                "sib2": json.dumps({"note": "fictitious demo data"}),
                "sib3": json.dumps({"note": "fictitious demo data"}),
                "sib5": json.dumps({"interFreqCarrierFreqList": [{"dl-CarrierFreq": c[0]} for c in CARRIERS
                                                                  if c[0] != e][:4]}),
            })
        readings_db.update_reading(conn, rid, **fields)
        conn.execute("UPDATE readings SET updated = ? WHERE id = ?", (t, rid))
conn.commit()
print("demo database:", path)
