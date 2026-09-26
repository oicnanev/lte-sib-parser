#!/usr/bin/env python3
# Current position for a reading. Sources, in order, only one is used:
#   1. gpsd (localhost:2947) with a 2D/3D fix
#   2. the location file written by the web app: the browser's geolocation
#      (Wi-Fi based) or a position the user set on the map
# Without either, readings are saved without a location.
import json
import socket
import time

GPSD = ("127.0.0.1", 2947)
LOCATION_FILE = "/tmp/lte_location.json"


def from_gpsd(timeout=2.0):
    """{"lat", "lon", "accuracy", "source": "gpsd", "time"} or None"""
    try:
        s = socket.create_connection(GPSD, timeout=timeout)
    except OSError:
        return None
    try:
        s.settimeout(timeout)
        s.sendall(b'?WATCH={"enable":true,"json":true}\n')
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("class") == "TPV" and msg.get("mode", 0) >= 2 and "lat" in msg:
                    # eph: horizontal position error estimate (m), not always reported
                    acc = msg.get("eph") or max(msg.get("epx", 0), msg.get("epy", 0)) or None
                    return {"lat": msg["lat"], "lon": msg["lon"], "accuracy": acc,
                            "source": "gpsd", "time": msg.get("time")}
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
