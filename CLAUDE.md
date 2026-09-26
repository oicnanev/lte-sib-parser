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
    `earfcn_to_band.py`, `band_to_earfcn.py`.
  - `dbparsers/` — Python tools to inspect results (`list-cells.py`,
    `get-info.py`, `get-sib.py`, `get-arfcns.py`).
  - `helpers/ue.conf` — srsue config; `helpers/lte_bands.sqlite3` — band/EARFCN table;
    `helpers/uhd_images/` — optional custom FPGA images (e.g. B210 clones).
  - `output/` — scan results (git-ignored).
- `run.sh` — runs the built image interactively with USB/X11 access.
- `docker-compose.yml` — builds the `worker` image.

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

- Python 3 scripts, plain stdlib (`sqlite3`, `json`), no package structure.
- Bash for orchestration.
- srsRAN changes go into patch files in `worker/`, not a vendored tree
  (`srsRAN_4G/` is git-ignored).

## User hardware

- **HackRF One** (primary): used via Soapy (`-d soapy -a "driver=hackrf"`).
  Max 20 MSPS → SIB decoding works for cells up to 10 MHz (15.36 MSPS with
  `lte_sample_rates`); 15/20 MHz cells only show up in `cell_search`.
  8-bit ADC → gain tuning matters.
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
- The user's HackRF clock is ~**-20 ppm** (LO low): needs
  `--rf.freq_offset` ≈ +20e-6 × f_DL (≈ +16300 Hz at 796 MHz). Passed via
  `sib-scan.sh -o <Hz>`. srsRAN cell search tolerates only a few kHz CFO.
- Gain: `-g 40` works for strong cells, weaker ones need `-g 56..70`.
- `cell_search` (C example) still fails with HackRF: runs at 1.92 MSPS
  (poor HackRF filtering below 8 MSPS) and has no freq-offset option.
- Working command (inside container):
  `./sib-scan.sh -d soapy -a "driver=hackrf" -g 56 -o 16300 -q "6200 6400" -n`
- Verified on B20: EARFCNs 6200 and 6400 decode MIB + SIB1-5,7.
- Debug helpers: `vol/helpers/srsue-debug.sh <earfcn> <gain> [srsue args]` runs srsue
  15 s with verbose logs; raw IQ via `hackrf_transfer` for offline PSS/CFO checks.

## Current work / plan

1. ✅ HackRF receiving and decoding SIBs on B20 via `-q` EARFCN list.
2. Make band scanning work with HackRF: patch `cell_search` (freq offset +
   higher srate with decimation) or replace it with a sweep-based detector.
3. Auto-calibrate the HackRF ppm error (scale offset with frequency).
4. Test B8 / B3 (B3 needs ~+37 kHz offset; 20 MHz cells → MIB only).
5. Further goals: to be defined with the user.
