#!/usr/bin/env python3
"""GSM BCCH decoder for wide captures (numpy only).

A wide capture (bladeRF-cli SC16 or hackrf_transfer 8-bit IQ) is split into
200 kHz channels by an FFT filter bank; on each channel: FCCH (frequency
burst) -> SCH (BSIC, frame number) -> BCCH blocks (system information 1-4, 13,
2bis/ter/quater), with a max-log BCJR equaliser and a soft Viterbi decoder.
Used by gsm_scan.py; also runs on a capture file:

  gsm_decode.py capture.iq -f 947.5e6 -r 56e6 -w 50e6 -b 900 [--int8]
"""
import argparse
import json
import os
import shutil
import sys
import time

import numpy as np

R = 1625000 / 6          # GSM symbol rate
SPS = 2
FSC = R * SPS            # channel sample rate
FRAME = 1250 * SPS       # samples per TDMA frame (8 x 156.25 symbols)
HYPER = 2715648          # frames per hyperframe
PLAN_M = 1000            # channel samples per FFT block (small blocks stay in the CPU cache)
FCCH_MAX_HZ = 25e3       # largest carrier frequency error searched (clock error x frequency)

SCH_TRAIN = [int(c) for c in "1011100101100010000001000000111100101101010001010111011000011011"]
TSC = [[int(c) for c in s] for s in (
    "00100101110000100010010111", "00101101110111100010110111",
    "01000011101110100100001110", "01000111101101000100011110",
    "00011010111001000001101011", "01001110101100000100111010",
    "10100111110110001010011111", "11101111000100101110111100")]

# ---------------------------------------------------------------- channelizer


def arfcn_freq(a):
    if 512 <= a <= 885:
        return 1805.2e6 + 0.2e6 * (a - 512)
    if 975 <= a <= 1023:
        return 935.0e6 + 0.2e6 * (a - 1024)
    return 935.0e6 + 0.2e6 * a


def band_arfcns(band):
    if band == 900:
        return list(range(975, 1024)) + list(range(0, 125))
    return list(range(512, 886))


def _plan(fs, target=None):
    from fractions import Fraction
    q = Fraction(int(fs)) / Fraction(1625000 * SPS, 6)
    # B/M = fs/FSC exactly, M about PLAN_M (56 MSPS: B = 86016, M = 832);
    # k a power of two: B must factor into small primes or the FFT is slow
    k = 2 ** max(4, round(np.log2((target or PLAN_M) / q.denominator)))
    return q.numerator * k, q.denominator * k


_CZ = {}


def _cz_blocks(rng):
    c = _CZ
    raw = np.memmap(c["path"], dtype=c["dtype"], mode="r")
    out = np.memmap(c["out"], dtype=np.complex64, mode="r+", shape=c["shape"])
    B, M, S, MS, W = c["B"], c["M"], c["S"], c["MS"], c["W"]
    E = M // 16
    for b in range(*rng):
        s = c["start0"] + b * S
        seg = raw[2 * s:2 * (s + B)].astype(np.float32)
        X = np.fft.fft(seg[0::2] + 1j * seg[1::2]) * c["scale"]
        for i, (c0, idx) in enumerate(c["chans"]):
            # block b starts at b*S: rotate its phase onto the common time base
            y = np.fft.ifft(X[idx] * W) * np.exp(-2j * np.pi * c0 * ((b * S) % B) / B)
            out[i, b * MS:(b + 1) * MS] = y[E:E + MS]
    out.flush()


def channelize(path, fs, fc, arfcns, bw=None, skip_s=0.01, jobs=8, tmp=None, secs=None,
               dtype=np.int16, full_scale=2048):
    """{arfcn: complex64 stream at FSC, 1.0 = ADC full scale} for the ARFCNs
    inside the capture (interleaved I/Q of `dtype`: bladeRF SC16 Q11, full
    scale 2048; HackRF int8, 128). Overlap-save FFT filter bank, blocks shared
    out to `jobs` processes."""
    import multiprocessing as mp
    import os
    import tempfile
    B, M = _plan(fs)
    # overlap-save, 1/8 overlap: the filter's impulse response is ~20 us long
    S, MS = B - B // 8, M - M // 8
    n = os.path.getsize(path) // (2 * np.dtype(dtype).itemsize)
    start0 = int(fs * skip_s)
    if secs:
        n = min(n, start0 + int(fs * secs) + B)
    nblk = (n - start0 - B) // S + 1
    df = fs / B
    af = np.abs(np.fft.fftfreq(M, 1 / (M * df)))       # Hz of each extracted bin
    W = np.where(af < 80e3, 1.0, np.where(af < 130e3, 0.5 + 0.5 * np.cos(np.pi * (af - 80e3) / 50e3), 0.0))
    names, chans = [], []
    for a in arfcns:
        off = arfcn_freq(a) - fc
        if abs(off) > (bw or 0.8 * fs) / 2 - 150e3:
            continue
        c0 = int(round(off / df))
        names.append(a)
        chans.append((c0, (c0 + np.fft.fftfreq(M, 1 / M).astype(int)) % B))
    if not names:
        return {}
    # ~0.5 GB per 100 channels and second: a file (page cache), not the heap;
    # /dev/shm only when it has room (Docker's default is 64 MB: writing past
    # it kills the worker processes and the pool hangs)
    need = len(names) * nblk * MS * 8
    if tmp is None and os.path.isdir("/dev/shm") and shutil.disk_usage("/dev/shm").free > need * 1.2:
        tmp = "/dev/shm"
    fd, outp = tempfile.mkstemp(dir=tmp, suffix=".cz")
    os.close(fd)
    shape = (len(names), nblk * MS)
    np.memmap(outp, dtype=np.complex64, mode="w+", shape=shape).flush()
    _CZ.update(path=path, dtype=dtype, scale=M / B / full_scale, out=outp, shape=shape,
               B=B, M=M, S=S, MS=MS, W=W, start0=start0, chans=chans)
    step = -(-nblk // jobs)
    with mp.get_context("fork").Pool(jobs) as pool:
        pool.map(_cz_blocks, [(i, min(nblk, i + step)) for i in range(0, nblk, step)])
    out = np.memmap(outp, dtype=np.complex64, mode="r", shape=shape)
    os.unlink(outp)          # the mapping keeps the data until it is dropped
    return {a: out[i] for i, a in enumerate(names)}

# ---------------------------------------------------------------- burst level


_ROT = {}


def _rot(n, cyc):
    """exp(-2j pi cyc k), k < n (cached: the same lengths come back per channel)"""
    key = (n, cyc)
    if key not in _ROT:
        _ROT[key] = np.exp(-2j * np.pi * cyc * np.arange(n)).astype(np.complex64)
    return _ROT[key]


def find_fcch(z):
    """start samples of FCCH bursts and the frequency offset (Hz)"""
    n = np.arange(len(z))
    w = z * _rot(len(z), (R / 4) / FSC)
    # differential: a tone at any small offset gives a constant w[n] w*[n-1]
    L = 250
    d = w[1:] * np.conj(w[:-1])
    cs = np.concatenate(([0], np.cumsum(d)))
    ce = np.concatenate(([0], np.cumsum(np.abs(d))))
    coh = np.real(cs[L:] - cs[:-L]) / (ce[L:] - ce[:-L] + 1e-12)
    hits = []
    i = 0
    while True:
        cand = np.nonzero(coh[i:] > 0.85)[0]
        if not len(cand):
            break
        j = i + cand[0]
        seg = coh[j:j + 400]
        top = seg.max()
        plateau = np.nonzero(seg > top * 0.97)[0]
        mid = j + int(plateau.mean())
        start = mid + L // 2 - 148 * SPS // 2   # burst centre -> burst start
        hits.append(start)
        i = j + 4000
    hits = fcch_pattern(hits)
    # frequency offset: FFT peak of the tone around each FCCH (robust to a
    # misplaced window, unlike the phase of w[n] w*[n-1])
    NF = 8192
    offs = []
    for s in hits:
        seg = w[max(0, s - 150):s + 148 * SPS + 150]
        F = np.abs(np.fft.fft(seg, NF))
        fr = np.fft.fftfreq(NF, 1 / FSC)
        ok = np.abs(fr) < FCCH_MAX_HZ
        offs.append(fr[ok][np.argmax(F[ok])])
    if not offs:
        return [], 0.0
    df = float(np.median(offs))
    # burst start: coherent sum of the corrected tone over one burst
    v = w * np.exp(-2j * np.pi * df / FSC * n).astype(np.complex64)
    cv = np.concatenate(([0], np.cumsum(v)))
    Lb = 148 * SPS
    starts = []
    for s in hits:
        lo, hi = max(0, s - 150), min(len(v) - Lb, s + 150)
        if hi <= lo:
            continue
        k = np.arange(lo, hi)
        starts.append(int(k[np.argmax(np.abs(cv[k + Lb] - cv[k]))]))
    return starts, df


_FCCH_K = (10, 11, 20, 21, 30, 31, 40, 41, 51)


def fcch_pattern(hits, tol=40, frac=0.4):
    """hits with another FCCH-like hit 10, 11, 20, 21 ... frames away (FCCH
    frames 0 10 20 30 40 of each 51-multiframe). Other carriers (LTE, noise)
    give tone-like hits too, at random distances: when most hits have no such
    partner, the channel is not a BCCH."""
    ok = set()
    for i in range(len(hits)):
        for j in range(i + 1, len(hits)):
            d = hits[j] - hits[i]
            k = round(d / FRAME)
            if k > 51:
                break
            if abs(d - k * FRAME) <= tol and k in _FCCH_K:
                ok.update((i, j))
    if len(ok) < 3 or len(ok) < frac * len(hits):
        return []
    return [hits[i] for i in sorted(ok)]


def derotate(z, df):
    n = np.arange(len(z), dtype=np.float64)
    # remove the frequency offset and the GMSK pi/2 per symbol rotation
    return z * _rot(len(z), 1 / (4 * SPS)) * np.exp(-2j * np.pi * df / FSC * n).astype(np.complex64)


LTAP, DLY = 5, 2
_ST = np.arange(16)
_SYM = lambda b: 1 - 2 * b  # noqa: E731


_EST = {}


def _est_matrix(train, tpos):
    """rows: known symbols for r[n], n in obs; r[n] = sum h_k s[n+DLY-k]"""
    key = (tuple(train), tpos)
    if key not in _EST:
        sy = _SYM(np.array(train, float))
        obs = np.arange(tpos + DLY, tpos + len(train) - (LTAP - 1 - DLY))
        A = np.array([[sy[n + DLY - k - tpos] for k in range(LTAP)] for n in obs])
        _EST[key] = (obs, A, np.linalg.pinv(A))
    return _EST[key]


_P = np.stack((_ST >> 1, (_ST >> 1) | 8), axis=1)      # predecessors [ns, x]
_OLD = np.arange(16)
_SUCC = np.stack(((_OLD << 1) & 15, ((_OLD << 1) & 15) | 1), axis=1)   # [old, u]
_XOLD = _OLD >> 3                                                  # x of old in its successors
_SYMTAB = np.array([[[_SYM((((ns >> 1) | (x << 3)) >> j) & 1) if j >= 0 else _SYM(ns & 1)
                      for j in range(-1, 4)] for x in range(2)] for ns in range(16)], float)  # [ns, x, tap]


def mlse(r, h, sigma2):
    """max-log BCJR equaliser, batched: r [nb, 148], h [nb, LTAP], sigma2 [nb];
    r[n] = sum h_k s[n+DLY-k]. Returns soft bits [nb, 148], > 0 means bit 0."""
    nb = r.shape[0]
    E = np.einsum("nxk,bk->bnx", _SYMTAB, h)                         # [nb, 16, 2]
    G = np.zeros((nb, 148, 16, 2))
    G[:, DLY:] = np.abs(r[:, :148 - DLY, None, None] - E[:, None]) ** 2 / sigma2[:, None, None, None]
    A = np.empty((nb, 149, 16))
    A[:, 0] = 0
    for m in range(148):
        A[:, m + 1] = np.min(A[:, m][:, _P] + G[:, m], axis=2)
        A[:, m + 1] -= A[:, m + 1].min(axis=1, keepdims=True)
    Bk = np.zeros((nb, 16))
    llr = np.empty((nb, 148))
    for m in range(147, -1, -1):
        tot = A[:, m][:, _P] + G[:, m] + Bk[:, :, None]                # [nb, ns, x]
        best = tot.min(axis=2)
        llr[:, m] = best[:, 1::2].min(axis=1) - best[:, 0::2].min(axis=1)
        # beta(m-1)[old] = min over its successors ns (with x = old >> 3)
        cand = G[:, m][:, _SUCC, _XOLD[:, None]] + Bk[:, _SUCC]       # [nb, old, u]
        Bk = cand.min(axis=2)
        Bk -= Bk.min(axis=1, keepdims=True)
    return llr


def demod(z, starts, train, tpos, search=6):
    """bursts near each start: timing by least squares residual on the
    training sequence (all offsets at once), then the batched equaliser.
    -> list of (soft bits, residual, start) or None per start"""
    obs, A, P = _est_matrix(train, tpos)
    offs = np.arange(-search, search + 1)
    rs, hs, sg, meta = [], [], [], []
    out = [None] * len(starts)
    for j, start in enumerate(starts):
        o = offs[(start + offs >= 0) & (start + offs + 150 * SPS < len(z))]
        if not len(o):
            continue
        Y = z[(start + o)[:, None] + SPS * obs[None, :]]           # [offset, obs]
        H = Y @ P.T
        res = np.sum(np.abs(Y - H @ A.T) ** 2, axis=1) / (np.sum(np.abs(Y) ** 2, axis=1) + 1e-12)
        i = int(np.argmin(res))
        s0 = int(start + o[i])
        r = z[s0:s0 + 148 * SPS:SPS]
        rs.append(r)
        hs.append(H[i])
        sg.append(max(res[i] * np.mean(np.abs(r) ** 2), 1e-9))
        meta.append((j, float(res[i]), s0))
    if rs:
        soft = mlse(np.array(rs), np.array(hs), np.array(sg))
        for k, (j, res, s0) in enumerate(meta):
            out[j] = (soft[k], res, s0)
    return out

# ---------------------------------------------------------------- channel coding


_CV_OUT = np.empty((16, 2, 2), np.int8)
for _st in range(16):
    for _u in range(2):
        _CV_OUT[_st, _u] = (_u ^ ((_st >> 2) & 1) ^ ((_st >> 3) & 1),
                            _u ^ (_st & 1) ^ ((_st >> 2) & 1) ^ ((_st >> 3) & 1))
_CV_NS = np.arange(16)
_CV_OLD = np.stack((_CV_NS >> 1, (_CV_NS >> 1) | 8))          # [x, ns]
_CV_SGN = 1 - 2 * _CV_OUT[_CV_OLD, (_CV_NS & 1)[None, :]]      # [x, ns, 2]: +1 for coded 0


def conv_decode(c):
    """soft Viterbi, rate 1/2 K=5 (G0 = 1+D3+D4, G1 = 1+D+D3+D4), ends in state 0.
    c: soft coded bits, > 0 means 0."""
    n = len(c) // 2
    metric = np.full(16, 1e9)
    metric[0] = 0
    bp = np.empty((n, 16), np.int8)
    for k in range(n):
        cand = metric[_CV_OLD] - (_CV_SGN[..., 0] * c[2 * k] + _CV_SGN[..., 1] * c[2 * k + 1])
        pick = cand[1] < cand[0]
        bp[k] = pick
        metric = np.where(pick, cand[1], cand[0])
        metric -= metric.min()
    st = 0
    u = np.empty(n, np.uint8)
    for k in range(n - 1, -1, -1):
        u[k] = st & 1
        st = (st >> 1) | (int(bp[k, st]) << 3)
    return u, metric[0]


def crc(bits, poly, nbits):
    reg = 0
    mask = (1 << nbits) - 1
    for b in bits:
        fb = ((reg >> (nbits - 1)) & 1) ^ int(b)
        reg = (reg << 1) & mask
        if fb:
            reg ^= poly
    return [(reg >> (nbits - 1 - i)) & 1 for i in range(nbits)]


SCH_POLY = (1 << 8) | (1 << 6) | (1 << 5) | (1 << 4) | (1 << 2) | 1          # D^10 omitted
FIRE_POLY = (1 << 26) | (1 << 23) | (1 << 17) | (1 << 3) | 1                  # D^40 omitted


def parity_ok(u, ninfo, poly, np_):
    p = crc(u[:ninfo], poly, np_)
    return all((1 - p[i]) == u[ninfo + i] for i in range(np_))


def pack_lsb(bits):
    out = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def decode_sch(bits):
    c = np.concatenate((bits[3:42], bits[106:145]))
    u, _ = conv_decode(c)
    if not parity_ok(u, 25, SCH_POLY, 10):
        return None
    d = pack_lsb(u[:25])
    bsic = (d[0] >> 2) & 0x3f
    t1 = ((d[0] & 3) << 9) | (d[1] << 1) | (d[2] >> 7)
    t2 = (d[2] >> 2) & 0x1f
    t3 = (((d[2] & 3) << 1) | (d[3] & 1)) * 10 + 1
    fn = 51 * ((t3 - t2) % 26) + t3 + 51 * 26 * t1
    return {"bsic": bsic, "fn": fn}


_IL = [(k % 4, 2 * ((49 * k) % 57) + ((k % 8) // 4)) for k in range(456)]


def decode_xcch(bursts):
    """4 bursts of 148 bits -> 23 octets, or None"""
    i = [np.concatenate((b[3:60], b[88:145])) for b in bursts]
    c = np.array([i[B][j] for B, j in _IL])
    u, _ = conv_decode(c)
    if not parity_ok(u, 184, FIRE_POLY, 40):
        return None
    return pack_lsb(u[:184])

# ---------------------------------------------------------------- layer 3

SI_NAMES = {0x19: "si1", 0x1a: "si2", 0x1b: "si3", 0x1c: "si4", 0x02: "si2bis",
            0x03: "si2ter", 0x07: "si2quater", 0x00: "si13"}


def lai(o):
    mcc = "%d%d%d" % (o[0] & 15, o[0] >> 4, o[1] & 15)
    mnc = "%d%d" % (o[2] & 15, o[2] >> 4)
    if (o[1] >> 4) != 15:
        mnc += "%d" % (o[1] >> 4)
    return {"mcc": mcc, "mnc": mnc, "lac": (o[3] << 8) | o[4]}


def bitmap0(o):
    """frequency list, bit map 0 format (ARFCN 1-124), or None for other formats"""
    if o[0] >> 6 != 0:
        return None
    out = []
    for a in range(1, 125):
        byte = 15 - (a - 1) // 8
        if o[byte] >> ((a - 1) % 8) & 1:
            out.append(a)
    return out


def parse_si(msg):
    if len(msg) < 3 or msg[1] != 0x06:
        return None, None
    t = msg[2]
    name = SI_NAMES.get(t, "rr_%02x" % t)
    info = {"hex": msg.hex()}
    body = msg[3:]
    if t == 0x1b:
        info["ci"] = (body[0] << 8) | body[1]
        info.update(lai(body[2:7]))
    elif t == 0x1c:
        info.update(lai(body[0:5]))
    elif t in (0x1a, 0x19):
        info["arfcns"] = bitmap0(body[0:16])
    return name, info

# ---------------------------------------------------------------- per channel


def decode_channel(z):
    """FCCH -> SCH -> BCCH on one channel stream -> dict (bsic and si when decoded)"""
    t0 = time.time()
    hits, df = find_fcch(z)
    res = {"fcch": len(hits), "df_hz": round(df),
           "level_dbfs": round(10 * np.log10(np.mean(np.abs(z) ** 2) + 1e-20), 1)}
    if not hits:
        return res
    y = derotate(z, df)
    # two SCH that agree (BSIC and frame number vs. time): a 10-bit parity
    # alone passes by chance on a non-GSM carrier
    sch = None
    got = []
    for dm in demod(y, [h + FRAME for h in hits[:12]], SCH_TRAIN, 42, search=12):
        if dm is None:
            continue
        bits, rr, s0 = dm
        d = decode_sch(bits)
        if not d:
            continue
        for d1, s1 in got:
            if d1["bsic"] == d["bsic"] and (d["fn"] - d1["fn"]) % HYPER == round((s0 - s1) / FRAME) % HYPER:
                sch = (d, s0)
        if sch:
            break
        got.append((d, s0))
    if not sch:
        res["sch"] = False
        return res
    d, s0 = sch
    res["bsic"] = d["bsic"]
    tsc = TSC[d["bsic"] & 7]
    fn0 = d["fn"]
    # every multiframe in the capture: BCCH on frames 2..5 (FN mod 51)
    first = fn0 - (fn0 % 51) - 51 * ((s0 // FRAME) // 51 + 1)
    blocks = []
    for base in range(first, first + 51 * 40, 51):
        starts = [s0 + (base + f - fn0) * FRAME for f in (2, 3, 4, 5)]
        if starts[0] >= 20 and starts[-1] + 160 * SPS <= len(y):
            blocks.append((base, starts))
    dms = demod(y, [st for _, sts in blocks for st in sts], tsc, 61, search=4)
    si = {}
    for k, (base, _) in enumerate(blocks):
        four = dms[4 * k:4 * k + 4]
        if any(dm is None for dm in four):
            continue
        msg = decode_xcch([dm[0] for dm in four])
        if msg is None:
            continue
        name, info = parse_si(msg)
        if name and name not in si:
            info["tc"] = (base // 51) % 8
            si[name] = info
    res["si"] = si
    res["secs"] = round(time.time() - t0, 2)
    return res


CHANS = {}


def _fcch_count(ar):
    return ar, len(find_fcch(CHANS[ar])[0])


def _decode_one(ar):
    return ar, decode_channel(CHANS[ar])


def decode_capture_cpp(path, fs, fc, arfcns, bw=None, jobs=8, secs=None, presearch=0.4,
                       dtype=np.int16, log=None):
    """the same with gsm_decoder (C++, in the image): ~5x faster. Its SI
    messages come as hex and are parsed here."""
    import subprocess
    cmd = ["gsm_decoder", "-i", path, "-f", repr(fc), "-r", repr(fs), "-w", repr(bw or 0.8 * fs),
           "-a", " ".join(map(str, arfcns)), "-j", str(jobs), "--presearch", repr(presearch or 0),
           "--fcch-max-hz", repr(FCCH_MAX_HZ)]
    if secs:
        cmd += ["--secs", repr(secs)]
    if np.dtype(dtype) == np.int8:
        cmd.append("--int8")
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if log:
        for line in p.stderr.splitlines():
            log(line)
    if p.returncode:
        raise RuntimeError("gsm_decoder failed (%d)" % p.returncode)
    out = []
    for line in p.stdout.splitlines():
        r = json.loads(line)
        si = {}
        for m in r.pop("msgs"):
            name, info = parse_si(bytes.fromhex(m["hex"]))
            if name and name not in si:
                info["tc"] = m["tc"]
                si[name] = info
        r["si"] = si
        out.append(r)
    return out


def decode_capture(path, fs, fc, arfcns, bw=None, jobs=8, secs=None, presearch=0.4,
                   dtype=np.int16, full_scale=2048, log=None):
    """decode every BCCH among `arfcns` in a capture file -> list of result
    dicts (with "arfcn"), in the order they finish. Uses gsm_decoder (C++)
    when it is installed, unless LTE_GSM_NUMPY is set."""
    if shutil.which("gsm_decoder") and not os.environ.get("LTE_GSM_NUMPY"):
        return decode_capture_cpp(path, fs, fc, arfcns, bw, jobs, secs, presearch, dtype, log)
    import multiprocessing as mp
    global CHANS
    t = time.time()
    kw = dict(bw=bw, jobs=jobs, dtype=dtype, full_scale=full_scale)
    if presearch and len(arfcns) > 8:
        # FCCH look on a short part first: most channels carry no BCCH
        CHANS = channelize(path, fs, fc, arfcns, secs=presearch, **kw)
        with mp.get_context("fork").Pool(jobs) as pool:
            found = [ar for ar, n in pool.imap(_fcch_count, list(CHANS)) if n >= 3]
        if log:
            log("FCCH on %d of %d channels (%.1f s)" % (len(found), len(CHANS), time.time() - t))
        arfcns = found
    CHANS = channelize(path, fs, fc, arfcns, secs=secs, **kw) if arfcns else {}
    out = []
    with mp.get_context("fork").Pool(jobs) as pool:
        for ar, r in pool.imap_unordered(_decode_one, list(CHANS)):
            r["arfcn"] = ar
            out.append(r)
    CHANS = {}
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("iq")
    p.add_argument("-f", "--fc", type=float, required=True)
    p.add_argument("-r", "--rate", type=float, default=56e6)
    p.add_argument("-b", "--band", type=int, default=900)
    p.add_argument("-a", "--arfcn", type=int, nargs="*")
    p.add_argument("-j", "--jobs", type=int, default=os.cpu_count())
    p.add_argument("-w", "--bw", type=float, help="analog bandwidth (default 0.8 x rate)")
    p.add_argument("--secs", type=float, help="use only this much of the capture")
    p.add_argument("--presearch", type=float, default=0.4, help="seconds for the FCCH presearch (0: off)")
    p.add_argument("--int8", action="store_true", help="HackRF 8-bit samples (default bladeRF SC16)")
    a = p.parse_args()
    t = time.time()
    res = decode_capture(a.iq, a.rate, a.fc, a.arfcn or band_arfcns(a.band), a.bw, a.jobs, a.secs,
                         a.presearch, np.int8 if a.int8 else np.int16, 128 if a.int8 else 2048,
                         log=lambda m: print(m, file=sys.stderr))
    for r in sorted(res, key=lambda r: arfcn_freq(r["arfcn"])):
        if r.get("bsic") is not None:
            print(json.dumps(r), flush=True)
    print("%.1f s" % (time.time() - t), file=sys.stderr)


if __name__ == "__main__":
    main()
