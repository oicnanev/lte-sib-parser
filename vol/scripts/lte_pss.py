#!/usr/bin/env python3
# LTE PSS search on raw HackRF IQ captures, with carrier frequency offset (CFO)
# hypotheses. Used to measure the SDR clock error and to find the exact EARFCN
# of a carrier, which srsRAN cannot do when the offset exceeds a few kHz.
# Needs numpy (python3-numpy).
import os
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor

import numpy as np

FS = 7.68e6  # capture rate: HackRF filters poorly below 8 MSPS
DECIM = 4  # search at 1.92 MSPS: PSS (±465 kHz) + CFO range fit in ±960 kHz
FS_SEARCH = FS / DECIM
N_FFT = int(FS_SEARCH / 15e3)
PSS_ROOTS = [25, 29, 34]


def capture(freq_hz, ms=40, lna=32, vga=20):
    """Record ms milliseconds at freq_hz with hackrf_transfer, return complex samples"""
    n = int(FS * ms / 1000)
    fd, path = tempfile.mkstemp(suffix=".iq")
    os.close(fd)
    try:
        subprocess.run(
            ["hackrf_transfer", "-r", path, "-f", str(int(freq_hz)), "-s", str(int(FS)),
             "-n", str(n), "-l", str(lna), "-g", str(vga)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        raw = np.fromfile(path, dtype=np.int8).astype(np.float32)
    finally:
        os.unlink(path)
    x = raw[0::2] + 1j * raw[1::2]
    # drop the first 10 ms (tuning transient), remove the DC offset
    x = x[int(FS * 0.01):]
    x = x - x.mean()
    # low-pass in the frequency domain, then keep every DECIM-th sample
    X = np.fft.fft(x)
    f = np.fft.fftfreq(len(x), 1 / FS)
    X[np.abs(f) > FS_SEARCH / 2] = 0
    return np.fft.ifft(X)[::DECIM]


def fast_len(n):
    """smallest 2^a 3^b 5^c >= n: FFTs of sizes with a large prime factor are
    many times slower (76928 = 2^7 * 601 took 21 s instead of 3 s)"""
    best = 1 << (n - 1).bit_length()
    p5 = 1
    while p5 < best:
        p35 = p5
        while p35 < best:
            q = p35
            while q < n:
                q *= 2
            best = min(best, q)
            p35 *= 3
        p5 *= 5
    return best


def pss_time(nid2):
    u = PSS_ROOTS[nid2]
    n = np.arange(62)
    d = np.where(n < 31, np.exp(-1j * np.pi * u * n * (n + 1) / 63),
                 np.exp(-1j * np.pi * u * (n + 1) * (n + 2) / 63))
    X = np.zeros(N_FFT, complex)
    X[np.r_[np.arange(-31, 0), np.arange(1, 32)] % N_FFT] = d
    return np.fft.ifft(X)


def _correlate(x, cfo, templates, t):
    """PSS correlation folded over 5 ms half-frames, per N_id_2"""
    L = fast_len(len(x) + N_FFT)
    X = np.fft.fft(x * np.exp(-2j * np.pi * cfo * t), L)
    half = int(FS_SEARCH * 0.005)
    out = []
    for k in range(3):
        c = np.abs(np.fft.ifft(X * templates[k]))[N_FFT:len(x)]
        out.append(c[: len(c) // half * half].reshape(-1, half).sum(axis=0))
    return out


def search(x, cfo_min=-150e3, cfo_max=150e3, step=2.5e3):
    """Best PSS match over CFO hypotheses (PSS only, see detect() for SSS check).

    Returns (ratio, nid2, cfo_hz, pos): ratio is peak / mean of the folded
    correlation, ~5 for noise, 10+ for a real cell. cfo_hz is where the carrier
    appears relative to the tuned frequency, pos the PSS position mod 5 ms.
    """
    def best_at(y, cfos):
        t = np.arange(len(y)) / FS_SEARCH
        templates = [np.fft.fft(np.conj(pss_time(k)[::-1]), fast_len(len(y) + N_FFT)) for k in range(3)]
        best = (0.0, 0, 0.0, 0)
        for cfo in cfos:
            for k, folded in enumerate(_correlate(y, cfo, templates, t)):
                r = folded.max() / folded.mean()
                if r > best[0]:
                    best = (r, k, cfo, int(folded.argmax()))
        return best

    # the coarse grid (121 offsets x 3 PSS) is most of the CPU time: 20 ms (four
    # PSS) is enough to find the offset; the fine search uses all the samples
    coarse = best_at(x[: int(FS_SEARCH * 0.02)], np.arange(cfo_min, cfo_max + step, step))
    return best_at(x, np.arange(coarse[2] - step, coarse[2] + step, step / 10))


### SSS (3GPP TS 36.211 6.11.2) ###

def _mseq(taps):
    x = [0, 0, 0, 0, 1]
    for i in range(26):
        x.append(sum(x[i + k] for k in taps) % 2)
    return 1 - 2 * np.array(x)


_S = _mseq([0, 2])  # x(i+5) = x(i+2) + x(i)
_C = _mseq([0, 3])  # x(i+5) = x(i+3) + x(i)
_Z = _mseq([0, 1, 2, 4])  # x(i+5) = x(i+4) + x(i+2) + x(i+1) + x(i)


def sss_seq(nid1, nid2, subframe5):
    qp = nid1 // 30
    q = (nid1 + qp * (qp + 1) // 2) // 30
    mp = nid1 + q * (q + 1) // 2
    m0 = mp % 31
    m1 = (m0 + mp // 31 + 1) % 31
    n = np.arange(31)
    s0, s1 = _S[(n + m0) % 31], _S[(n + m1) % 31]
    c0, c1 = _C[(n + nid2) % 31], _C[(n + nid2 + 3) % 31]
    z0, z1 = _Z[(n + m0 % 8) % 31], _Z[(n + m1 % 8) % 31]
    d = np.zeros(62)
    if subframe5:
        d[0::2], d[1::2] = s1 * c0, s0 * c1 * z1
    else:
        d[0::2], d[1::2] = s0 * c0, s1 * c1 * z0
    return d


_SSS = None


def _sss_table():
    global _SSS
    if _SSS is None:
        _SSS = {k: np.array([[sss_seq(n1, k, sf) for n1 in range(168)] for sf in (0, 1)])
                for k in range(3)}
    return _SSS


def _subcarriers(sym):
    Y = np.fft.fft(sym)
    return Y[np.r_[np.arange(-31, 0), np.arange(1, 32)] % N_FFT]


def sss_score(x, nid2, cfo, pos):
    """(score, nid1): coherent SSS match at the PSS positions for this CFO.

    score is the normalised correlation, ~1 for a clean cell, ~0.1-0.2 for noise.
    """
    x = x * np.exp(-2j * np.pi * cfo * np.arange(len(x)) / FS_SEARCH)
    half = int(FS_SEARCH * 0.005)
    cp = int(round(FS_SEARCH * 4.6875e-6))  # normal CP of symbols 1-6: 9 samples
    pss_ref = np.fft.fft(pss_time(nid2))[np.r_[np.arange(-31, 0), np.arange(1, 32)] % N_FFT]
    pss_ref /= np.abs(pss_ref)
    table = _sss_table()[nid2]  # [subframe5][nid1][62]
    corrs = []
    # correlation index i = PSS symbol starting at sample i + 1; back off 2 samples into the CP
    start = pos + 1 - 2
    while start + N_FFT <= len(x):
        s0 = start - N_FFT - cp
        if s0 >= 0:
            h = _subcarriers(x[start:start + N_FFT]) * np.conj(pss_ref)  # channel from the PSS
            z = _subcarriers(x[s0:s0 + N_FFT]) * np.conj(h)  # MRC-equalised SSS
            corrs.append((table @ z.real) / (np.abs(h) ** 2).sum())  # [2][168]
        start += half
    if not corrs:
        return 0.0, 0
    # SSS alternates between the subframe 0 and 5 patterns: try both alignments
    best = (0.0, 0)
    for first in (0, 1):
        acc = sum(c[(first + k) % 2] for k, c in enumerate(corrs)) / len(corrs)
        if acc.max() > best[0]:
            best = (float(acc.max()), int(acc.argmax()))
    return best


def detect(x, cfo_min=-150e3, cfo_max=150e3):
    """PSS search plus SSS check of the integer-subcarrier CFO ambiguity.

    PSS (Zadoff-Chu) correlates almost as well at the true CFO +-15/30 kHz;
    only the true CFO also decodes the SSS. Returns a dict with cfo_hz, pci,
    pss_ratio and sss_score (see sss_score).
    """
    best = search(x, cfo_min, cfo_max)
    t = np.arange(len(x)) / FS_SEARCH
    templates = [np.fft.fft(np.conj(pss_time(k)[::-1]), fast_len(len(x) + N_FFT)) for k in range(3)]
    result = None
    for m in (-2, -1, 0, 1, 2):
        cfo0 = best[2] + m * 15e3
        # re-time the PSS at this hypothesis (fine grid +-1.5 kHz)
        cands = []
        for cfo in np.arange(cfo0 - 1.5e3, cfo0 + 1.6e3, 250):
            folded = _correlate(x, cfo, templates, t)[best[1]]
            cands.append((folded.max() / folded.mean(), cfo, int(folded.argmax())))
        ratio, cfo, pos = max(cands)
        score, nid1 = sss_score(x, best[1], cfo, pos)
        if result is None or score > result["sss_score"]:
            result = {"cfo_hz": float(cfo), "pci": 3 * nid1 + best[1],
                      "pss_ratio": float(ratio), "sss_score": float(score)}
    return result


def _decide(first, second, strong, weak):
    """lock rule, see measure()"""
    if second is None:
        return dict(first, locked=True)
    best = max(first, second, key=lambda r: r["sss_score"])
    locked = (best["sss_score"] >= strong or
              (min(first["sss_score"], second["sss_score"]) >= weak and
               abs(first["cfo_hz"] - second["cfo_hz"]) <= 2e3))
    return dict(best, locked=locked)


def measure_many(freqs_hz, lna=32, vga=20, strong=0.6, weak=0.25, workers=None):
    """measure() for several frequencies, analysed on all CPU cores.

    Each frequency gets one 90 ms capture, split into two independent 40 ms
    looks after the tuning transient (starting hackrf_transfer costs ~1 s, the
    signal only 40 ms). The SDR captures one frequency at a time; each capture's
    PSS/SSS searches (~3 s of CPU each) go to a process pool at once and run in
    parallel with the next captures.
    """
    with ProcessPoolExecutor(workers or os.cpu_count()) as pool:
        jobs = []
        for f in freqs_hz:
            x = capture(f, ms=90, lna=lna, vga=vga)
            half = len(x) // 2
            jobs.append((pool.submit(detect, x[:half]), pool.submit(detect, x[half:])))
        results = [(a.result(), b.result()) for a, b in jobs]
    out = []
    for first, second in results:
        if first["sss_score"] >= strong:
            out.append(_decide(first, None, strong, weak))
        else:
            out.append(_decide(first, second, strong, weak))
    return out


def measure(freq_hz, lna=32, vga=20, strong=0.6, weak=0.25):
    """capture + detect at freq_hz; returns the detect() dict plus "locked".

    A lock is accepted when the SSS score reaches `strong`, or when two captures
    both reach `weak` at the same CFO (within 2 kHz): a false lock falls at a
    random CFO in the +-150 kHz search range. The PCI may differ between the two:
    several cells (sectors) are often seen on one carrier. Gain is fixed: more
    gain only lets strong neighbouring carriers eat the 8-bit range.
    """
    return measure_many([freq_hz], lna, vga, strong, weak, workers=1)[0]
