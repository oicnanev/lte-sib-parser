# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project overview

LTE cell scanner + MIB/SIB parser. Combines srsRAN's `cell_search` with a patched
`srsue` (TX disabled by `worker/rx_only.patch`) to passively decode broadcast system information from LTE
cells and store it in SQLite.

Scan logic (driven by `vol/sib-scan.sh`):
1. `cell_search` sweeps an EARFCN range.
2. When a cell is found, remember where the search stopped.
3. Launch `srsue` on that cell and parse SIBs until a timeout.
4. If SIB5 is decoded, recursively scan the neighbouring EARFCNs it lists.
5. When no SIB5 neighbours remain, resume `cell_search` where it stopped.
6. All MIBs/SIBs are saved to SQLite (default `/vol/output/cells.sqlite`).

## Layout

- `worker/Dockerfile` — Ubuntu 22.04 image; builds srsRAN_4G at a pinned commit
  (`ec29b0c1…`) with `sib_logger.patch` and `rx_only.patch` applied; installs UHD/Soapy/LimeSDR drivers.
- `worker/sib_logger.patch` — large patch on srsRAN_4G that makes srsue log
  decoded SIBs as JSON (`[I] Content: {...}`) and power measurements
  (`[powermeasure] {...}`). Despite the upstream README, it does NOT disable TX.
- `worker/rx_only.patch` — makes srsue's radio RX-only (no TX, no TX retune).
- `vol/` — mounted at `/vol` in the container.
  - `sib-scan.sh` — main entry point / orchestrator (bash).
  - `scripts/` — Python helpers used by the scan: `parse_save_sib.py`
    (reads srsue log, extracts JSON, writes SQLite), `get_neigh.py`,
    `earfcn_to_band.py`, `band_to_earfcn.py`, `earfcn_to_freq.py`,
    `sweep_candidates.py` (HackRF sweep carrier finder), `has_mib.py`,
    `lte_pss.py` (PSS/SSS search), `calibrate_ppm.py`.
  - `dbparsers/` — Python tools to inspect results (`list-cells.py`,
    `get-info.py`, `get-sib.py`, `get-arfcns.py`).
  - `helpers/ue.conf` — srsue config; `helpers/lte_bands.sqlite3` — band/EARFCN table;
    `helpers/uhd_images/` — optional custom FPGA images (e.g. B210 clones).
  - `output/` — scan results (git-ignored): `cells.sqlite` (legacy, one row
    per EARFCN) and `readings.sqlite` (tables `scans`, `readings`: every
    reading with location, PCI, CGI, RSRP, MIB/SIBs; see `readings_db.py`).
  - `webapp/` — local web app: `server.py` (stdlib, SSE, 127.0.0.1:8080) and
    `static/` (index.html, app.js, style.css; Leaflet from cdnjs, OSM tiles).
    Location: gpsd (`scripts/location.py`) > browser geolocation > manual map
    position; effective position written to `/tmp/lte_location.json`.
- `run.sh` — runs the built image interactively with USB/X11 access.
- `docker-compose.yml` — `worker` (shell: `docker compose run --rm worker`) and
  `webapp` (`docker compose up webapp`, host network, `init: true`); both share
  the image and the `srsran-home` volume (srsue FFTW wisdom in /root).

## Build & run

```bash
docker compose build
./run.sh                       # enter container (SDR connected)
./sib-scan.sh -h               # inside container
./sib-scan.sh -d soapy -a "rxant=LNAW" -b 3
python3 dbparsers/list-cells.py -d ./output/cells.sqlite
```

Requires real SDR hardware (LimeSDR via Soapy, USRP via UHD, bladeRF) — scans
cannot be tested end-to-end without it.

## Conventions

- **Every change and design decision goes into README.md** (section "Changes
  and design decisions" + affected usage docs), in the same commit. README is
  in English and lists per-distro install commands. Talk to the user in PT-PT.

- Python 3 scripts, plain stdlib (`sqlite3`, `json`), no package structure;
  exception: `lte_pss.py` and its users need numpy (in the image).
- Bash for orchestration.
- srsRAN changes go into patch files in `worker/`, not a vendored tree
  (`srsRAN_4G/` is git-ignored).

## User hardware

- **HackRF One** (primary): used via Soapy (`-d soapy -a "driver=hackrf"`).
  Max 20 MSPS → SIB decoding works for cells up to 10 MHz (15.36 MSPS with
  `lte_sample_rates`); 15/20 MHz cells only show up in the sweep.
  8-bit ADC → gain tuning matters.
- **bladeRF 2.0 micro xA5** (at the user's workplace, 2026-09-28): serial
  51ba89f7…, firmware v2.6.0 / FPGA v0.16.0 (= Nuand release 2025.10, latest;
  FPGA autoloads from flash), USB 3. Used with `-d bladeRF` (native srsRAN
  plugin, needs libbladeRF 2.6.0 from source + `worker/bladerf_rx.patch`).
  Clock ~1 ppm. Gain 30 (B20/B8) / 40 (B3/B1/B7) where signal is strong; 40
  saturates B20 there. Antenna must be on **RX1** (wideband one; a 1.4 GHz
  antenna there hurt B3/B7). Decodes 20 MHz cells (EARFCN 500, PRB 100).
  AGC is on by default: `bladeRF-cli` needs `set agc rx off` before `set gain`.
  TX module is never enabled (bladerf_rx.patch). Since 2026-09-29 a second
  identical antenna is on RX2: lte_sib_decoder -A 2 / sib-scan -A 2 / web app
  "RX antennas" (README 55); srsue, bladeRF-cli checks and 2G use RX1 only.
  Sample-rate changes are slow (1.92 MSPS 1.56 s, full cycle ~4 s): sib-scan
  uses `--rf.srate 30.72e6` by default for -d bladeRF (plugin reads in chunks,
  bladerf_rx.patch). srsue start-up ~9 s per cell regardless of SDR. With the
  user's Cisco 4G-LTE-ANTM-D on RX1: gain 15 (<1 GHz) / 40 (≥1 GHz).
- RTL-SDR (RTL2838): only useful for `cell_search`/MIB (≤2.4 MSPS, ≤1.75 GHz).
  DVB kernel modules are blacklisted on the host.
- Host: Arch Linux; bands of interest: B20, B8, B3, B1, B7.
- Repo is a public fork (origin = oicnanev/lte-sib-parser, upstream =
  godfuzz3r/lte-sib-parser). Never commit scan results, PCIs, TACs or other
  location-identifying data.

## HackRF findings (2026-09-26)

- **TX was never disabled** by `sib_logger.patch` (it only changes log levels);
  srsue attempts RRC connection / PRACH. `worker/rx_only.patch` makes
  `radio::tx`, `tx_end`, `set_tx_freq`, `set_tx_gain` no-ops. Keep it.
- SoapyHackRF has one shared LO: `set_tx_freq` retuned the radio to the UL
  frequency → no cells seen. Fixed by `rx_only.patch`.
- The user's HackRF clock is ~**-20 ppm** (LO low; drifts 19.2-21.6 with
  temperature): `sib-scan.sh -p <ppm>` (positive tunes higher) or `-p auto`.
  sib-scan converts ppm to `--rf.freq_offset` per EARFCN; `cell_search` gets
  `-p` via `worker/cell_search_ppm.patch`. srsRAN PSS search tolerates only a
  few kHz CFO.
- PSS/SSS lock (lte_pss.measure): SSS ≥0.6, or two captures ≥0.25 at the same
  CFO ±2 kHz. Real B20 cells score only 0.3-0.6; more capture gain is worse.
- Gain: `-g 56` on B20; B3 (1.8 GHz) needs `-g 70` (at 56 srsue sees PSS at
  PSR ~2.4 but never locks). `-r 15.36e6` makes no difference.
- `cell_search` (C example) is unreliable with HackRF even with `-p`: finds
  cells ~1 in 5 tries, on the wrong EARFCN, with garbage ID/PRB (it restarts the
  stream per EARFCN at 1.92 MSPS). Use sweep mode instead.
- **`vol/scripts/lte_pss.py`** (numpy): captures 40 ms with `hackrf_transfer` at
  7.68 MSPS, decimates to 1.92, PSS search over CFO ±150 kHz, then **SSS**
  check at the true CFO ±15/30 kHz — PSS (Zadoff-Chu) alone can't tell an
  integer-subcarrier CFO alias from the truth (peaks within 1%). SSS score
  ~0.4-0.9 for real cells, ~0.1-0.15 noise. Also gives the PCI.
- **`calibrate_ppm.py -b <band>`**: sweeps, measures the strongest carriers,
  assumes the raster error (k×100 kHz) giving the smallest |ppm|, prints the
  median. Unambiguous while |ppm|·f < 50 kHz (prefer B20/B8).
- **Sweep mode** `sib-scan.sh -S -b <band>`: `sweep_candidates.py` runs
  `hackrf_sweep` (25 kHz bins, band ±5 MHz for the noise floor), finds LTE
  blocks by half-level edges (centre only ±100 kHz accurate). With numpy it
  `--refine`s each carrier with PSS/SSS to the exact EARFCN and drops blocks
  without LTE sync (GSM/NR); without numpy it prints the 3 closest EARFCNs and
  sib-scan skips ±2 neighbours once `has_mib.py` sees a decoded MIB.
  Blocks closer than 0.5 MHz are merged first: lightly loaded cells have holes
  in their spectrum. Calibration prefers blocks whose width matches a standard
  LTE bandwidth.
- Working command (inside container):
  `./sib-scan.sh -S -p auto -d soapy -a "driver=hackrf" -g 56 -b 20 -n`
  → B20: calibration + 3 carriers (MIB + SIBs) in ~10 min, mostly srsue SIB
  collection (each new SIB extends the timeout by `-T`).
- `parse_save_sib.py` used to busy-wait on the srsue log (100% CPU, starving
  srsue); it now sleeps 50 ms when there is no new line.
- Debug helpers: `vol/helpers/srsue-debug.sh <earfcn> <gain> [srsue args]` runs srsue
  15 s with verbose logs; raw IQ via `hackrf_transfer` for offline PSS/CFO checks.
- A fresh container's first srsue run takes >15 s to start (FFTW wisdom).

## Current work / plan

1. ✅ HackRF receiving and decoding SIBs on B20.
2. ✅ Band scanning with HackRF via sweep mode (`-S`).
3. ✅ Automatic ppm calibration (`-p auto`) and exact EARFCN/PCI via PSS/SSS.
4. ✅ B3 end to end (`-S -p auto -g 70 -b 3`, ~6 min): 10 MHz carrier → MIB +
   SIB1-5,7. 20 MHz carrier: srsue finds it (PRB=100) and decodes the MIB in
   manual runs, but in the pipeline no MIB within 30 s — intermittent; not
   worth fixing on HackRF (no SIBs possible), bladeRF covers it.
5. ✅ Speed: `-T` is now an idle timeout (last new MIB/SIB + T) and the early
   stop works (RSRP was never marked received) → ~70 s per cell instead of ~3 min.
6. ✅ README: HackRF section, TX note.
7. ✅ 2026-09-28: bladeRF 2.0 micro xA5 works (it was not a 1st-gen board).
   Ubuntu/PPA libbladeRF 2.4.1 → constant overruns; libbladeRF 2.6.0 built
   from source (tag 2025.10) → MIB but no SIBs, because srsRAN's plugin read
   META samples with RX_NOW each call and dropped samples → srsue re-synced SFN
   on every overrun. `worker/bladerf_rx.patch`: continuous SC16_Q11 RX, sample
   counting, TX same format. SoapyBladeRF path found no cell (not used).
   `lte_pss.configure(sdr="bladerf")` + `check_earfcns.py --sdr bladerf
   --gain` capture with bladeRF-cli; sib-scan picks it for `-d bladeRF`;
   web app skips `-W` for device bladeRF. Sweep mode stays HackRF-only.
8. ✅ Web app (2026-09-26): run/stop, live log/status (SSE), readings table +
   Leaflet map, location gpsd > browser > manual. Tested end to end with HackRF
   (list mode 6200 + 1875 → PCI/CGI/TAC/RSRP/SIBs + manual location; stop during
   calibration leaves no zombies). Browser geolocation not testable in the
   in-app browser (denied) — check in the user's own browser.
9. ✅ Web app: Frequency column; band presets (Portugal: 20 8 28 3 1 7) and
   custom lists run as sequential sib-scan jobs (ppm from first band reused,
   `-x` skips carriers already read — B28 overlaps B20); gain ≥1 GHz field.
10. ✅ systemd: `systemd/install-service.sh` installs a unit running
    `docker compose up --no-build webapp`. Installed on the user's machine on
    2026-09-26 (`lte-sib-parser-webapp.service`, enabled, docker enabled);
    after code changes: `sudo systemctl restart lte-sib-parser-webapp`.
    Boot autostart confirmed after a reboot on 2026-09-26 (up 13 s after boot).
11. ✅ Web app: Auto/Light/Dark theme, stopwatch (run + band, last run), scan
    durations. PSS/SSS checks parallel (ProcessPool, 8 cores), one 90 ms
    capture split in two, FFT sizes 5-smooth (`fast_len`), coarse search on
    20 ms → B3 sweep 150 s → 32 s. One HackRF cannot scan bands in parallel.
12. ✅ Carriers ≥16 MHz wide (sweep) are saved as detection-only readings
    (`detection='pss'`, PCI, bandwidth, location) instead of running srsue
    (`sib-scan.sh -w` to disable). New columns `bandwidth_mhz`, `detection`,
    migrated automatically. B1: 7 s instead of ~80 s.
13. ✅ Known-EARFCN mode: `sib-scan.sh -K list -W wide -G gain_high`,
    `scripts/check_earfcns.py` (PSS/SSS on exact EARFCNs, measures ppm too),
    SIB5 neighbours checked during the run. Seed list
    `vol/helpers/earfcns/portugal.txt` (from SIB5, one location) + everything
    in readings DB. Web preset "Portugal (known EARFCNs, fast)". 19 EARFCNs
    checked in 64 s; found EARFCN 1300 that the sweep never did.
14. ✅ Learned EARFCNs persisted in `vol/output/earfcns_learned.json`
    (server.update_learned, start/end of each run), Known EARFCNs panel,
    `/api/earfcns`. README screenshot `doc/webapp.png` from fictitious data:
    `vol/webapp/demo/make_demo_db.py` + `server.py --port 8081 --db ... --learned ...`
    in a separate container, headless Firefox with `?view=38.70790,-9.13705,17&theme=light`.
    Never screenshot real data for the repo.
15. ✅ portugal.txt cross-checked with portugaltowers.eu/espetro (community-measured
    carrier centres): identical for B20/B8/B3/B1/B7; added B28 9359, 9468 (21 EARFCNs).
16. ✅ Favicon (static/favicon.svg, favicon-16.svg, favicon.ico; /favicon.ico route) and
    header logo; readings table sortable by header (saved in localStorage).
17. Planned after the bladeRF works alone — **multi-SDR parallel scan**:
    - detect SDRs at start (`SoapySDRUtil --find`, serials) → capabilities:
      bladeRF x40 max 20 MHz cells, ~1 ppm; HackRF max 10 MHz, ~20 ppm (calibrate
      each), gain 56/70.
    - one PSS/SSS check pass (captures via SoapySDR, not hackrf_transfer —
      needed for bladeRF anyway), then a work queue: 20 MHz EARFCNs only to the
      bladeRF; ≤10 MHz to the first free SDR; no bladeRF → current behaviour.
    - one srsue per SDR (device serial, own log/stdout paths, own ppm/gain).
    - Python orchestrator; keep sib-scan.sh for single-SDR use.
    - Limits: HackRF is USB 2.0 and all USB 2.0 devices share ~480 Mbit/s → two
      HackRFs decoding at once likely overflow (bladeRF x40 is USB 3.0);
      realistic set = bladeRF + 1 HackRF. CPU: 1-2 cores per srsue (more at
      20 MHz), 8 cores total.
    - Expected: srsue phase (~4 of ~5.5 min) split → ~2.5-3 min per Portugal run
      (estimate, to measure).
18. Android cell scanner idea (notes/android-cell-scanner-guide.md, not in git):
    on hold by the user's decision (2026-09-27).
19. ✅ srsue retries (`sib-scan.sh -y`, default 1 with -K/-S): failed confirmed
    EARFCNs are retried at the end; success = MIB in this scan's readings
    (`has_mib.py -R db -I scan_id`). bladeRF runs: srsue decodes ~60-80% of
    confirmed cells per attempt, failures random.
20. ✅ Parallel bladeRF + HackRF tested (2026-09-28): slower (7:40, 11/13) than
    bladeRF alone (5:32, 12/12) — CPU contention between two real-time srsue.
    Kept: multi-instance-safe sib-scan (per-PID log files, pid=$!), -n honoured
    in -K, LTE_HACKRF_LOW_GAIN. HackRF clock here ~16.5-17 ppm (varies with
    temperature). Recommendation: bladeRF alone.
21. ✅ Web app SDR selector (HackRF/bladeRF/Other) with defaults for the Cisco
    antennas the user always uses; README "Gain and antennas" explains other
    antennas/places need other values and how to measure them.
22. Next (agreed 2026-09-28), in this order:
    a. **MacBook Pro M4**: Docker Desktop on macOS cannot pass USB devices to
       containers → Ubuntu 24.04 arm64 VM (Parallels, or UTM) with USB
       passthrough, our Docker setup inside. Steps: hackrf_info in the VM;
       bladeRF must show 5000M in `lsusb -t` (USB 3 passthrough); `docker compose
       build` (arm64, srsRAN uses NEON); one srsue test per SDR counting
       overflows (bladeRF 30.72 MSPS ≈ 123 MB/s over virtual USB is the risk);
       web app binds 127.0.0.1 of the VM (browser in the VM, or add a bind option).
       Status 2026-09-28: Ubuntu 26.04 arm64 VM (QEMU), bladeRF at 5000M, image
       built, PSS/SSS captures fine (+0.93 ppm). srsue stuck in "Waiting PHY to
       initialize": FFTW_MEASURE planning ~20 min on arm64, and sib-scan's
       kill -9 meant the wisdom was never saved → `vol/helpers/fftw-warmup.sh`
       (file RF device on /dev/zero, SIGINT once PHY is up), run by sib-scan at
       start. With wisdom: bladeRF Found Cell 25 s, SIB1 decoded, 2 overflows/min.
       sib-scan's kill -9 during cell search made QEMU drop the bladeRF ("fatal
       IO error", 2/2) → stop_srsue: SIGINT in a VM (sys_vendor), -9 natively.
       stop_srsue confirmed (device stays across attempts). srsue kept losing
       the cell at 30.72 MSPS: gdb showed libbladeRF's stream thread (named
       SYNC) at 96% in ioctl — plugin RX buffers were 1024 samples (sized from
       1.92 MSPS at stream start). bladerf_rx.patch: 32768-sample buffers →
       ~10% CPU, 20 MHz cell SIB1 after 11 s in the VM (temporary 15.36 MSPS
       arm64 default reverted). Web app runs (gain 15/40) before the fix: 13
       cells, 15 min; 2nd run 9:10. After the fix (image rebuilt): 13/13 cells
       with SIB1 incl. 5× 20 MHz, 9:37, gain 15/40. ✅ VM works with bladeRF.
       Next: re-measure bladeRF success rate on the x86 laptop; HackRF in VM.
    b. ✅ 2026-09-28 night: **`lte_sib_decoder`** (worker/sib_decoder/, built in the
       image, README decision 48). Default for -d bladeRF in sib-scan (coproc,
       SDR opened once, closed for bladeRF-cli checks; -U = srsue, -X elsewhere).
       Portugal preset via web app in the VM: 2:32, 14/14 cells with CGI (srsue
       9:37); without the bladeRF-cli PSS pre-check (decoder searches itself,
       ppm from CFO): 1:18, 15 cells with CGI. Open: soft combining for low-SNR
       cells (3475 SIB1 only), B7 2950 (weak 20 MHz: sync lost after MIB),
       HackRF with -X untested. Later: soft combining + up to 4 acquisitions per
       carrier (README 50) → preset 1:22, 16 cells, 15 CGI, 14 complete.
       Bad attempts = bad timing lock from acquisition (SNR low from the start).
       2026-09-29: search 2 s, SI wait = 3 periods of slowest missing SI, no
       sib-scan retry with decoder → known preset + 2G 1:33 → 1:04 (README 53).
       Blind CFI + PCI kept per reading once SIB1 is in, RSRP after SIB1, search
       3 s → 1:07-1:09, co-channel-sector cell B8 3475 decoded (README 54).
       Original plan: srsue is a whole UE (~9 s
       start-up per cell, camps on one cell). Build a small C program on
       libsrsran's PHY (see srsRAN lib/examples/pdsch_ue.c, which decodes SIB1
       with SI-RNTI): PSS/SSS → PBCH/MIB → PCFICH/PDCCH (SI-RNTI) → PDSCH → SIB1 and
       SI messages, output JSON like srsue's "Content:" lines so
       parse_save_sib/readings_db stay. Estimate (unmeasured): ~8 s per carrier,
       Portugal preset ~2 min instead of 5.5. Prototype and measure first.
    c. ✅ 2026-09-28 night: **wide captures** (README decision 49): `sib-scan -S -d
       bladeRF` captures 2 s per ~23 MHz (30.72 MSPS; wide_chunks.py) and the
       decoder's `scan` probes every EARFCN + decodes from RAM. Portugal sweep
       preset 4:01, 16 carriers/17 cells, no list. Web app Sweep enabled for
       bladeRF. 61.44 MSPS works but probing is 5-10x slower per EARFCN.
       Not done: several PCIs per carrier (only the strongest per N_id_2).
       Original plan: one capture up to ~56 MHz covering several
       carriers of a band (e.g. most of B1/B3), digital down-conversion per
       carrier, decode all from the same samples; also all cells (PCIs) on a
       carrier, not just the strongest.
    d. ✅ **2G (GSM)** (2026-09-29): `vol/scripts/gsm_scan.py` + `gsm_decode.py`
       (numpy, no gr-gsm: not packaged for 22.04, gr-osmosdr links libbladeRF
       2.4.1). bladeRF 56 MSPS/50 MHz: GSM-900 one capture, DCS-1800 two, 1.2 s
       each, next capture recorded while decoding. FFT filter bank (small
       blocks: cache), FCCH differential detector (real part > 0.85) + hits
       10/11/…/51 frames apart, CFO from the FFT peak of the tone, two SCH
       that agree, LS channel estimate + batched max-log BCJR + soft Viterbi +
       Fire code. ~29 s for both bands on the i7-8550U, 29-33 cells here
       (GSM-900 3 operators, DCS-1800 1). Readings: rat='GSM', ARFCN in earfcn,
       BSIC in pci, LAC in tac, CI in cell_id, JSON in `gsm`. Web app: mode
       "2G only" and "Also 2G" (runs after LTE). HackRF path written, untested.
       Gains bladeRF 15 (900) / 30 (1800; 40 clipped).
       Then (same day) `worker/sib_decoder/gsm_decoder.cc` (C++ port, FFTW,
       threads; Python parses its hex SIs) + one bladeRF-cli session for all 3
       captures (file complete = full size): 2G in ~10 s, 25-30 cells, 23-24
       with CGI. numpy path kept (LTE_GSM_NUMPY=1). Remaining cost: FCCH
       look-ahead ~1 s per capture, opening the bladeRF ~2 s. Next ideas: SI2
       range formats (DCS neighbour lists).
    Out of scope by the user's choice: Wi-Fi, Bluetooth, 3G.
23. Further goals: to be defined with the user.
