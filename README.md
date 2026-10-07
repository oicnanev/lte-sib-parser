# LTE cell scanner and MIB/SIB parser

Passively finds LTE cells and decodes their broadcast system information
(MIB, SIB1–SIB13) with an SDR, storing everything in SQLite. It combines
srsRAN's `cell_search` with a patched, **receive-only** `srsue` (and, with a
bladeRF, its own decoder). GSM cells (2G, 900/1800 MHz) are read too: see
[2G (GSM)](#2g-gsm).

This is a fork of [godfuzz3r/lte-sib-parser](https://github.com/godfuzz3r/lte-sib-parser)
that adds HackRF One support, fixes srsue transmitting, adds a sweep-based
scan mode with automatic clock calibration, a readings database with location
and cell identity, and a local [web app](#web-app) with a live map. Every
change is listed in
[Changes and design decisions](#changes-and-design-decisions).

![Web app with demo data](doc/webapp.png)
*The web app ([below](#web-app)), with fictitious demo data.*

> **srsue never transmits.** `worker/rx_only.patch` turns srsue's radio TX
> functions into no-ops. The upstream project relied on `sib_logger.patch` for
> this, but that patch only changes log levels: srsue would attempt an RRC
> connection (PRACH) through the SDR's TX port.

## How it works

`vol/sib-scan.sh` drives the scan:

1. **Find a cell**
   - default: `cell_search` walks the band EARFCN by EARFCN until it finds a cell;
   - `-K "e1 e2 …"` (HackRF): a list of known EARFCNs is checked for cells with
     PSS/SSS, and EARFCNs advertised in SIB5 are added (see [Known EARFCNs](#known-earfcns));
   - `-S` (HackRF): `hackrf_sweep` measures the whole band, LTE carriers are
     found in the spectrum and their exact EARFCN is confirmed with PSS/SSS;
   - `-q "e1 e2 …"`: an explicit list of EARFCNs.
2. **Decode**: srsue is started on the EARFCN and `parse_save_sib.py` reads
   its log, saving MIB, RSRP and SIBs to SQLite (see [Timeouts](#timeouts)):
   one row per EARFCN in `cells.sqlite`, and one row per reading, with
   location, CGI and PCI, in `readings.sqlite` (see [Readings database](#readings-database)).
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
| bladeRF 2.0 micro | tested in this fork (xA5, firmware v2.6.0, FPGA v0.16.0) | `-d bladeRF`, decodes 20 MHz cells, see [bladeRF usage](#bladerf-usage) |
| bladeRF (1st gen, x40/x115) | should work (same srsRAN plugin) | not tested |

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

### GPS receiver (optional)

With a GPS receiver and `gpsd` on the host, every reading gets a GPS position;
otherwise the web app uses the browser's position or one set on the map (see
[Location](#location)).

| Distribution | Command |
|---|---|
| Arch | `sudo pacman -S gpsd` |
| Ubuntu / Debian | `sudo apt install gpsd gpsd-clients` |
| Fedora | `sudo dnf install gpsd gpsd-clients` |
| openSUSE | `sudo zypper install gpsd` |

Point gpsd at the receiver (often `/dev/ttyACM0` or `/dev/ttyUSB0`): on
Debian/Ubuntu set `DEVICES="/dev/ttyACM0"` in `/etc/default/gpsd`, on other
distributions in `/etc/gpsd` or `/etc/sysconfig/gpsd`. Then:
```bash
sudo systemctl enable --now gpsd
gpspipe -w -n 10 | grep TPV      # should show "mode":2 or 3 with lat/lon
```
gpsd listens on `127.0.0.1:2947`; the container reaches it through the host
network.

**Receivers with a Prolific PL2303** (USB `067b:2303`, e.g. GlobalSat BU-353
and other SiRF Star III units, `/dev/ttyUSB0`): the gpsd package leaves the
hotplug rule for this generic USB-serial chip commented out, so gpsd drops the
receiver when it is unplugged and does not take it back. Enable it for the
PL2303 only:
```bash
echo 'SUBSYSTEM=="tty", KERNEL=="ttyUSB*", ATTRS{idVendor}=="067b", ATTRS{idProduct}=="2303", SYMLINK+="gps%n", TAG+="systemd", ENV{SYSTEMD_WANTS}+="gpsdctl@%k.service"' | sudo tee /etc/udev/rules.d/61-gpsd-pl2303.rules
sudo udevadm control --reload && sudo systemctl restart gpsd
```
Several sources can be listed, first preferred:
`DEVICES="/dev/ttyUSB0 udp://0.0.0.0:29998"`.

**Keep the GPS receiver away from USB 3.** USB 3 ports, cables and devices
radiate broadband noise around 1.5 GHz, where GPS L1 is (1575 MHz). Next to
the MacBook and the bladeRF (USB 3, ~120 MB/s), two SiRF III receivers saw no
satellite at all for 25 minutes, even outside the window and after cold and
factory resets; on a 2 m USB 2 extension one had a 3D fix in 15 s (4
satellites, SNR 30-35 dB). Without a fix these receivers report their
firmware's default date (June 2026), not an error in the system clock.

**A phone's GPS instead of a receiver** (e.g. on a MacBook, which has no GPS:
its location is Wi-Fi based). On Android, *GPSd Forwarder* (F-Droid, open
source) sends the phone's NMEA by UDP to a host and port. gpsd reads it with:
```bash
# /etc/default/gpsd
DEVICES="udp://0.0.0.0:29998"
GPSD_OPTIONS="-n"
```
If the scanner runs in a VM behind NAT (UTM's *Shared Network*, VM at e.g.
192.168.64.3), the phone cannot reach the VM: point the app at the Mac's IP
(`ipconfig getifaddr en0`, Mac and phone on the same Wi-Fi or the phone's
hotspot) and relay on the Mac with
`socat -u UDP-RECV:29998 UDP-SENDTO:192.168.64.3:29998` (`brew install socat`).
Check with `gpspipe -w -n 5` (TPV with `"mode":3`). Take the app out of
Android's battery optimisation: when it pauses, readings fall back to the
browser position. Measured indoors: 3D fix, ±27-49 m; better outdoors.

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

**After a `git pull`, rebuild the image** when anything under `worker/`
changed (the decoders and srsRAN patches are compiled into it), then restart
the web app:
```bash
git pull && docker compose build && sudo systemctl restart lte-sib-parser-webapp
```
The scripts in `vol/` are used straight from the checkout, so without the
rebuild they can ask the old binaries for options they do not have;
`sib-scan.sh` warns about this and falls back to srsue.

Or start the [web app](#web-app):
```bash
docker compose up webapp          # then open http://localhost:8080
```

## sib-scan.sh options

```
usage: sib-scan.sh [OPTION]...
  -h      show this help message
  -d      device name (UHD,soapy,bladeRF)
  -a      device args (example: "rxant=LNAW")
  -g      rx gain (default: 30)
  -G      rx gain for EARFCNs at 1 GHz and above (default: same as -g)
  -r      force srsue rf sample rate in Hz, srsue decimates in software
          (the ratio to the cell's sample rate must be an integer).
          Default with -d bladeRF: 30.72e6 (sample-rate changes take ~4 s
          on a bladeRF 2.0; 15 MHz cells, 23.04 MSPS, then fail)
  -p      frequency correction in ppm for SDR clock error, positive
          tunes higher (e.g. a HackRF whose clock is 20 ppm slow: -p 20)
          -p auto measures it on the band's LTE cells (HackRF, needs -b)
  -b      lte band
  -s      start earfcn
  -e      end earfcn
  -S      find carriers with hackrf_sweep instead of cell_search (HackRF
          only: -d soapy -a driver=hackrf; needs -b). With numpy the exact EARFCN is found with PSS/SSS,
          otherwise each carrier is tried on the 3 closest EARFCNs.
  -w      with -S: also run srsue on carriers >= 16 MHz wide (20 MHz cells).
          By default they are saved as detection-only readings (PCI and
          bandwidth, no SIBs): a HackRF (20 MSPS) cannot decode them
  -x      with -S: DL frequencies in MHz to skip, e.g. -x "796.0 806.0"
          (carriers already read in an overlapping band)
  -K      check a list of known EARFCNs for cells with PSS/SSS (HackRF,
          numpy), then run srsue only where there is one. EARFCNs advertised
          in SIB5 by the cells decoded are checked too. With -p auto the
          clock is measured in the same pass. Example: -K "6200 1875 2800"
  -W      with -K: EARFCNs known to be too wide for the SDR (20 MHz cells on
          a HackRF): saved as detection-only readings, no srsue
  -y      srsue retries for EARFCNs where PSS/SSS confirmed a cell but srsue
          did not decode SIB1 (the cell identity); retries run at the end
          (default: 1 with -K, or -S with numpy; 0 otherwise)
  -q      use explict list of earfcn's (avoid cell_search)
          example: -q "1300 1301 1302 1303"
  -n      no reqursive scan, do no scan cells from sib5
  -t      seconds srsue gets to decode anything (MIB) on an EARFCN
          (default: 30)
  -T      after each newly decoded MIB/SIB, srsue keeps listening for
          this many seconds more; it stops earlier once all SIBs
          scheduled in SIB1 are decoded (default: 30)
  -D      sqlite database to save results, one row per EARFCN
          (default: /vol/output/cells.sqlite)
  -R      readings database: one row per cell reading with location,
          CGI, PCI and RSRP (default: /vol/output/readings.sqlite)
  -L      location file written by the web app (browser or map position),
          used when gpsd has no fix (default: /tmp/lte_location.json)
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
  progress and stops once it has been quiet for `-T` seconds; right after the
  MIB the countdown is only 10 s until SIB1 arrives (SIB1 is sent every 80 ms,
  so it normally follows within 1–2 s);
- **everything expected is decoded**: RSRP, MIB, SIB1, SIB2 and every SIB that
  SIB1 schedules. This is the usual case for a good cell (~1 minute).

The total time per EARFCN is therefore at most about `-t` + (number of SIBs ×
`-T`), but in practice it ends as soon as the SIB list is complete.

When PSS/SSS has confirmed a cell on an EARFCN (`-K`, or `-S` with numpy) and
srsue does not decode its SIB1 (no MIB, or a MIB without SIB1 and so without
cell identity), the EARFCN is tried once more at the end of the scan (`-y` sets
the number of retries); the retry completes the same reading row. A failed attempt costs `-t` plus srsue's
start-up (~10–15 s). SIBs are
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
  needed `-g 70` with the stock antenna. With a Cisco 4G-LTE-ANTM-D at a
  strong-signal site: `-g 44 -G 56`, and `LTE_HACKRF_LOW_GAIN=24,16` for the
  PSS/SSS captures below 1 GHz.

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

## Web app

A local web page to run scans and watch them live:

```bash
docker compose build              # once
docker compose up webapp          # Ctrl+C to stop
```
Open <http://localhost:8080>. It shows:

- **Scan form**: mode (sweep, cell_search, EARFCN list), band, device, gain,
  clock ppm (`auto` or a number), timeouts, SIB5 neighbours; **Run** / **Stop**.
  The **SDR** selector (HackRF One, bladeRF 2.0, Other) fills in device, device
  args, gains, `-t`/`-T` and the HackRF capture gain with the values measured
  for that SDR with a Cisco LTE antenna (see [Gain and antennas](#gain-and-antennas));
  every field stays editable and the choice is remembered. The mode and band
  are remembered too; a new browser starts on "Portugal (known EARFCNs, fast)".
  The band can also be a preset or a custom list, see [Several bands](#several-bands).
- **Activity**: the scan's live output, in a panel that opens like Known
  EARFCNs (closed by default; opening it jumps to the latest lines). The
  current task and EARFCN are always shown in the header.
- **Operators** in the PLMNs column: a coloured badge per PLMN (268-01
  Vodafone, 268-02 DIGI, 268-03 NOS, 268-06 MEO; both for RAN sharing, e.g.
  `268-01 268-03`), with the code in its tooltip; any other PLMN (e.g. abroad)
  is shown as its code. The database and the CSV keep the codes. Logo files named by PLMN in
  `vol/webapp/static/logos/` (`268-01.svg`, `.png`, ...) replace the badges;
  they stay out of git (trademarks, public repository). Page only: nothing in
  the database, no effect on scanning.
- **Repeat until Stop** (tick box above Run): the server starts the same scan
  again as soon as it ends, e.g. while driving; each run is its own scan in the
  database. Unticked during a run, that run is the last; Stop ends it at once.
  A run that fails within 15 s (SDR unplugged) is not repeated. Not remembered
  across page reloads. The stopwatch shows the run: `⏱ 12:40 · run 7 1:05 ↻`.
- **Stopwatch** in the header: elapsed time of the run and of the current band
  while scanning (the step: `B20`, `LTE` for an EARFCN list or preset, `2G`),
  then `last run 13:46 (6 bands)` or `(6 bands, with 2G)`. The scan filter shows each
  band's duration.
- **Theme** button in the header: Auto (follows the system), Light or Dark;
  the choice is kept in the browser. In dark mode the map tiles are darkened.
- **Known EARFCNs** panel (collapsed): every EARFCN the known-EARFCN preset
  will check, with band, frequency, bandwidth, where it came from (list file,
  read, advertised in SIB5) and when a cell was last read on it.

Optional URL parameters: `?view=lat,lon,zoom` opens the map at that view and
keeps it (e.g. `http://localhost:8080/?view=38.708,-9.137,17`), and
`?theme=light|dark` overrides the theme for that page load without saving it.
- **Map**: your current position (with its accuracy) and one marker per place
  where readings were made, coloured by the best RSRP there; click it for the
  list of cells.
- **Readings table**: time, band, downlink frequency, bandwidth, EARFCN, PCI,
  CGI, PLMNs, TAC, eNB ID, cell ID, RSRP, decoded SIBs (or *detected only*) and
  location, updated live. GSM cells share the columns: ARFCN, BSIC, LAC and
  their SI messages. Click a column header to sort by it, click again to
  reverse (▲/▼); empty values always go last and the choice is remembered; filter by scan; click a row
  for every field and the full MIB/SIB contents.

Only one scan runs at a time (the SDR can't be shared). The server is
`vol/webapp/server.py`, Python standard library only; live updates use
Server-Sent Events.

### Known EARFCNs

The preset **Portugal (known EARFCNs, fast)** skips the band sweeps. It checks
a list of EARFCNs for cells and runs srsue only where it finds one:

1. The list is `vol/helpers/earfcns/portugal.txt` plus every EARFCN learned
   so far: read directly or advertised in any cell's SIB5, in any earlier scan
   anywhere. Learned EARFCNs are kept in `vol/output/earfcns_learned.json`
   (first/last reading, last SIB5 advertisement, bandwidth, operators), which is
   updated from the readings database at the start and end of every run and
   survives deleting the readings. So a carrier found in another city is
   checked in every later run. To forget them, delete that file too.
2. Each EARFCN gets a PSS/SSS check (~1 s of capture each, analysed in
   parallel). Since the EARFCN is exact, the offset measured on each cell is
   the clock error, so `ppm auto` is measured in the same pass on any band.
3. srsue runs on the EARFCNs with a cell, using *Gain* below 1 GHz and
   *Gain ≥ 1 GHz* above. EARFCNs known to be 20 MHz wide (from an earlier MIB
   or sweep) are saved as detected only.
4. EARFCNs that the decoded cells advertise in SIB5 and that were not checked
   yet are checked and read before the run ends, so in a new area the local
   cells extend the list.

The list file was cross-checked with the carrier centre frequencies that the
[Portugal Towers](https://portugaltowers.eu/espetro) community measured
nationwide (September 2026): the 19 EARFCNs learned from SIB5 at one place
matched every carrier listed for B20, B8, B3, B1 and B7, and the two B28
carriers there (EARFCN 9359, 9468) were added. That site only lists carriers
seen in at least 27 cells and gives centres, not bandwidths.

**The list is not complete and cannot be.** A cell's SIB5 lists the
frequencies its operator uses *in that area*; other areas may use other
carriers (the list already has two B8 carriers 1 MHz apart, 3475 and 3485,
which cannot both be in use at one place), an operator never decoded
contributes nothing, and operators refarm spectrum over time. Run the
**Portugal sweep** preset now and then, or in a new region, to find carriers
nobody advertised; its readings then join the list.

From the command line:
```bash
./sib-scan.sh -K "$(grep -o '^[0-9]*' helpers/earfcns/portugal.txt)" -W "500 1700" \
    -p auto -d soapy -a "driver=hackrf" -g 56 -G 70
```

### Several bands

The **Band** list starts with presets and ends with **Custom list…** (e.g.
`20 3 7`). The preset **Portugal sweep: B20, B8, B28, B3, B1, B7** covers the FDD bands
Portuguese operators use for LTE; B38 (TDD) is left out because the PSS/SSS
detector assumes FDD.

The bands are scanned one after another, in the order given, each as its own
`sib-scan.sh` run and its own entry in `scans`:

- **Gain**: *Gain* is used below 1 GHz and *Gain ≥ 1 GHz* above (with the
  author's HackRF, 56 on B20 and 70 on B3); leave the second empty to use one
  gain everywhere.
- **Clock**: with `auto`, the clock error is measured on the first band that
  has LTE cells and reused for the rest, since calibration on high bands is
  ambiguous for large errors. Put a low band first.
- **Overlapping bands**: B28's downlink (758–803 MHz) includes the lower part of
  B20 (791–821 MHz). Carriers already read in an earlier band are passed to the
  next sweeps with `-x` and skipped, so a B20 cell is not read again as B28.
- **Stop** ends the current band and the rest of the list.

Bands cannot run in parallel on one SDR: a HackRF has a single tuner and a
single USB stream, and only one program can open it at a time. Parallel bands
would need one SDR per band.

### Location

Each reading stores the position at the moment its first message was decoded,
from exactly one source, in this order:

1. **gpsd**, when its last 2D/3D fix is at most 30 s old (see
   [GPS receiver](#gps-receiver-optional)); the reading keeps that fix's time;
2. otherwise the **browser's geolocation**, which on a laptop is Wi-Fi based
   (typically 20–100 m); the browser asks for permission first;
3. if that is missing or not good enough, **Correct on map** and click where you
   are: this manual position is kept until you press **Use browser** again.

While gpsd has a fix, the browser and map buttons are disabled. The source
(`gpsd`, `browser` or `manual`) and accuracy are saved with every reading.
Scans run from the command line use gpsd, or the last browser/map position if
the web app is running.

### Security and privacy

- The server listens on `127.0.0.1` only: it can start the SDR, so it is not
  exposed to the network.
- Requests that change state must be JSON and addressed to `localhost`, so a
  web page open in another tab cannot start or stop scans (no CSRF, no DNS
  rebinding).
- Map tiles come from OpenStreetMap's servers, which therefore see which area
  the map shows. Everything else stays on your machine; `vol/output/` is
  ignored by git because readings reveal where they were made.

### Run at boot (systemd)

To start the web app automatically when the machine boots:
```bash
docker compose build                   # the service does not build the image
sudo systemd/install-service.sh        # install, enable and start
```
The script writes `/etc/systemd/system/lte-sib-parser-webapp.service` from
`systemd/lte-sib-parser-webapp.service.in` (filling in this folder and the path
of `docker`), enables `docker.service`, and enables and starts the unit, which
runs `docker compose up --no-build webapp` in this folder after Docker starts.

```bash
systemctl status lte-sib-parser-webapp
journalctl -u lte-sib-parser-webapp -f          # server log
sudo systemctl stop lte-sib-parser-webapp       # stop until next boot
sudo systemd/install-service.sh --uninstall     # remove
```
Moving the project folder requires running the install script again. Stop a
web app started by hand (`docker compose stop webapp`) before installing, or
the two will compete for port 8080.

## 2G (GSM)

`vol/scripts/gsm_scan.py` reads every GSM cell of GSM-900 (E-GSM, 925–960
MHz) and DCS-1800 (1805–1880 MHz) from a few wide captures: BSIC, MCC/MNC,
LAC, cell ID (CI) and the system information messages (SI1, SI2 with the
neighbour list, SI3, SI4, SI13, SI2bis/ter/quater), one reading per cell in
the [readings database](#readings-database). Nothing is transmitted.

- **Web app**: mode *2G only (GSM 900/1800)*, or tick **Also 2G** to run it
  after any LTE scan (e.g. the known-EARFCN preset). Needs the SDR set to
  bladeRF or HackRF. The 2G part is a scan of its own: in the table's scan
  filter it is marked *2G* (choose *all* to see LTE and GSM together).
- **Command line** (inside the container):
  ```bash
  python3 scripts/gsm_scan.py                          # bladeRF, both bands
  python3 scripts/gsm_scan.py --sdr hackrf --ppm 17    # HackRF: pass its clock error
  python3 scripts/gsm_scan.py --bands 900 --gain-low 20
  ```
- **bladeRF**: 56 MSPS with a 50 MHz filter, so GSM-900 is one capture and
  DCS-1800 two, 1.2 s each, all three in one `bladeRF-cli` session (6.1 s);
  each is decoded as soon as it is complete. Default gains 15 (900) and 30 (1800), for a Cisco LTE antenna
  (40 clipped at 1.8 GHz next to strong LTE carriers); a warning is printed
  when more than 0.2 % of the samples clip.
- **HackRF**: 20 MSPS, 3 + 5 captures; gains `24,16` / `32,20` (lna,vga,
  `LTE_HACKRF_LOW_GAIN` is honoured below 1 GHz). Give its clock error with
  `--ppm` (the web app passes the one measured by the LTE steps); without it
  the FCCH search covers ±45 kHz. **Not yet tested with a HackRF.**
- **Time**: ~10 s for both bands with a bladeRF on a 4-core i7-8550U; 25–30
  cells, 23–24 with the full CGI (`MCC-MNC-LAC-CI`), at a site with three
  operators on GSM-900 and one on DCS-1800.
- **Decoder**: `gsm_decoder` (C++, FFTW, in the image) does the signal
  processing; `vol/scripts/gsm_decode.py` parses its SI messages, and is also
  a complete numpy version of the same decoder (used when `gsm_decoder` is
  missing or `LTE_GSM_NUMPY=1`; ~3x slower overall).
- Captures go to `/dev/shm` (docker-compose gives the containers 2 GB; `/tmp`
  otherwise): up to three captures of 280 MB at a time; the decoder needs
  another ~300 MB of RAM.
- `vol/scripts/gsm_decode.py` also decodes a capture file:
  `gsm_decode.py file.iq -f 942.5e6 -r 56e6 -w 50e6 -b 900` (`--int8` for a
  HackRF file), one JSON line per cell.

Limits: SI2 neighbour lists are decoded only in the "bit map 0" format
(GSM-900); other formats (range 128/256/512/1024, variable bit map, used on
DCS-1800) are kept as hex. The level is in dBFS, not dBm (not calibrated).
GSM readings have a bandwidth of 0.2 MHz (every GSM carrier is 200 kHz wide;
filled in for older readings when the database is opened). Each GSM reading
has an **RSSI** (the BCCH carrier's power, what a phone
reports as RxLev) in dBm in the `rsrp` column, shown in the table's
*RSRP / RSSI* column and exported: the level in dBFS minus the capture gain,
with srsue's offset (`dBFS + 30 - (gain + 62)`, `readings_db.gsm_rssi`). It is
not calibrated: compare GSM readings of one SDR with each other, not with the
LTE RSRP, which srsRAN takes after an unnormalised FFT and comes out tens of dB
higher (e.g. -35 dBm LTE next to -82 dBm GSM). Readings saved before it are
filled in from their level and the scan's gain when the database is opened.
Cells weaker than ~-70 dBFS give the BSIC but often no SI3 in 1.2 s; the web
app shows them as "BSIC only (weak signal)" (or "no SI3" when other SI
decoded). They are GSM cells, not 3G: the BSIC comes from the SCH, found via
the FCCH tone, and a UMTS carrier (5 MHz of WCDMA) has neither. A cell next to
a stronger one (e.g. 200 kHz away) can also stop at the BSIC. 3G is not
supported.

## Readings database

`vol/output/readings.sqlite` (option `-R`) keeps every reading instead of one
row per EARFCN, so the same cell read at different places or times gives
separate rows.

In the web app, **Export CSV** (next to the Scan filter) downloads the chosen
scan, or every scan with the filter on *all*: one row per reading with every
column (MIB, SIBs and GSM system information as JSON) plus the scan's start
and band, `rat` = `LTE` or `GSM` (`/api/export.csv[?scan_id=N]`). **Clear DB…**
deletes every reading and scan after a confirmation (export first: it cannot
be undone); the Known EARFCNs list is kept, and it is refused while a scan
runs.

Table `scans`: `id`, `started`, `finished`, `band`, `ppm`, `args`.
Learned EARFCNs are kept apart, in `vol/output/earfcns_learned.json` (see
[Known EARFCNs](#known-earfcns)).

Table `readings`:

| Column | Content |
|---|---|
| `id`, `scan_id` | reading and scan |
| `time`, `updated` | first decoded message and last update (UTC, ISO 8601) |
| `earfcn`, `band`, `dl_freq_mhz` | carrier |
| `pci` | physical cell ID (from srsue) |
| `mcc`, `mnc`, `plmns` | first PLMN, and all PLMNs of a shared cell (`268-01 268-03`) |
| `tac` | tracking area code |
| `eci`, `enb_id`, `cell_id` | 28-bit E-UTRAN cell identity, split into eNB ID (`eci >> 8`) and cell ID (`eci & 0xff`) |
| `cgi` | cell global identity `MCC-MNC-ECI`, as phones show it (e.g. `268-02-26040502`) |
| `rsrp` | reference signal received power, dBm |
| `bandwidth_mhz` | channel bandwidth: from the MIB when decoded, else estimated by the sweep |
| `detection` | `srsue` or `decoder` (decoded), `pss` (found by its sync signals only, see below), `gsm` |
| `rat` | empty for LTE; `GSM` for a 2G cell, whose ARFCN is in `earfcn`, BSIC in `pci`, LAC in `tac`, CI in `cell_id`, CGI `MCC-MNC-LAC-CI` in `cgi`, RSSI in `rsrp`, band `GSM900`/`DCS1800` |
| `gsm` | 2G: BSIC, level (dBFS), frequency error and every decoded SI message (JSON: hex plus the parsed fields) |
| `lat`, `lon`, `accuracy_m`, `location_source`, `location_time` | position of the reading (see [Location](#location)) |
| `mib`, `sib1` … `sib13` | decoded messages as JSON, as in `cells.sqlite` |

A `pss` reading is a carrier the sweep found and identified (EARFCN, PCI,
bandwidth, location) but did not decode, because the SDR cannot follow it: with
a HackRF, 20 MHz cells. It has no RSRP, CGI or SIBs. Columns added later
(`bandwidth_mhz`, `detection`, `rat`, `gsm`) are added to older databases
automatically. GSM readings are left out of the learned EARFCNs.

```bash
sqlite3 vol/output/readings.sqlite "SELECT time, earfcn, pci, cgi, rsrp, lat, lon FROM readings"
```

## bladeRF usage

A bladeRF 2.0 micro (AD9361, 12-bit, up to 61.44 MSPS, 47 MHz–6 GHz) goes
through srsRAN's native plugin, built against libbladeRF 2.6.0 in the image:

```bash
./sib-scan.sh -K "$(grep -o '^[0-9]*' helpers/earfcns/portugal.txt)" -p auto -d bladeRF -g 30 -G 40 -t 45
```

- **MIB/SIBs are decoded by `lte_sib_decoder`**, not srsue (decision 48): the
  bladeRF is opened once per scan (~8 s) and each carrier takes ~1-3 s. `-U`
  goes back to srsue; srsue's `-t`/`-T` do not apply to the decoder.
- **20 MHz cells are decoded** (30.72 MSPS), so no `-W` list: in the web app,
  choose device *bladeRF* (device args empty) and the known-EARFCN preset
  sends 20 MHz carriers to srsue too.
- **Clock**: factory-calibrated VCTCXO, measured at ~1 ppm (0.8 kHz at 796 MHz,
  2.5 kHz at 2.6 GHz), within srsRAN's tolerance. `-p auto` still works.
- **Gain**: much lower than a HackRF's; 30 on B20/B8 and 40 on B3/B1/B7 worked
  where the signal is strong, 40 already saturated on B20 there (MIB SNR 1.9 dB
  at 40, 11.9 dB at 30).
- **Antenna on RX1**: srsRAN and the checks use channel RX1; TX1 and TX2 can
  stay unconnected (the TX module is never enabled). **A second antenna on
  RX2** is used by `lte_sib_decoder` with `sib-scan.sh -A 2` (web app: *RX
  antennas* 2, the bladeRF default): both channels are combined (decision 55).
  srsue, the PSS/SSS checks and 2G still use RX1 only. A 1.4 GHz antenna
  there gave much weaker B3/B7 detections than a wideband one. A Cisco
  4G-LTE-ANTM-D (LTE dipole, 698–960 / 1710–2690 MHz) gave +17.6 dB on B20 and
  +2.9 dB on B8 over the generic telescopic antenna, and about the same on
  B3/B7 — with it, use gain **15** below 1 GHz (30 saturated B20: MIB SNR
  1.6 dB at 30, 12.5 dB at 15) and 40 above.
- The PSS/SSS checks capture with `bladeRF-cli`, which must switch the AGC off
  before a manual gain is accepted.
- **Fixed 30.72 MSPS** (`-r 30.72e6`, the default with `-d bladeRF`): changing
  the AD9361's sample rate is slow (see decision 39), so the hardware stays at
  30.72 MSPS and srsue decimates in software. 15 MHz cells (23.04 MSPS, not an
  integer divisor) would fail; none is on the Portuguese list.
- **Sweep mode (`-S`) without a list** (decision 49): the band is captured in
  ~23 MHz pieces at 30.72 MSPS (2 s each) and `lte_sib_decoder` searches every
  EARFCN of each capture and decodes the cells from the same samples. In the
  web app choose *Sweep* with the bladeRF (e.g. the "Portugal sweep" preset):
  ~4 min for B20, B8, B28, B3, B1, B7; the known-EARFCN preset is faster
  (~1.5 min) when the carriers are known.
- **Use `-t 45`**: srsue takes ~10 s longer to start on a bladeRF, and with
  `-t 30` some cells that decode fine by hand (B3 1875, B7 2800) ran out of
  time in a full run.

Measured (known-EARFCN list, 21 EARFCNs, `-g 30 -G 40 -t 30`, strong-signal
site): 5:45 in total; 14 EARFCNs had a cell and **11 were fully decoded,
including six 20 MHz cells** (B3 1815/1835, B1 2120.3/2140/2160, B7 2640),
which a HackRF can only record as detected. The RSRP a bladeRF reports is not
calibrated to the HackRF's: compare values within one SDR only.

## Gain and antennas

The web app's SDR defaults (and the values in this README) were measured with a
**Cisco 4G-LTE-ANTM-D** LTE dipole on the SDR's RX input, at a site with strong
signals. **Another antenna, another place or another SDR unit needs other
values**: gain is the setting that most often decides whether a cell decodes.

Measured so far:

| SDR | Antenna, place | srsue gain < 1 GHz / ≥ 1 GHz | PSS/SSS capture gain | `-t` |
|---|---|---|---|---|
| HackRF One | stock telescopic, home (weaker signals) | 56 / 70 | 32,20 (default) | 30 |
| HackRF One | Cisco 4G-LTE-ANTM-D, work (strong) | 44 / 56 | 24,16 below 1 GHz | 30 |
| bladeRF 2.0 micro | generic wideband, work | 30 / 40 | 30 | 45 |
| bladeRF 2.0 micro | Cisco 4G-LTE-ANTM-D, work | 15 / 40 | same as srsue gain | 45 |

Signs that the gain is wrong:

- **Too high** (strong signal saturates the ADC or overloads the front end):
  cells are found but the MIB decodes with a low SNR (e.g. `snr=1.6 dB` in
  srsue's log) and SIBs rarely follow; PSS/SSS scores drop on the strongest
  carriers (a HackRF at 32,20 gave SSS 0.12 on B20 with the Cisco antenna);
  with a bladeRF, `bladeRF-cli` captures near full scale (±2048). Lower bands
  usually saturate first: a good antenna adds more there (+17.6 dB on B20 for
  the Cisco over the stock antenna, about the same on B3/B7).
- **Too low**: no cell found or PSS peaks near 1–2 (noise), weak cells missing.

How to find the values for a new setup (inside the container):

```bash
# PSS/SSS on a few known EARFCNs, one low and one high band at a time
python3 scripts/check_earfcns.py -v --sdr bladerf --gain 20 -e "6200 3625"
python3 scripts/check_earfcns.py -v --sdr hackrf -l 24 -g 16 -e "6200 3625"
# srsue on one cell: look at "MIB decoded ... snr=" and the number of SIs
DEV=bladeRF ARGS= SECS=25 helpers/srsue-debug.sh 6200 15
helpers/srsue-debug.sh 6200 44 --rf.freq_offset 16000     # HackRF: pass its clock offset
```

Aim for an MIB SNR of roughly 8 dB or more and all SIBs within ~15 s, then
put the values in the web app's form (or edit `SDR_DEFAULTS` in
`vol/webapp/static/app.js` to make them the defaults).

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
| `lte_sib_decoder -e <earfcn> -d bladeRF -r 30.72e6 -g <gain> [-v]` | (in the image) decode one carrier's MIB/SIBs without srsue; `-s` reads "earfcn gain offset_hz logfile" lines on stdin (how `sib-scan.sh` uses it), `-v` shows the cell search and SI decoding |
| `vol/helpers/fftw-warmup.sh [ue.conf]` | compute and save srsue's FFTW plans without an SDR (`sib-scan.sh` runs it at start; ~1 s once saved) |
| `vol/helpers/srsue-debug.sh <earfcn> <gain> [srsue args]` | run srsue for 15 s with verbose logs, show sync peaks and decoded messages (`DEV=bladeRF ARGS= SECS=25` for a bladeRF) |
| `vol/scripts/readings_db.py` | readings database schema, SIB1 → CGI decoding, `new-scan`/`set-ppm`/`end-scan` commands |
| `vol/scripts/location.py` | current position: gpsd, else the web app's location file |
| `vol/webapp/server.py` | the web app (`--port`, `--db` readings database, `--learned` learned-EARFCN file) |
| `vol/scripts/gsm_scan.py [--sdr hackrf] [--bands 900 1800]` | 2G scan: GSM-900/DCS-1800 cells into the readings database (see [2G (GSM)](#2g-gsm)) |
| `vol/scripts/gsm_decode.py <file.iq> -f <Hz> -r <rate>` | decode every GSM BCCH in a capture file (library used by `gsm_scan.py`; numpy version of `gsm_decoder`) |
| `gsm_decoder -i <file.iq> -f <Hz> -r <rate> -a "<arfcns>"` | (in the image) the GSM decoder in C++: one JSON line per cell, SI messages as hex |
| `vol/scripts/wide_chunks.py -b <band>` | split a band into the wide captures used by `sib-scan.sh -S` with a bladeRF (centre and EARFCN range per capture) |
| `vol/scripts/check_earfcns.py -e "<earfcns>"` | check EARFCNs for cells with PSS/SSS, measure the clock (`-p auto`) |
| `vol/webapp/demo/make_demo_db.py <db>` | readings database with fictitious data (test PLMN 001-01), for demos and screenshots |

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
    Python `__pycache__/` ignored; `notes/` ignored (private working notes).
12. **`run.sh` no longer runs `xhost +`.** It disabled X server access control
    for every client (local and remote) until `xhost -`, only so a GUI in the
    container could open windows, which the scan never does. If a GUI is
    needed, `xhost +SI:localuser:root` grants access to the local root user only.
13. **Readings database** (`readings_db.py`, `-R`). `cells.sqlite` has one row
    per EARFCN (`UNIQUE`), so a scan elsewhere overwrote earlier results; a map of
    readings needs every reading. The new database keeps all of them with
    location, PCI, RSRP, the identity decoded from SIB1 (MCC, MNC, all PLMNs,
    TAC, ECI → eNB ID and cell ID, CGI) and every field of `cells.sqlite`.
    `cells.sqlite` is still written, so the `dbparsers` scripts keep working.
    Each run of `sib-scan.sh` is recorded in `scans`, registered before
    calibration or sweep so a scan stopped early is recorded too.
14. **PCI from srsue's standard output.** With `sib_logger.patch` the PHY log
    has no PCI at the default level; srsue prints `Found Cell: … PCI=n` on
    stdout, which used to go to `/dev/null`. It now goes to `/tmp/ue.out` and
    the PCI of the last cell found (the one srsue camps on) is saved when the
    MIB or SIB1 arrives; a later miss never erases a known PCI.
15. **Location from one source at a time**: gpsd with a fix, else the browser,
    which the user can override on the map (see [Location](#location)). A manual
    position is kept until the user switches back to the browser, so a poor
    Wi-Fi fix does not silently replace it.
16. **Web app with the Python standard library** and Server-Sent Events: no new
    dependency in the image; Leaflet and OpenStreetMap tiles are loaded by the
    browser. It runs in the same image as a compose service with host
    networking (to reach gpsd and listen on the host's loopback only).
    State-changing requests must be JSON with a `localhost` Host header.
17. **`init: true` in docker-compose.** Stopping a scan kills its process group;
    the orphaned srsue/hackrf processes stayed as zombies because PID 1 was the
    Python server. Docker's init now reaps them.
18. **srsue's FFTW plans kept in a volume** (`srsran-home` mounted at `/root`).
    A new container spends >15 s planning FFTs on its first srsue run, longer
    than the `-t` timeout, so the first EARFCN of every new container failed.
19. **Container shell with `docker compose run --rm worker`** documented as the
    default instead of `run.sh`.
20. **Several bands in one run** (web app). Implemented in the server as a list
    of `sib-scan.sh` runs rather than in the script, so each band keeps its own
    scan record and log. The ppm measured on the first band is reused: on high
    bands the raster/clock split is ambiguous (decision 6). A second gain for
    bands above 1 GHz, because B3 needed `-g 70` where B20 used 56.
21. **Overlapping bands** (`-x`). B28 (758–803 MHz) contains B20's 791–803 MHz:
    a test scan read a B20 cell again as B28 EARFCN 9590. Bands now run in the
    given order and later sweeps skip carriers already read (±0.25 MHz).
22. **Joined and separate spectrum blocks both tried** (sweep with PSS/SSS).
    Merging blocks closer than 0.5 MHz (decision 7) also merged two operators'
    adjacent 10 MHz B20 carriers into one fake 20 MHz block. With PSS/SSS
    available, both the joined and the separate blocks are tested, and only
    those with LTE sync at their centre are kept.
23. **Lock needs a strong SSS or two agreeing captures.** A single capture gave
    a false lock with SSS 0.42 between two carriers, while real B20 cells score
    only 0.3–0.6 (strong neighbours fill the 8-bit range; more gain made it
    worse, so the capture gain stays fixed). A lock is accepted at SSS ≥ 0.6, or
    when two captures both reach 0.25 at the same CFO (±2 kHz): a false lock
    lands at a random CFO in ±150 kHz, so two agree by chance ~1 % of the time.
    The PCI may differ between the two, as several sectors share a carrier.
24. **systemd unit around `docker compose up`** instead of a Docker restart
    policy: it starts after `docker.service`, logs to the journal, and can be
    stopped/disabled like any service. `--no-build`, because building srsRAN at
    boot would take many minutes; the install script refuses to install
    without the image.
25. **Web page fits a phone screen**: the page never scrolls sideways (the
    readings table scrolls inside its card), so a scan can be followed from a
    phone's browser.
26. **Faster PSS/SSS checks.** In a 13.8 min Portugal run, checking the
    sweep's spectrum blocks took ~4–5 min: ~30 blocks, 1–2 captures each, ~3 s
    of single-threaded CPU per capture. Now:
    - one 90 ms capture per block, split into two independent 40 ms looks
      (starting `hackrf_transfer` costs ~1 s, the signal only 40 ms);
    - each look is analysed in a process pool on all CPU cores while the SDR
      takes the next capture (the SDR itself stays sequential);
    - correlations are zero-padded to FFT sizes with only factors 2, 3 and 5:
      a 40 ms look gave 76928 = 2⁷ × 601 points, and the prime factor made each
      FFT ~7× slower;
    - the coarse offset search (121 offsets × 3 PSS) uses 20 ms; the fine search
      and the SSS check use all 40 ms.
    Measured: B3 sweep (19 blocks) 150 s → 32 s, B20 40 s → 15 s, same carriers,
    EARFCNs and PCIs.
27. **Stopwatch and theme** in the web app. The server reports when the run and
    the current band started and when the run finished; the page counts from
    those. The theme is stored in `localStorage` (a per-browser preference) and
    applied before the page paints.
28. **Carriers too wide for the SDR are recorded, not decoded** (`-S`, default;
    `-w` to disable). On a HackRF, srsue always failed on 20 MHz carriers
    (30.72 MSPS > 20 MSPS) after waiting the full `-t` timeout plus start-up,
    ~40 s each, ~1.5 min per Portugal run. Carriers whose sweep block is
    ≥ 16 MHz wide are now saved at once as `detection = 'pss'` readings with
    EARFCN, PCI (from PSS/SSS), estimated bandwidth and location. The limit is
    16 MHz, not 15 MHz-class widths, because a B8 block measured 14.7 MHz wide
    was decoded by srsue on the HackRF (its MIB later showed a 5 MHz cell: the
    sweep had joined it with a neighbouring signal). Every reading also gets
    `bandwidth_mhz` (exact from the MIB when decoded), shown as the web app's
    BW column; detected carriers count as already read for overlapping bands.
    Measured: B1 7 s instead of ~80 s.
29. **Known-EARFCN mode** (`-K`, preset "Portugal (known EARFCNs, fast)").
    In the timed Portugal sweep (7:18) most of the time outside srsue went to
    sweeping and checking spectrum blocks, and the sweep still missed carriers:
    a weak B3 carrier at 1815 MHz (EARFCN 1300) looked like fragments and was
    never found, and the B8 carrier at 942.5 MHz was missed in one run. Cells
    advertise their operator's other carriers in SIB5, which gives a list of
    exact EARFCNs; checking each with PSS/SSS found both carriers and took 64 s
    for 19 EARFCNs, calibration included. A full run with this preset took
    5:29 instead of 7:18. Running srsue directly on the list
    was rejected: an EARFCN without a cell costs the full `-t` timeout plus
    start-up (~40 s), and about half the list has no cell at any one place.
    The list is seeded from a file and grows from the readings database and
    from SIB5 during the run, because no list is complete (see
    [Known EARFCNs](#known-earfcns)). `-G` sets the gain for EARFCNs above
    1 GHz, as one run now mixes low and high bands.
30. **Learned EARFCNs kept in their own file.** The known list already grew
    from the readings database, but deleting the readings (done twice while
    testing) forgot every carrier found. `vol/output/earfcns_learned.json` keeps
    each EARFCN ever read or advertised, with dates, bandwidth and operators; it
    is outside git because regional carrier variants show where you have been.
    The web app shows it in the Known EARFCNs panel.
31. **Screenshot with fictitious data.** A screenshot of real use shows CGIs,
    PCIs, TACs and the user's position, which must not be published.
    `vol/webapp/demo/make_demo_db.py` builds a database with the 3GPP test
    network PLMN 001-01, made-up identities and positions around Praça do
    Comércio (Lisbon); the server's `--db`/`--learned` options serve it on
    another port. The screenshot was taken with headless Firefox
    (`firefox --headless --screenshot`); the `?view=` parameter exists so the
    map tiles load with the page instead of after it.
32. **EARFCN list cross-checked with community data.** The Portugal Towers
    spectrum page lists the carrier centres its users' phones measured, per
    operator and band. Converted to EARFCNs they matched the SIB5-derived list
    exactly for B20/B8/B3/B1/B7, which suggests that list is close to complete
    nationally for those bands; B28's two carriers (9359, 9468) were missing
    and were added even though they showed no LTE sync here (probably 5G NR),
    because checking an EARFCN costs ~2 s.
33. **Favicon and sortable table.** `vol/webapp/static/favicon.svg` (antenna
    mast with radio waves) is the tab icon and the header logo;
    `favicon.ico` (16–64 px, served at `/favicon.ico` as browsers expect) was
    made with `rsvg-convert` and ImageMagick, its 16 px image from the simpler
    `favicon-16.svg` because the full drawing blurs at that size. Sorting is
    done in the page (every reading is already there); on a first click, time,
    RSRP and SIBs sort descending (newest, strongest, most complete first),
    the other columns ascending.
34. **bladeRF 2.0 micro support.** The board (xA5, firmware v2.6.0, FPGA v0.16.0
    — the current Nuand release 2025.10) did not work with the Ubuntu 22.04 or
    Nuand PPA libbladeRF (2.4.1): srsue reported constant overruns and never
    found a cell. The image now builds libbladeRF 2.6.0 (tag 2025.10) from
    source. With it srsue found the cell and decoded the MIB but never the SIBs:
    the srsRAN plugin read `SC16_Q11_META` samples with
    `BLADERF_META_FLAG_RX_NOW` on every call, which dropped samples buffered
    between calls (e.g. 9380 of 15360 valid), and every overrun made srsue
    re-sync the SFN ("Detected overflow, trying to resync SFN"), so the cell
    was never camped long enough for SIB1. `worker/bladerf_rx.patch` reads RX
    as a continuous `SC16_Q11` stream and counts samples for the RX time (TX
    uses the same format, as libbladeRF requires, and never transmits). Result:
    SIB1 + all SIs on 10 MHz cells and on a 20 MHz B1 cell (PRB 100, 30.72 MSPS).
    The SoapyBladeRF path was tried as an alternative and found no cell.
35. **PSS/SSS checks with a bladeRF** (`lte_pss.configure`, `check_earfcns.py
    --sdr bladerf --gain`): captures with `bladeRF-cli` (AGC off, manual gain,
    SC16 Q11). The same 15 EARFCNs took 63 s and 14 had a cell (SSS 0.5–0.99),
    against ~9 with SSS 0.3–0.6 on the HackRF at another place.

36. **srsue retries for confirmed cells** (`-y`, default 1 with `-K`/`-S`).
    In two bladeRF runs of the known-EARFCN list, PSS/SSS found 14 and 15
    cells but srsue decoded 11 and 9: it failed on different cells each time
    (1700/1875/2800 in one run, 6200/3475/300/… in the other, some with SSS
    0.87), so the failures are random, not tied to a cell or band. A failed
    EARFCN is queued again for the end of the scan; success is judged from
    this scan's rows in `readings.sqlite`, since `cells.sqlite` keeps MIBs of
    earlier scans and made the old neighbour-skip check (which now uses the
    same test) count them too. Only confirmed cells are retried: in `-q` mode
    there is no confirmation and a retry could be a wasted `-t`.

37. **Browser position resent after a server restart.** The page only sends
    the browser's position when it moves 5 m or its accuracy changes, so after
    a server restart (e.g. `systemctl restart`) the server had no position and
    readings were saved without one until the user moved or reloaded the page.
    The page now resends its last position when the server reports none.

38. **bladeRF TX module never enabled.** srsRAN's plugin configured and enabled
    the TX module whenever it started receiving. No sample was ever sent
    (`rx_only.patch` stops every TX path in the radio), but an enabled
    transmitter with no load on its port contradicts "never transmits".
    `bladerf_rx.patch` now leaves TX unconfigured and disabled when RX starts;
    the only remaining enable is in the send path, which `rx_only.patch` makes
    unreachable.

39. **bladeRF at a fixed 30.72 MSPS.** Per cell, srsue searches at 1.92 MSPS
    and decodes at 15.36 or 30.72 MSPS. On a bladeRF 2.0 each change
    reconfigures the AD9361: measured with `bladeRF-cli`, 1.56 s to 1.92 MSPS,
    0.60 s to 15.36 MSPS, ~0.35 s per filter bandwidth, 4.1 s for a full
    1.92 → 15.36 cycle (near instant on a HackRF). The time between MIB and the
    first frame at the decoding rate was ~2.9 s, and when the cell was lost
    srsue paid the cycle again. With `--rf.srate 30.72e6` the hardware rate
    never changes and srsRAN's radio decimates in software (integer ratios for
    1.4/3/5/10/20 MHz cells). srsRAN's bladeRF plugin then failed ("Error
    receiving samples"): with 16x decimation the radio asks for up to 153600
    samples per read, more than its 122880-sample conversion buffer, so
    `bladerf_rx.patch` now reads in pieces. Measured: SIB1 9.2 s after start on
    a B20 cell (it had timed out or taken ~12–13 s), 10.6 s instead of 11.3 s
    on a 20 MHz B1 cell. The remaining ~9 s per cell before the search starts
    is srsue start-up (PHY init ~6.5 s), the same with a HackRF.

40. **Two SDRs in parallel: tried, slower on this laptop.** bladeRF (20 MHz and
    unknown-width EARFCNs) and HackRF (5/10 MHz EARFCNs), each with its own
    `sib-scan.sh -K ... -n` at the same time, took 7:40 with 11 of 13 cells
    decoded and 4 first-attempt failures, against 5:32 and 12 of 12 with the
    bladeRF alone. The bladeRF failed only while sharing the CPU: two real-time
    srsue instances (the bladeRF one at a fixed 30.72 MSPS) plus the parallel
    PSS/SSS checks on an 8-core laptop starve each other. What remains from the
    test: `sib-scan.sh` is safe to run as several instances (per-instance srsue
    log/stdout files; it waits for its own srsue, `pidof` also matched the other
    SDR's and killed it), `-n` now also stops the SIB5 follow-up in `-K` mode,
    and `LTE_HACKRF_LOW_GAIN=lna,vga` sets the HackRF capture gain below 1 GHz.
    With a Cisco 4G-LTE-ANTM-D, a HackRF needed capture gain 24/16 below 1 GHz
    (32/20 overloaded it: SSS 0.12) and srsue gain 44 below / 56 above.

41. **Per-SDR defaults in the web app.** The best settings differ a lot between
    the SDRs (gain 44/56 vs 15/40, `-t` 30 vs 45, a HackRF capture gain) and
    were easy to get wrong. The SDR selector fills them in with the values
    measured with a Cisco 4G-LTE-ANTM-D at a strong-signal site (with the
    HackRF's stock antenna, 56/70 had worked at home). The HackRF capture gain
    below 1 GHz reaches `sib-scan.sh` as `LTE_HACKRF_LOW_GAIN`; the server only
    accepts `lna,vga` numbers.

42. **Sweep mode only with a HackRF.** A web app scan with the SDR set to
    bladeRF but the mode left on *Sweep* ran `hackrf_sweep` and the clock
    calibration on the HackRF that was also plugged in (18.8 ppm), then srsue
    on the bladeRF (~1.2 ppm) with that correction, ~14 kHz off: no cell
    decoded, one garbled MIB. Now choosing the bladeRF in the web app disables
    *Sweep* and selects the known-EARFCN preset, the server rejects sweep with
    any SDR but a HackRF, and `sib-scan.sh -S` refuses other devices before it
    registers a scan or calibrates.

43. **Retry and time out on SIB1, not on the MIB.** A bladeRF run found 14
    cells in 8:26: 12 complete and two (20 MHz, B1 2120.3 and B7 2680) with a MIB
    but no SIB1, hence no CGI. Those two counted as decoded, so they were not
    retried, and each waited the full `-T` (30 s) after the MIB for a SIB1 that
    never came. Now the countdown after the MIB is 10 s until SIB1 arrives, a
    retry is decided on SIB1 (`has_mib.py --sib1`), and a retry completes the
    reading row of the first attempt instead of adding a second one.

44. **srsue's FFTW plans computed before the scan, without an SDR.** srsue
    plans its FFTs with `FFTW_MEASURE` at start-up and saves them
    (`~/.srsran_fftwisdom`) only when it exits, but `sib-scan.sh` stops it with
    `kill -9`. On an ARM64 VM (Ubuntu 26.04 under QEMU on a MacBook M4,
    bladeRF over USB 3 passthrough) planning took ~20 min, so srsue never got
    past "Waiting PHY to initialize" before the per-cell timeout, and every
    attempt started over.
    `vol/helpers/fftw-warmup.sh` runs srsue with the `file` RF device reading
    `/dev/zero` until the PHY is up, then stops it with SIGINT so it saves the
    plans; `sib-scan.sh` calls it once at the start (~1 s when the plans exist).
    srsue is still killed with `-9` per cell on real hardware (see 45): a
    SIGINT costs 5 s per cell ("Couldn't stop after 5s") and the plans made
    after the cell is found are quick. On the VM, with the plans saved, srsue found a B20 cell 25 s after
    start with the bladeRF and decoded SIB1 with 2 overflows per minute.

45. **srsue stopped with SIGINT inside a VM.** In the same VM, the bladeRF
    vanished from the guest ("USB Device [2cf0:5250] disconnected (fatal IO
    error)" in QEMU) right when `sib-scan.sh` killed srsue with `-9` while it
    was still searching for the cell (twice, at the `-t` timeout), and the
    retry failed with "Unable to open device". srsue stopped with SIGINT closes
    the SDR itself and the device stayed. `sib-scan.sh` now sends SIGINT and
    waits up to 15 s when `/sys/class/dmi/id/sys_vendor` names a hypervisor
    (QEMU, Parallels, VMware, VirtualBox); natively it keeps `kill -9`, which is
    ~5 s faster per cell.

46. **bladeRF at 15.36 MSPS on arm64.** In the same VM srsue found the B20 cell
    but kept losing it (16 "Found Cell" in 60 s) and SIB1 took 30-50 s or never
    came: its SYNC thread, which reads and decimates the fixed 30.72 MSPS, ran
    at 97% CPU. At 15.36 MSPS it ran at 57%, srsue kept the cell and a
    `sib-scan.sh -K` run decoded MIB and SIB1-5 in one attempt. Letting srsue
    change the rate itself found no cell in 50 s (see the bladeRF section).
    `sib-scan.sh` defaulted to `--rf.srate 15.36e6` for `-d bladeRF` on
    arm64, and the web app passed 20 MHz EARFCNs as `-W` there. Replaced by 47.

47. **bladeRF USB buffers of 32768 samples.** Profiling decision 46 with gdb
    showed the thread at 97% was not srsue's but libbladeRF's stream thread
    (it inherits the name SYNC), in `ioctl` submitting USB transfers. The
    plugin sized its RX buffers from the sample rate when the stream starts,
    1.92 MSPS, i.e. 1024 samples: ~30000 transfers per second at 30.72 MSPS,
    each an expensive call in a VM. On a 20 MHz B3 cell in the arm64 VM:

    | RX buffer (samples) | stream thread CPU | SIB1 after start | SI messages in 60 s |
    |---|---|---|---|
    | 1024 (old) | 96% | 44 s | 10 |
    | 4096 | 44% | 50 s | 12 |
    | 16384 | ~10% | 14 s | 25 |
    | 32768 | ~10% | 11 s | 59 |

    `bladerf_rx.patch` now configures 32768-sample buffers (32 buffers, 16
    transfers), and decision 46 is reverted: 30.72 MSPS again on every
    platform, 20 MHz cells decoded with srsue in the VM (a 10 MHz cell: SIB1
    after 12 s). Portugal preset in the VM afterwards: 13 cells, all 13 with
    SIB1 (5 of them 20 MHz), in 9:37; before the fix 6 of 13 in 15:06. The
    same small buffers were used natively and may explain
    part of the random srsue failures with a bladeRF there (decision 36).

48. **`lte_sib_decoder` instead of srsue (bladeRF).** srsue is a whole UE: ~9 s
    to start per cell (plus ~7 s to open a bladeRF), then it camps and collects
    SIBs at its own pace. `worker/sib_decoder/lte_sib_decoder.cc` uses
    libsrsran's PHY directly, as `pdsch_ue` does: PSS/SSS search and PBCH at
    1.92 MSPS, then `ue_sync` + `ue_dl` at the cell's rate with SI-RNTI in the
    subframes that can carry SIB1 and SI messages, ASN.1 decoding with srsRAN's
    RRC library. It writes the same `Content:`/`powermeasure`/`Found Cell` lines
    as srsue, so `parse_save_sib.py` and the databases are unchanged; a final
    `[decoder] done` line ends each carrier's log. `sib-scan.sh` runs it as a
    coprocess for the whole scan (`-s`: one "earfcn gain offset log" line per
    carrier), so the SDR is opened once; it is closed while `bladeRF-cli` does
    the PSS/SSS checks. Measured on the bladeRF in the arm64 VM:

    | | srsue | lte_sib_decoder |
    |---|---|---|
    | open the bladeRF | per cell (~7 s) | once per scan (8.3 s) |
    | one carrier, MIB to all SIBs | ~25-60 s | 0.4-3 s (up to ~10 s on weak cells) |
    | Portugal preset (web app) | 9:37, 13 cells with SIB1 | 2:32, 14 cells with SIB1; **1:18, 15 cells** without the pre-check |

    Details that mattered: candidates of the cell search are only accepted
    once their PBCH decodes (a false PSS/SSS hit at PSR ~3 otherwise blocked the
    carrier); srsue's receiver settings are needed (channel-estimator filter,
    CFO from the reference signals fed back to `ue_sync`, 8 turbo iterations:
    with the fields left at zero the SNR fell to ~1 dB within seconds); SI
    messages sent with DCI 1C carry no redundancy version and ue_dl applies
    SIB1's formula to them, so the four RVs are tried; a PBCH read every 32
    frames and a subframe-continuity check keep the SFN right after lost
    samples; `ue_sync` is reset when "subframe 0" carries no PBCH (a wrong SSS
    decision stays wrong while tracking). Readings get `detection = decoder`.
    Some cells also send SIB24 (NR neighbours), which srsue never reported;
    `parse_save_sib.py` used to crash on it (no such column in `cells.sqlite`)
    and lose the rest of the carrier's SIBs.
    Default with `-d bladeRF` (`-X` elsewhere, untested with a HackRF; `-U`
    for srsue). With `-K` and a bladeRF the PSS/SSS pre-check (`bladeRF-cli`
    captures, ~75 s of the 2:32) is skipped: the decoder's own search finds the
    cells (it found B1 2140, which the pre-check had missed), an empty EARFCN
    costs ~5 s, SIB5 neighbours go straight to the decoder, retries only happen
    where a MIB was decoded, and `-p auto` is the median of the cells' CFO in
    ppm (1.16, the pre-check had measured 1.07-1.17), recorded but not applied. Not yet: soft combining of SI retransmissions, which would help
    low-SNR cells (B8 3475 at ~4 dB often gives SIB1 only).

49. **Wide captures with the bladeRF: carriers found without a list.** With
    the decoder at ~1 s per carrier, decoding several known carriers from one
    capture saves little; what a capture wider than one carrier adds is looking
    at every EARFCN it covers, which gives the bladeRF a sweep mode (`-S`, until
    now HackRF-only). `lte_sib_decoder` got a capture source: 2 s of samples in
    RAM, and per EARFCN a frequency shift (NCO) before the usual decimation,
    with time counted in samples (offline decoding runs faster than real time:
    all SIBs of a carrier in 0.2-0.7 s). Commands in `-s` mode: `wide` (known
    carriers from one capture) and `scan` (capture, probe each EARFCN with
    PSS/SSS and two matching PBCH decodes, then decode the cells found; hits
    within 3 EARFCNs of a stronger one are aliases). `vol/scripts/wide_chunks.py`
    splits a band into captures with non-overlapping EARFCN ranges that fit in
    0.4 x the rate - 0.6 MHz each side of the centre; `-x` (MHz already read in
    an overlapping band, e.g. B20 inside B28) is honoured. The web app allows
    *Sweep* with the bladeRF again (decision 42 blocked it because it meant
    `hackrf_sweep`); a bladeRF sweep never touches a HackRF.
    Measured in the arm64 VM: ~150-230 EARFCNs probed in 3.5-4 s per capture;
    B20 (2 captures) 22 s including opening the bladeRF; the Portugal sweep
    preset (17 captures) 4:01, 16 carriers / 17 cells found (two on B1 2160),
    15 with SIB1 — every carrier of the known list, without it.
    What went wrong on the way: the first probes missed ~1 cell in 3 because
    each capture began with samples buffered before the retune (libbladeRF
    holds ~50 ms; now 200 ms are dropped); and a single PBCH decode was not
    enough: its 16-bit CRC, tried over 40 frames x 4 SFN offsets x 3 antenna
    counts, passed by chance about once per band ("PCI 0" hits).
    Later: the probe tries the PBCH of every PSS/SSS candidate in PSR order
    (B3 1500 holds two cells, and the stronger PSS was often the one whose
    PBCH failed: found in 3 of 4 captures instead of 1 of 3), and a cell the
    2 s capture leaves at MIB or SIB1 gets a live decode at the end of the
    band (up to 4 acquisitions, decision 50). With the decoder, retries only
    happen where a MIB was decoded: SIB5 neighbours without a cell here cost
    ~5 s once, not twice. Sweeps in a row: 16 and 15 carriers, 14 with SIB1.
    61.44 MSPS (49 MHz filter) also works over the VM's USB 3 and found all
    three 20 MHz carriers of B1 and of B3 in one capture each, but probing costs
    5-10x more per EARFCN (22-41 s per capture), so 30.72 MSPS is the default.

50. **Decoder on weak or interfered cells: new acquisitions and soft
    combining.** Detailed logs of the carriers that failed (B8 3475, B3 1875,
    B7 2800) showed two kinds of attempt: good ones with ~10 dB SNR and SIB1
    within ~0.2 s, and bad ones with 1-7 dB from the first subframe on and a
    PCFICH (CFI) that changed every subframe, i.e. a bad timing lock from the
    acquisition (probably another cell with the same PSS and other timing;
    srsue has the same 60-80 % per attempt). So the decoder now waits 1.5 s
    for SIB1 instead of 4 and acquires again, up to 4 times within 12 s per
    carrier, also when SI messages stall after SIB1; what was decoded is kept
    across acquisitions. It also soft-combines retransmissions like srsue's
    MAC: SIB1's four transmissions per 80 ms and an SI message's
    retransmissions within its SI window (computed from SIB1's
    si-WindowLength, periodicities and order), with the 36.321 redundancy
    versions; the four RVs on their own remain as a fallback. On six hard
    carriers, three rounds: 15 of 18 complete (before: most rounds had several
    carriers with MIB or SIB1 only). Portugal known-EARFCN preset: 1:22, 16
    cells, 15 with SIB1 and 14 with every SIB (B7 2950 gives only the MIB).

51. **2G (GSM) with a numpy decoder on wide captures** (`gsm_scan.py`,
    `gsm_decode.py`). gr-gsm is not packaged for Ubuntu 22.04 and would pull
    GNU Radio into the image, and gr-osmosdr links the distribution's
    libbladeRF 2.4.1, which does not work with this board (decision 34). So
    the decoder is written from the specifications (3GPP 45.002/45.003/44.018)
    in Python with numpy, and captures come from `bladeRF-cli` /
    `hackrf_transfer` like the PSS/SSS checks. Chain per capture:
    - **Filter bank**: overlap-save FFT (1/8 overlap), each 200 kHz channel
      filtered (flat to 80 kHz, cosine to 130 kHz) and resampled exactly to
      2 samples per symbol (block sizes with B/M = rate/541.67 kHz). Blocks of
      ~86 k samples: the first version used 2.7 M and its FFTs did not fit in
      the CPU cache (8 processes were no faster than 2).
    - **FCCH**: the tone at +67.7 kHz, found with a differential detector
      (insensitive to a small frequency error; a coherent one lost the tone at
      1 kHz error). GMSK data also gives coherence ~0.7 at phase -45°, so only
      the real part counts (threshold 0.85). LTE carriers still give tone-like
      hits, so hits count only when another one is 10, 11, 20, 21 … 51 frames
      away (FCCH frames of the 51-multiframe), and at least 40 % of them must.
      A 0.4 s look at every channel picks the ones worth channelising in full
      (~50 of 174 on GSM-900).
    - **Frequency error** from the FFT peak of the tone around each FCCH (the
      phase of w[n]w*[n-1] was biased by GMSK samples in the window: −4 to −13
      kHz instead of +1 kHz on weak channels, which broke everything after).
    - **SCH**: BSIC and frame number; two SCH must agree (same BSIC, frame
      numbers matching the time between them): its 10-bit parity alone passed
      by chance on LTE carriers.
    - **BCCH** on frames 2–5 of every 51-multiframe: least-squares channel
      estimate on the training sequence (5 taps, all timing offsets at once),
      max-log BCJR equaliser (soft bits, batched over all bursts of a
      channel), deinterleaving, soft Viterbi (K=5) and the Fire code check.
    Captures: 1.2 s (0.8 s: 17 cells with CI, 1.2 s and 1.6/2 s: 25), at 56
    MSPS so GSM-900 fits in one (2 s at 40 MSPS, 38 MHz filter: 20 cells; at
    56 MSPS: 28).
    The next capture is recorded while the previous one is decoded; once a
    `bladeRF-cli` capture started during a decode stopped after a few ms and
    never returned, so a capture that takes 15 s longer than it should is
    recorded again. Speed on the i7-8550U (GSM-900 capture): first version
    88 s, parallel channels 21 s, then cache-sized blocks, FCCH look-ahead and
    batched equaliser ~11 s. GSM readings reuse the LTE columns (ARFCN in
    `earfcn`, BSIC in `pci`, LAC in `tac`, CI in `cell_id`) with `rat = 'GSM'`,
    so the web app's table, map and filters work unchanged; the learned-EARFCN
    list and `-x` skip them. The GSM step runs after the LTE ones in the web
    app so that a HackRF's measured clock error can be passed on. Measured:
    Portugal known-EARFCN preset + 2G with the bladeRF, 2:22 in total (LTE 16
    cells in 1:53, GSM 33 cells, 23 with CGI, in 29 s).

52. **2G in ~10 s: the decoder in C++ and one capture session.** Timing the
    29 s scan: ~3.4 s for the first capture, then decoding (11 s GSM-900,
    ~7 and ~5 s DCS-1800, 2.8 s of each being the FCCH look-ahead), with the
    later captures hidden behind it. `worker/sib_decoder/gsm_decoder.cc` is
    the same chain as `gsm_decode.py` in C++ (FFTW single precision, threads
    over blocks and over channels); on the same GSM-900 capture it found 21
    cells against 22 (one weak cell on the edge), same BSICs and SI3s, in
    3.9 s instead of 10.6 s, then 2.3 s after removing a sin/cos per sample
    (phase by recurrence), two modulos per gathered bin (and the bins outside
    the filter) and the single-threaded zeroing of ~300 MB of channel
    buffers. It prints the SI messages as hex; Python parses them, so there
    is one parser. With decoding that fast, opening the bladeRF (~2 s per
    `bladeRF-cli`) dominated: the three captures now run in one session and
    a file counts as complete when it has its full size (6.1 s for all three
    instead of ~10 s). Result: ~10 s for both bands (4 runs: 25–30 cells,
    23–24 with CGI). Also fixed on the way: the multiprocessing pool stops
    its workers with SIGTERM and they inherited `gsm_scan.py`'s cleanup
    handler, which deleted the captures (a run ended with "No such file");
    and the numpy version put its buffers in `/dev/shm` without checking the
    space (Docker's default is 64 MB), which hung the pool.

53. **Known-EARFCN preset + 2G from 1:33 to ~1:04.** Timing a run: 8 s
    opening the bladeRF, ~25 s on five EARFCNs without a cell (5 s of search
    each), ~15 s on a weak cell that the decoder acquired 4 times and
    `sib-scan.sh` then retried 4 times more, ~13 s on a cell whose SI
    messages stalled after SIB1 (6 s wait after each new SIB), ~18 s decoding
    the rest, ~8 s for 2G. Changes: the decoder's search + MIB limit is 2 s
    (cells show up within ~1 s); it waits for a missing SI message 3
    periods of the slowest one still missing (from SIB1's schedule, capped at
    `-T`, 6 s) instead of 6 s; and with the decoder `sib-scan.sh` does not
    retry a carrier (cells of a wide capture still get their live decode).
    Two runs: 1:04 and 1:03, 15 and 16 LTE cells all with SIB1 and SIB2/3
    (before: 1:33, 16 cells, 15 with SIB1), plus 25-28 GSM cells. A weak cell
    (B8 3525, -78 dBm) can now be missed by the shorter search.

54. **Co-channel sectors: blind CFI and one PCI per reading.** A B8 cell with
    RSRP -57 dBm gave no SIB1: the PSS was strong (PSR 8-10) but the SNR stayed
    at 2-5 dB and the PCFICH gave a different CFI almost every subframe. The
    carrier holds two sectors of one eNB (PCIs 3n and 3n+2) and the spot was
    between them: good RSRP, poor SINR. When no SI-RNTI DCI is found with the
    decoded CFI, the decoder now tries the other two (the PDSCH's 24-bit CRC
    guards the result). That test also showed that a new acquisition could
    lock onto the other sector and add its SIBs to the first one's reading: a
    reading now keeps one PCI once its SIB1 is in (before that, the decoder
    moves on to the new cell), and the RSRP line is written after SIB1 (or at
    the end), so it is the kept cell's. Search + MIB went back to 3 s (2 s
    once missed a strong cell). That cell: SIB1 in 8 of 8 attempts (every SIB
    in 6), none before; known preset + 2G: 1:09 and 1:07, the cell with CGI
    both times.

55. **Two receive antennas on the bladeRF (RX1 + RX2).** Where two sectors of
    one site overlap on a carrier, RSRP is high but the SINR is 2-5 dB and the
    decoder needs re-acquisitions or fails. The bladeRF has a second receive
    channel: `bladerf_rx.patch` now opens RX1+RX2 when asked for 2 channels
    (`BLADERF_RX_X2`, interleaved samples split per channel on read, gain on
    both, shared LO and rate), and `lte_sib_decoder -A 2` passes both to the
    cell search, PBCH, `ue_sync` and `ue_dl`, which combine them; captures
    keep one buffer per channel. A/B with two identical antennas, 3 rounds:
    B8 3475 (sector overlap) + B3 1875, every SIB in 9 of 9 decodes with 2
    antennas against 6 of 9 with 1, and 11-15 s against 21-26 s (fewer
    re-acquisitions). It does not help against a co-channel interferer: on
    B20 6400 one sector (PCI 65) gets its SIB1 DCI in every subframe but no
    PDSCH ever decodes, with 1 or 2 antennas (probably the other sector's
    SIB1 on the same resources; combining adds signal, it does not cancel
    interference); the other sector (63) decodes and fills the reading. Known
    preset + 2G with 2 antennas: 1:08 and 0:58, 15 and 16 of 16 cells with
    SIB1 (1 antenna: 1:09 and 1:07, 14 of 16 and 15 of 15).

56. **Warning when the image is older than the scripts.** A run on the x86
    PC took 10:49 instead of ~1:30: after `git pull` brought the RX1 + RX2
    change, `sib-scan.sh` passed `-A` to an `lte_sib_decoder` built before it,
    which refused to start, and every cell fell back to srsue. `sib-scan.sh`
    now checks that the decoder knows `-A` and otherwise says to rebuild the
    image; "Build and run" says to rebuild after a pull.

56. **gpsd: last fix up to 30 s old, without waiting.** With the phone's GPS
    relayed over Wi-Fi (GPSd Forwarder → Mac → VM), the stream stops for
    seconds, sometimes minutes. `location.from_gpsd` used to wait up to 2 s for
    the next fix: with gpsd running but no GPS, every reading paid it, and a
    known preset + 2G run took 1:48 instead of ~1:05. It now asks gpsd for its
    last fix (`?POLL`, answered in ~2 ms) and uses it if it is at most 30 s old
    (`MAX_FIX_AGE_S`); the web app's fallback to the browser position waits
    30 s too (`GPS_STALE_S`, was 5 s). Short gaps keep the GPS position; the
    fix time is stored with the reading, so its age is visible.

57. **Stop when the SDR stops answering.** In a VM with the bladeRF and a GPS
    puck on the same USB-C hub of the host, the bladeRF dropped off the bus
    mid-run (`NIOS II ... timed out`, then `fatal IO error`); the rest of the
    run, and every "Repeat until Stop" run after it, spent minutes timing out
    on each carrier. With `lte_sib_decoder`, `sib-scan.sh` now exits with code
    3 after 3 carriers in a row that showed USB errors and gave no MIB, and
    `gsm_scan.py` exits with 3 when a capture fails. The web app treats 3 as
    "SDR lost": it skips the remaining steps (e.g. 2G) and does not repeat.
    A bus-powered bladeRF needs a port (or a powered hub) of its own: the
    bladeRF 2.0 micro draws up to ~900 mA, and a second device on the same
    port can brown it out when it tunes above ~1 GHz at high gain.
    In a VM (UTM/QEMU) the bladeRF can also drop off the virtual USB bus even
    on its own port: `gsm_scan.py` now stops its `bladeRF-cli` with SIGINT
    (kill -9 only after 8 s) because killing it makes QEMU lose the device.
    If it still happens after a run, disable USB autosuspend in the VM
    (`usbcore.autosuspend=-1`) and check `sudo dmesg` for the `xhci` message.
    Analysis of a drop (2026-10-07, `dmesg` against the `scans` table, VM
    clock vs UTC): every `usb 4-3: reset SuperSpeed USB device` fell on the
    exact second a process opened or closed the bladeRF (end of
    `lte_sib_decoder`, start of `gsm_scan.py`, ...), and after 2-5 such resets
    the device disconnected (`USB disconnect`), the last time right as the
    final LTE step ended. Nothing else on the bus was involved (the u-blox GPS
    is on another controller). The bladeRF is already on `qemu-xhci`
    (`usb3`/`usb4` are PCI 00:05.0; the NEC xHCI, 00:04.0, only has the
    keyboard/mouse/tablet), passed through by UTM's SPICE `usb-redir`, so
    changing the controller will not help; the resets come from usbredir.
    The web app now waits
    `STEP_GAP_S` (2 s) between steps, to cut the open/close churn; whether
    that is enough is still to be confirmed over several Runs.
    Second drop, same day, with the 2 s pause in place: 4 resets, then the
    disconnect at the `gsm_scan.py` -> LTE hand-over (the first drop was at
    the LTE -> `gsm_scan.py` one), and the next LTE step then ran 37 s with
    0 readings. So the pause is not enough: both drops hit a change between
    `gsm_scan.py`'s `bladeRF-cli` and `lte_sib_decoder`.
    Measured with `dmesg -w` timestamps against the web app's step starts
    (4 drops, 2026-10-07): every reset and every disconnect falls on the
    second a new process starts and opens the bladeRF (`sib-scan.sh`,
    `gsm_scan.py`); none happens while a process is running or when it closes
    the device. Roughly 1 open in 4 ends in a disconnect (last one: the first
    open after the replug survived, the second, the 2G step 3 s after the LTE
    step, did not). A Run opens the bladeRF twice with "Also 2G", so fewer
    opens per hour is the way out (one process holding the SDR for several
    steps/Runs), not longer pauses.

58. **`-K` with the bladeRF: known EARFCNs from wide captures.** Decision 49
    left `-K` decoding one carrier at a time (retune, ~3 s search, SIB wait:
    ~73 s for the 22 EARFCNs of the Portugal preset). With the decoder's
    `wide` command, `sib-scan.sh -K` now groups the list with
    `scripts/known_chunks.py` into captures of up to ~23 MHz (same usable
    width as `wide_chunks.py`; groups never straddle 1 GHz because the gain
    differs), captures 2 s per group and decodes every carrier from RAM: the
    Portugal list is 10 captures. EARFCNs the capture could not read (`error`)
    are decoded live; cells left at the MIB or SIB1 get a live decode at the
    end (as in decision 49); a carrier with no cell (`nocell`) is not retried.
    3 groups in a row with no readable carrier exit with code 3 (SDR lost).
    Measured (VM, bladeRF, Portugal preset, 22 EARFCNs): the captures
    themselves take ~38 s for the 10 groups (2.2 s each plus decoding), but
    only 5 of 14 cells came out with all SIBs; the other 9 stopped at the MIB
    or SIB1 (a 2 s capture allows one timing lock, and SIBs with long periods
    do not fit) and went to the live decode at the end, ~6 s each. Two runs:
    1:06 and 1:32, against 1:02-1:17 one carrier at a time, same 15 cells. No
    gain, so it is **off by default**: `-Z` enables it. The live decoder
    already is the fast path; the time is the SI wait, not the retune.

59. **Persistent decoder: the bladeRF is opened once, not once per step.**
    Decision 57's measurements: every USB reset/disconnect of the bladeRF in
    the VM falls on a process opening the board. `sib-scan.sh -P` (the web
    app passes it for the bladeRF) starts `lte_sib_decoder -s -D /tmp/lte_decoder`
    in its own session instead of as a child: commands go in through
    `/tmp/lte_decoder.in`, answers come out of `.out` (FIFOs opened read/write
    by the daemon, so clients come and go), `.pid` and `.cfg` (antennas,
    device, rate) say whether the running one can be reused; with other
    settings it is restarted. Each client sends `hello` first and discards
    everything up to the answer, so lines left by an interrupted client never
    reach the next. The decoder closes the SDR after 10 min without a command
    (`-I`), and `sib-scan.sh` kills it when the SDR stops answering (a replug
    needs a fresh open). While it is open, nothing else can use the board
    (`bladeRF-cli`, the old 2G path): wait 10 min or `kill $(cat /tmp/lte_decoder.pid)`.
    2G goes through it too: `gsm_scan.py --sdr bladerf` detects the daemon
    and uses its new `rec <file> <hz> <s> <gain>` command (captures written
    as int16 I/Q like `bladeRF-cli`, so `gsm_decoder` is unchanged). The
    daemon runs at its fixed 30.72 MSPS (23 MHz usable), so GSM-900 takes 2
    captures and DCS-1800 4 instead of 1 and 2 at 56 MSPS: ~3 s more of
    capture, against ~2 s saved by not opening `bladeRF-cli`, and no extra
    opens. Without a daemon `gsm_scan.py` behaves as before. A Run with
    "Also 2G" now opens the bladeRF once per 10 minutes of activity instead
    of twice per Run. Tested with the `file` RF device and a protocol stub
    only; **not yet with the real bladeRF**: compare the Run time and the
    cells found with decision 53/54's numbers, and count the resets in
    `usb-events.log`.

    First real test (2026-10-07, VM, "Also 2G" + Repeat): **31 runs in 45
    minutes with a single open** (one reset in `usb-events.log`, no
    disconnect), median 69 s LTE (16 cells) + 13 s 2G (30 cells; 8 s before,
    the extra captures at 30.72 MSPS). Then the board stayed on the USB bus
    but stopped answering (libbladeRF "Transfer timed out", "NIOS II ...
    timed out", "Read/Write Error -5", no xhci message): three carriers came
    back `nocell`, the 2G capture failed, exit 3, repeat stopped. Two gaps:
    the daemon's stderr went to its log file, so `sib-scan.sh` never saw the
    USB errors (now `dup2`'d to the FIFO, as the old coprocess had it with
    `2>&1`), and exit 3 ended the repeat for good. Now, after 3 carriers with
    USB errors and no MIB, `sib-scan.sh -P` closes and reopens the SDR (2
    times, those carriers are queued again) and the web app opens it again
    in the next run after 10 s (3 runs in a row with exit 3 before it gives
    up). Whether a reopen revives a board in that state is not known yet.

    2G through the daemon, first 6 runs (2026-10-07): 33-38 cells per run
    (29 before), but only 4, 8, 20, 3, 5, 1 of them with a CGI (SI3), against
    23-24 of 25-30 with `bladeRF-cli`; a strong cell (ARFCN 15, -79 dBm) never
    had one. Run-to-run variation that large points at the samples, not the
    signal: `gsm_decoder` (all cores) was decoding capture i while the daemon
    was still reading the SDR for capture i+1, and a GSM decode needs 1.2 s
    without a gap. `gsm_scan.py` now decodes only after the last capture is
    recorded (costs ~1-2 s). To be checked in the next runs; if the CGI
    ratio stays low, the next suspects are overruns inside the daemon's read
    loop and the plugin's gain/bandwidth defaults against `bladeRF-cli`'s.
    Optional **61.44 MSPS session** (web app: "bladeRF sample rate", passes
    `-r 61.44e6`; `gsm_scan.py` reads the rate from `/tmp/lte_decoder.cfg`):
    2G needs 3 captures (GSM-900 in one, DCS-1800 in two, 46 MHz usable)
    instead of 6. Protocol-tested with the stub only; the LTE side at 61.44
    (32x decimation, 246 MB/s over the virtual USB) is untested.

### Known limitations

- 20 MHz cells on a HackRF are saved as detection-only readings (no SIBs).
- `cell_search` with a HackRF remains unreliable; use `-S`.
- `-p auto` needs LTE cells on the chosen band, and calibration on bands
  above ~1.5 GHz is ambiguous for clocks more than ~25 ppm off.
- The first `sib-scan.sh` run on a new machine, or in a container without the
  `srsran-home` volume (e.g. `run.sh`), first computes srsue's FFTW plans:
  a few seconds on x86, ~20 min on an ARM64 VM. `docker compose run`/`up`
  keep them in the `srsran-home` volume.
- A `sib-scan.sh` call rejected by the cell_search checks is still recorded as
  an empty scan in `readings.sqlite`.
- A reading's band comes from its EARFCN; the band a cell announces in SIB1
  (`freqBandIndicator`) is not checked.
- A sweep sometimes misses a lightly loaded carrier that other sweeps find
  (seen once for a 20 MHz B3 carrier); scanning the band again picks it up.
- The Portugal preset's band list is based on the bands Portuguese operators
  hold; bands without LTE cells just cost a calibration/sweep (~30 s).

## License

AGPL-3.0, see [LICENSE](LICENSE). Based on
[godfuzz3r/lte-sib-parser](https://github.com/godfuzz3r/lte-sib-parser) and
[srsRAN_4G](https://github.com/srsran/srsRAN_4G).
