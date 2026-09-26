# LTE cell scanner and MIB/SIB parser

Passively finds LTE cells and decodes their broadcast system information
(MIB, SIB1–SIB13) with an SDR, storing everything in SQLite. It combines
srsRAN's `cell_search` with a patched, **receive-only** `srsue`.

This is a fork of [godfuzz3r/lte-sib-parser](https://github.com/godfuzz3r/lte-sib-parser)
that adds HackRF One support, fixes srsue transmitting, and adds a sweep-based
scan mode with automatic clock calibration. Every change is listed in
[Changes and design decisions](#changes-and-design-decisions).

> **srsue never transmits.** `worker/rx_only.patch` turns srsue's radio TX
> functions into no-ops. The upstream project relied on `sib_logger.patch` for
> this, but that patch only changes log levels: srsue would attempt an RRC
> connection (PRACH) through the SDR's TX port.

## How it works

`vol/sib-scan.sh` drives the scan:

1. **Find a cell**
   - default: `cell_search` walks the band EARFCN by EARFCN until it finds a cell;
   - `-S` (HackRF): `hackrf_sweep` measures the whole band, LTE carriers are
     found in the spectrum and their exact EARFCN is confirmed with PSS/SSS;
   - `-q "e1 e2 …"`: an explicit list of EARFCNs.
2. **Decode**: srsue is started on the EARFCN and `parse_save_sib.py` reads
   its log, saving MIB, RSRP and SIBs to SQLite (see [Timeouts](#timeouts)).
3. **Follow neighbours**: EARFCNs listed in SIB5 are scanned the same way,
   recursively (disable with `-n`).
4. With `cell_search`, the band search resumes where it stopped until the
   end of the range.

## Supported SDRs

| SDR | Status | Notes |
|---|---|---|
| LimeSDR Mini / USB | tested upstream | `-d soapy -a "rxant=LNAW"` |
| HackRF One | tested in this fork (B20, B3) | `-d soapy -a "driver=hackrf"`, see [HackRF usage](#hackrf-usage) |
| RTL-SDR | not usable for SIBs | ≤2.4 MSPS: not enough for any LTE cell's SIBs |
| USRP (and B210 clones) | should work (srsRAN supports it) | see [USRP clones](#usrp-clones) |
| bladeRF | should work (srsRAN supports it) | the native srsRAN bladeRF plugin is not built in the image yet |

## Installation

Everything (drivers, srsRAN, Python) runs inside a Docker image. The host
needs Docker with the compose plugin, git, and access to the SDR over USB.

### Docker, compose and git

Tested on Arch Linux. The other commands use each distribution's standard
packages and were not tested with this project.

**Arch Linux / Manjaro**
```bash
sudo pacman -S docker docker-compose docker-buildx git
```

**Ubuntu 22.04 / 24.04**
```bash
sudo apt update
sudo apt install docker.io docker-compose-v2 git
```

**Debian 12**: Debian's own `docker-compose` package is the old v1, so use
Docker's repository: follow <https://docs.docker.com/engine/install/debian/>
(installs `docker-ce` and `docker-compose-plugin`), then:
```bash
sudo apt install git
```

**Fedora**
```bash
sudo dnf install moby-engine docker-compose git
```
(or Docker's own packages: <https://docs.docker.com/engine/install/fedora/>)

**openSUSE Tumbleweed / Leap**
```bash
sudo zypper install docker docker-compose git
```

Then, on every distribution, start Docker and allow your user to use it:
```bash
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```
Log out and back in (or reboot) so the `docker` group applies to all your
programs; `newgrp docker` only applies to the current terminal.

### Optional host tools

Not needed to run the project (the image has them), but handy to check that
the SDR is seen by the host:

| Distribution | Command |
|---|---|
| Arch | `sudo pacman -S hackrf usbutils` |
| Ubuntu / Debian | `sudo apt install hackrf usbutils` |
| Fedora | `sudo dnf install hackrf usbutils` |
| openSUSE | `sudo zypper install hackrf usbutils` |

```bash
lsusb            # the SDR should be listed
hackrf_info      # HackRF only
```

### RTL-SDR dongles

The kernel's DVB-T driver grabs RTL2832 dongles; blacklist it so SDR software
can use them:
```bash
echo -e "blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830" | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf
sudo modprobe -r dvb_usb_rtl28xxu rtl2832
```

## Build and run

```bash
git clone https://github.com/oicnanev/lte-sib-parser.git
cd lte-sib-parser
docker compose build        # compiles srsRAN, takes a while
```

Connect the SDR, then open a shell in the container (the `vol/` folder is
mounted at `/vol`):
```bash
docker compose run --rm worker
```
or with `./run.sh`, which also forwards the X11 socket and PulseAudio. Nothing
in the scan needs a GUI; if you need one, allow only the container's root user
with `xhost +SI:localuser:root` (see [decision 12](#changes-and-design-decisions)).

Inside the container:
```bash
./sib-scan.sh -h
./sib-scan.sh -d soapy -a "rxant=LNAW" -b 3                             # LimeSDR
./sib-scan.sh -S -p auto -d soapy -a "driver=hackrf" -g 56 -b 20        # HackRF
```

Results are written to `vol/output/` (ignored by git).

## sib-scan.sh options

```
usage: sib-scan.sh [OPTION]...
  -h      show this help message
  -d      device name (UHD,soapy,bladeRF)
  -a      device args (example: "rxant=LNAW")
  -g      rx gain (default: 30)
  -r      force srsue rf sample rate in Hz, srsue decimates in software
          (the ratio to the cell's sample rate must be an integer)
  -p      frequency correction in ppm for SDR clock error, positive
          tunes higher (e.g. a HackRF whose clock is 20 ppm slow: -p 20)
          -p auto measures it on the band's LTE cells (HackRF, needs -b)
  -b      lte band
  -s      start earfcn
  -e      end earfcn
  -S      find carriers with hackrf_sweep instead of cell_search (HackRF
          only, needs -b). With numpy the exact EARFCN is found with PSS/SSS,
          otherwise each carrier is tried on the 3 closest EARFCNs.
  -q      use explict list of earfcn's (avoid cell_search)
          example: -q "1300 1301 1302 1303"
  -n      no reqursive scan, do no scan cells from sib5
  -t      seconds srsue gets to decode anything (MIB) on an EARFCN
          (default: 30)
  -T      after each newly decoded MIB/SIB, srsue keeps listening for
          this many seconds more; it stops earlier once all SIBs
          scheduled in SIB1 are decoded (default: 30)
  -D      sqlite database to save results
          (default: /vol/output/cells.sqlite)
```

Examples:
```bash
./sib-scan.sh -b 3                       # whole band 3, then SIB5 neighbours
./sib-scan.sh -b 3 -s 1300 -e 1400       # part of band 3
./sib-scan.sh -s 1300 -e 1400            # band is derived from the EARFCNs
./sib-scan.sh -q "1300 1301 1302 1303"   # explicit EARFCNs
./sib-scan.sh -b 3 -D /tmp/myoutput.sqlite
```

### Timeouts

For each EARFCN, srsue runs until one of these happens:

- **nothing decoded within `-t` seconds** (default 30): no cell there, or too
  weak;
- **no new MIB/SIB for `-T` seconds** (default 30): every newly decoded MIB or
  SIB restarts this countdown, so srsue keeps going while it is still making
  progress and stops once it has been quiet for `-T` seconds;
- **everything expected is decoded**: RSRP, MIB, SIB1, SIB2 and every SIB that
  SIB1 schedules. This is the usual case for a good cell (~1 minute).

The total time per EARFCN is therefore at most about `-t` + (number of SIBs ×
`-T`), but in practice it ends as soon as the SIB list is complete. SIBs are
repeated every 80 ms to a few seconds, so `-T 30` leaves room for decoding
errors; lower it to scan faster at the risk of missing a rarely sent SIB.

## HackRF usage

The HackRF One works through soapy, with some limits:

- **Max 20 MSPS**: SIBs can be decoded from cells up to 10 MHz (15.36 MSPS).
  15/20 MHz cells are found by the sweep and srsue may decode their MIB, but
  it cannot follow them to read SIBs.
- **Clock error**: HackRF crystals can be ~20 ppm off (16 kHz at 800 MHz,
  38 kHz at 1.9 GHz), beyond what srsRAN's cell search tolerates (a few kHz).
  Pass `-p <ppm>`, or `-p auto` to measure it on the band's cells.
- **cell_search is unreliable** with a HackRF (see [decisions](#changes-and-design-decisions)):
  use `-S`.
- **8-bit ADC**: gain matters. `-g 56` worked on B20 (800 MHz); B3 (1.8 GHz)
  needed `-g 70`.

Scan a band, calibrating the clock on the same band:
```bash
./sib-scan.sh -S -p auto -d soapy -a "driver=hackrf" -g 56 -b 20
./sib-scan.sh -S -p auto -d soapy -a "driver=hackrf" -g 70 -b 3
```

Or measure the clock error once (it drifts ~1 ppm with temperature) and reuse it:
```bash
python3 scripts/calibrate_ppm.py -b 20       # prints e.g. 20.52; prefer B20/B8
./sib-scan.sh -S -p 20.52 -d soapy -a "driver=hackrf" -g 70 -b 3
```

Measured on the author's setup: B20 (3 × 10 MHz carriers) and B3 (10 MHz
carrier) decoded MIB and SIB1–5, 7; each cell takes about a minute.

## LimeSDR usage

For LimeSDR devices use `-d soapy` to avoid a long search for UHD devices:
```bash
./sib-scan.sh -d soapy -a "rxant=LNAW" -b 3
```

## USRP clones

Place the custom firmware in `vol/helpers/uhd_images/` with the name UHD
expects, such as `usrp_b210_fpga.bin`. It is copied to
`/usr/share/uhd/images/` when the container starts. Useful for USRP B210
clones such as LibreSDR.

## Reading the results

```bash
cd /vol
python3 dbparsers/list-cells.py -d ./output/cells.sqlite   # band, EARFCN, RSRP, decoded SIBs
```
![cell basic info](./doc/1.png)

```bash
python3 dbparsers/get-info.py -d ./output/cells.sqlite     # SIB3/SIB5 info per EARFCN
```
![cell basic info](./doc/2.png)

```bash
python3 dbparsers/get-sib.py -d ./output/cells.sqlite -e 6200 -s sib1   # one SIB as JSON
python3 dbparsers/get-arfcns.py -d ./output/cells.sqlite                # all scanned EARFCNs
```

Rescan the EARFCNs of an earlier scan:
```bash
./sib-scan.sh -d soapy -a "rxant=LNAW" -g 40 -q "$(python3 dbparsers/get-arfcns.py -d ./output/cells.sqlite)"
```

## Helper scripts

| Script | Purpose |
|---|---|
| `vol/scripts/sweep_candidates.py -b <band>` | HackRF sweep: carriers and their EARFCNs (`-r` exact EARFCN + PCI, `-v` details) |
| `vol/scripts/calibrate_ppm.py -b <band>` | HackRF clock error in ppm, measured on real cells |
| `vol/scripts/lte_pss.py` | PSS/SSS search on raw IQ (library used by the two above) |
| `vol/scripts/earfcn_to_freq.py <earfcn>` | EARFCN → downlink frequency in Hz |
| `vol/helpers/srsue-debug.sh <earfcn> <gain> [srsue args]` | run srsue for 15 s with verbose logs, show sync peaks and decoded messages |

## Changes and design decisions

Changes in this fork, newest last, with the reason for each.

1. **Receive-only srsue** (`worker/rx_only.patch`). `sib_logger.patch` does
   not disable TX, and srsue tried to connect (RRC connection request, PRACH).
   `radio::tx`, `tx_end`, `set_tx_freq` and `set_tx_gain` are now no-ops.
   Required anyway for SDRs with a single local oscillator (HackRF): SoapyHackRF
   applies `set_tx_freq` to the shared LO, so srsue ended up listening on the
   uplink frequency and never saw a cell.
2. **HackRF and RTL-SDR soapy modules** added to the image.
3. **Clock correction in ppm** (`-p`). A HackRF was measured at ~20.5 ppm
   (+16 kHz at 796 MHz); srsRAN's PSS search tolerates only a few kHz. The
   correction is given in ppm, not Hz, so one value works on every band:
   sib-scan converts it to srsue's `--rf.freq_offset` per EARFCN, and
   `worker/cell_search_ppm.patch` adds `-p` to `cell_search`.
4. **Sweep mode instead of cell_search for HackRF** (`-S`). Even with the
   clock corrected, `cell_search` found a cell about 1 time in 5, on the wrong
   EARFCN and with a wrong ID: it samples at 1.92 MSPS, where the HackRF
   filters poorly, and restarts the stream on every EARFCN. `hackrf_sweep`
   covers a band in under a second: LTE carriers show up as blocks of standard
   width (edges found at half level between noise and the block's plateau,
   25 kHz bins, noise floor measured 5 MHz beyond the band edges).
5. **Exact EARFCN and PCI with PSS/SSS** (`lte_pss.py`, numpy added to the
   image). A block's centre is only known to ±100 kHz, while srsue needs the
   exact EARFCN. 40 ms of IQ are captured at 7.68 MSPS, filtered to 1.92 MSPS
   and correlated with the three PSS over frequency offsets of ±150 kHz. The
   PSS is a Zadoff-Chu sequence, which correlates almost as well at the true
   offset ±15/30 kHz (peaks within 1 % measured on B3), so each of those
   hypotheses is checked with the SSS, which only decodes at the true offset
   and also gives the PCI. Blocks without PSS/SSS (GSM, NR, noise) are dropped.
   Without numpy, sib-scan falls back to trying the 3 closest EARFCNs per
   carrier and skips the rest once a MIB is decoded.
6. **Automatic clock calibration** (`-p auto`, `calibrate_ppm.py`). The
   offset measured on a carrier is its raster error (a multiple of 100 kHz)
   plus the clock error; the combination with the smallest |ppm| is taken, and
   the median over up to 3 carriers is used. This is unambiguous while the
   clock error is below 50 kHz at that frequency (~60 ppm at 800 MHz, ~25 ppm
   at 1.9 GHz), hence the advice to calibrate on B20/B8.
7. **Merge spectral holes.** A lightly loaded cell only transmits reference
   and control signals on part of its band, and one 20 MHz B3 carrier showed
   up as several 5 MHz blocks. Blocks closer than 0.5 MHz (less than the guard
   between any two adjacent LTE carriers) are merged, and calibration prefers
   blocks whose width matches a standard LTE bandwidth.
8. **No busy wait in `parse_save_sib.py`.** It polled the srsue log at 100 %
   CPU, competing with srsue, which must process samples in real time. It now
   sleeps 50 ms when there is no new line.
9. **Idle timeout and early stop** (`-T`). `-T` used to add its seconds to a
   deadline for every decoded SIB, so a cell with 6 SIBs was listened to for
   30 + 6 × 30 s. The deadline is now "last new MIB/SIB + `-T`". Also, RSRP was
   never marked as received, so the "everything decoded" check never passed
   and every cell ran until the timeout. With both fixes a cell takes ~70 s
   instead of ~3 min, with the same SIBs decoded.
10. **`-r` no longer recommended for HackRF.** Forcing 15.36 MSPS made no
    difference once tuning was fixed, and it asserts on 20 MHz cells (30.72 MSPS
    is not an integer multiple).
11. **Repository hygiene**: `vol/output/cells.sqlite` is no longer tracked
    (scan results reveal where the scan was made); obsolete `version:` key
    removed from `docker-compose.yml`; `-d`/`-D` typo fixed in an example;
    Python `__pycache__/` ignored.
12. **`run.sh` no longer runs `xhost +`.** It disabled X server access control
    for every client (local and remote) until `xhost -`, only so a GUI in the
    container could open windows, which the scan never does. If a GUI is
    needed, `xhost +SI:localuser:root` grants access to the local root user only.

### Known limitations

- 15/20 MHz cells on a HackRF: no SIBs; the MIB is decoded only sometimes.
- `cell_search` with a HackRF remains unreliable; use `-S`.
- `-p auto` needs LTE cells on the chosen band, and calibration on bands
  above ~1.5 GHz is ambiguous for clocks more than ~25 ppm off.
- The first srsue run in a new container takes >15 s to start (FFTW plans).

## License

AGPL-3.0, see [LICENSE](LICENSE). Based on
[godfuzz3r/lte-sib-parser](https://github.com/godfuzz3r/lte-sib-parser) and
[srsRAN_4G](https://github.com/srsran/srsRAN_4G).
