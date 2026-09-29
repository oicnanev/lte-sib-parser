#!/usr/bin/env python3
# Current position for a reading. Sources, in order, only one is used:
#   1. gpsd (localhost:2947) with a 2D/3D fix at most MAX_FIX_AGE_S old
#   2. the location file written by the web app: the browser's geolocation
#      (Wi-Fi based) or a position the user set on the map
# Without either, readings are saved without a location.
import datetime
import json
import socket
import time

GPSD = ("127.0.0.1", 2947)
LOCATION_FILE = "/tmp/lte_location.json"


# a fix is used up to this old: a phone relayed over Wi-Fi (GPSd Forwarder) has
# gaps of several seconds; the reading keeps the fix's time
MAX_FIX_AGE_S = 30


def _fix_age(t):
    """seconds since a gpsd ISO time ("2026-09-29T15:59:54.000Z"), or None"""
    try:
        fix = datetime.datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.datetime.now(datetime.timezone.utc) - fix).total_seconds()


def from_gpsd(timeout=1.0):
    """{"lat", "lon", "accuracy", "source": "gpsd", "time"} or None

    Asks gpsd for its last fix (?POLL, answered at once) instead of waiting for
    the next one: with the GPS gone but gpsd running, waiting cost every reading
    its whole timeout (~30 s over a scan)."""
    try:
        s = socket.create_connection(GPSD, timeout=timeout)
    except OSError:
        return None
    try:
        s.settimeout(timeout)
        s.sendall(b'?WATCH={"enable":true};?POLL;\n')
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("class") != "POLL":
                    continue
                for tpv in msg.get("tpv", []):
                    age = _fix_age(tpv.get("time"))
                    if tpv.get("mode", 0) >= 2 and "lat" in tpv and age is not None and age <= MAX_FIX_AGE_S:
                        # eph: horizontal position error estimate (m), not always reported
                        acc = tpv.get("eph") or max(tpv.get("epx", 0), tpv.get("epy", 0)) or None
                        return {"lat": tpv["lat"], "lon": tpv["lon"], "accuracy": acc,
                                "source": "gpsd", "time": tpv.get("time")}
                return None  # no fix, or an old one
    except OSError:
        pass
    finally:
        s.close()
    return None


def from_file(path=LOCATION_FILE):
    try:
        with open(path) as f:
            loc = json.load(f)
        return loc if "lat" in loc and "lon" in loc else None
    except (OSError, ValueError):
        return None


def current(path=LOCATION_FILE):
    return from_gpsd() or from_file(path)


if __name__ == "__main__":
    print(json.dumps(current()))
