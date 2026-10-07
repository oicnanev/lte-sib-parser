#!/bin/bash

show_help () {
  echo """usage: sib-scan.sh [OPTION]...
  -h      show this help message
  -d      device name (UHD,soapy,bladeRF)
  -a      device args (example: "rxant=LNAW")
  -g      rx gain (default: 30)
  -G      rx gain for EARFCNs at 1 GHz and above (default: same as -g)
  -X      decode MIB/SIBs with lte_sib_decoder instead of srsue (default
          with -d bladeRF): the SDR is opened once per scan and each carrier
          takes ~1-3 s instead of ~25 s
  -U      use srsue even with -d bladeRF
  -A      receive antennas for lte_sib_decoder: 1 (RX1, default) or 2 (bladeRF
          RX1 + RX2, combined; helps cells with interference from a nearby
          sector: more complete SIBs, fewer re-acquisitions)
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
          With -d bladeRF (lte_sib_decoder): the band is captured in ~23 MHz
          pieces (2 s each) and every EARFCN of each capture is searched and
          decoded from the same samples; -p auto is measured from the CFO
  -w      with -S: also run srsue on carriers >= 16 MHz wide (20 MHz cells).
          By default they are saved as detection-only readings (PCI and
          bandwidth, no SIBs): a HackRF (20 MSPS) cannot decode them
  -x      with -S: DL frequencies in MHz to skip, e.g. -x "796.0 806.0"
          (bladeRF: EARFCNs within 0.35 MHz of them are not searched)
          (carriers already read in an overlapping band)
  -K      check a list of known EARFCNs for cells with PSS/SSS (HackRF,
          numpy), then run srsue only where there is one. EARFCNs advertised
          in SIB5 by the cells decoded are checked too. With -p auto the
          clock is measured in the same pass. Example: -K "6200 1875 2800"
          With -d bladeRF and lte_sib_decoder there is no pre-check: the
          decoder searches each EARFCN itself (-p auto: measured from CFO)
  -W      with -K: EARFCNs known to be too wide for the SDR (20 MHz cells on
          a HackRF): saved as detection-only readings, no srsue
  -y      srsue retries for EARFCNs where PSS/SSS confirmed a cell but srsue
          did not decode SIB1 (the cell identity); retries run at the end
          (default: 1 with -K, or -S with numpy; 0 otherwise)
  -P      with the decoder: keep lte_sib_decoder (and so the SDR) open after
          this scan, for the next sib-scan.sh / gsm_scan.py. Opening a
          bladeRF is what makes it drop off a VM's USB bus; the decoder
          closes it after 10 min without a command
  -Z      with -K and the decoder: group the known EARFCNs in ~23 MHz captures
          (2 s each, decoded from RAM); cells left at the MIB/SIB1 get a live
          decode at the end. Not faster than the default (README 58)
  -q      use explict list of earfcn's (avoid cell_search)
          example: -q \"1300 1301 1302 1303\"
  -n      no reqursive scan, do no scan cells from sib5
  -t      seconds srsue gets to decode anything (MIB) on an EARFCN
          (-t/-T apply to srsue only; lte_sib_decoder has its own limits)
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

  Usage examples
  1
  Scan full band, parse SIBs from cells, also parse SIB's from cells that
  are found on SIB5, do search and parse neighboors from SIB5 reqursively:
  ./sib-scan.sh -b 3

  2
  Do the same, but not for full band:
  ./sib-scan.sh -b 3 -s 1300 -e 1400
  ./sib-scan.sh -s 1300 -e 1400       # will automatically determine band
  ./sib-scan.sh -s 1300               # will automatically determine band
                                      # and end-earfcn
  ./sib-scan.sh -e 1400               # will automatically determine band
                                      # and start-earfcn

  3
  Scan explict list of earfcn's, also reqursively scan neighboors from SIB5:
  ./sib-scan.sh -q \"1300 1301 1302 1303\"

  4
  Add device name and device args:
  ./sib-scan.sh -d soapy -a "rxant=LNAW" -b 3

  5
  Use another sqlite path:
  ./sib-scan.sh -b 3 -D /tmp/myoutput.sqlite
  """
}

containsElement () {
  local e match="$1"
  shift
  for e; do [[ "$e" == "$match" ]] && return 0; done
  return 1
}

# lte_sib_decoder runs as a coprocess for the whole scan: opening a bladeRF 2.0
# takes ~7 s, decoding one carrier ~1-3 s. It is closed while something else
# needs the SDR (the PSS/SSS checks with bladeRF-cli) and reopened afterwards.
DEC_PID=""
sdr_fail=0
sdr_failed=()
sdr_reopen=0
DEC_PREFIX=/tmp/lte_decoder
DEC_ATTACHED=0

# -P: the decoder is a daemon (own session) that outlives this script; commands
# and answers go through two FIFOs, which gsm_scan.py uses too
dec_attach () {
  [[ $DEC_ATTACHED -eq 1 ]] && return 0
  local cfg="$nof_rx|$device_name|$device_args|${dec_srate[*]}"
  local pid=""
  [[ -r $DEC_PREFIX.pid ]] && pid=$(<$DEC_PREFIX.pid)
  if [[ -n $pid ]] && kill -0 $pid 2>/dev/null && [[ $(cat $DEC_PREFIX.cfg 2>/dev/null) != "$cfg" ]]; then
    echo "[decoder] settings changed: restarting the persistent decoder"
    dec_kill
    pid=""
  fi
  if [[ -z $pid ]] || ! kill -0 $pid 2>/dev/null; then
    rm -f $DEC_PREFIX.in $DEC_PREFIX.out $DEC_PREFIX.pid
    mkfifo $DEC_PREFIX.in $DEC_PREFIX.out
    echo "$cfg" > $DEC_PREFIX.cfg
    setsid lte_sib_decoder -s -D $DEC_PREFIX -A "$nof_rx" -d "$device_name" -a "$device_args" "${dec_srate[@]}" \
        >>$DEC_PREFIX.log 2>&1 </dev/null &
    pid=$!
    echo $pid > $DEC_PREFIX.pid
    echo "[decoder] started the persistent decoder (pid $pid)"
  fi
  # read/write opens never block, even if the daemon is not there yet
  exec {DEC_R}<>$DEC_PREFIX.out {DEC_W}<>$DEC_PREFIX.in
  DEC=($DEC_R $DEC_W)
  echo hello >&$DEC_W
  local line slow=0
  while [[ $slow -lt 30 ]]; do
    if read -r -t 3 -u $DEC_R line; then
      echo "$line"
      if [[ $line == hello ]]; then
        DEC_ATTACHED=1
        return 0
      fi
    else
      kill -0 $pid 2>/dev/null || break
      slow=$((slow+1))
    fi
  done
  echo "the persistent decoder did not answer"
  dec_kill
  return 1
}
dec_detach () {
  [[ $DEC_ATTACHED -eq 0 ]] && return
  eval "exec ${DEC[0]}<&- ${DEC[1]}>&-" 2>/dev/null
  DEC_ATTACHED=0
}
# close the SDR for good (it vanished, or another tool needs it)
dec_kill () {
  local pid=""
  [[ -r $DEC_PREFIX.pid ]] && pid=$(<$DEC_PREFIX.pid)
  if [[ -n $pid ]] && kill -0 $pid 2>/dev/null; then
    [[ $DEC_ATTACHED -eq 1 ]] && echo quit >&"${DEC[1]}" 2>/dev/null
    for _ in $(seq 20); do kill -0 $pid 2>/dev/null || break; sleep 0.5; done
    kill -INT $pid 2>/dev/null
    for _ in $(seq 10); do kill -0 $pid 2>/dev/null || break; sleep 0.5; done
    kill -9 $pid 2>/dev/null
  fi
  dec_detach
  rm -f $DEC_PREFIX.in $DEC_PREFIX.out $DEC_PREFIX.pid $DEC_PREFIX.cfg
}
# stop using the SDR: with -P only let go of it unless it is lost
dec_abort () {
  if [[ $keep_decoder -eq 1 ]]; then dec_kill; else dec_stop; fi
}
dec_start () {
  [[ $keep_decoder -eq 1 ]] && { dec_attach; return; }
  [[ -n $DEC_PID ]] && kill -0 $DEC_PID 2>/dev/null && return 0
  coproc DEC { exec lte_sib_decoder -s -A "$nof_rx" -d "$device_name" -a "$device_args" "${dec_srate[@]}" 2>&1; }
  DEC_PID=$DEC_PID
  local line
  while read -r -t 60 -u "${DEC[0]}" line; do
    echo "$line"
    [[ $line == ready* ]] && return 0
    [[ $line == error* ]] && break
  done
  echo "lte_sib_decoder did not start"
  dec_stop
  return 1
}
dec_stop () {
  [[ $keep_decoder -eq 1 ]] && { dec_detach; return; }
  [[ -z $DEC_PID ]] && return
  local pid=$DEC_PID
  eval "exec ${DEC[1]}>&-" 2>/dev/null  # EOF on its stdin: it closes the SDR and exits
  for _ in $(seq 10); do kill -0 $pid 2>/dev/null || break; sleep 1; done
  kill -INT $pid 2>/dev/null
  for _ in $(seq 10); do kill -0 $pid 2>/dev/null || break; sleep 1; done
  kill -9 $pid 2>/dev/null
  DEC_PID=""
}

# a carrier the decoder finished: save its log, note its CFO for -p auto
dec_save () {
  local e=$1 log=$2
  local f
  f=$(python3 $PY_PATH/earfcn_to_freq.py $e)
  local cfo
  cfo=$(sed -n 's/.*Found Cell:.*CFO=\([-0-9.]*\) KHz.*/\1/p' "$log" | tail -1)
  if [[ -n ${ppm_from_cfo:-} && -n $cfo ]]; then
    cfo_ppm+=($(python3 -c "print(round($cfo * 1e9 / $f, 2))"))
  fi
  # the log is complete: parse_save_sib stops at its "[decoder] done" line
  python3 $PY_PATH/parse_save_sib.py -f "$log" -t 30 -T 30 -e "$e" -d "$database" \
      -R "$readings_database" -I "$scan_id" -L "$location_file" -o "$log" --detection decoder
}

# stop srsue. In a VM, kill -9 while srsue streams from a USB SDR made the
# hypervisor's USB passthrough drop the device (QEMU on macOS: "disconnected
# (fatal IO error)"), so there srsue gets SIGINT and closes the SDR itself
# (~5 s: it forces its exit after 5 s); natively kill -9 is instant and safe
stop_srsue () {
  local pid=$1
  if grep -qiE "qemu|parallels|vmware|innotek" /sys/class/dmi/id/sys_vendor 2>/dev/null; then
    kill -INT $pid 2>/dev/null
    for _ in $(seq 15); do kill -0 $pid 2>/dev/null || return; sleep 1; done
  fi
  kill -9 $pid 2>/dev/null
}

PY_PATH=/vol/scripts/
SRSUECFG=/vol/helpers/ue.conf
# per-instance files: several sib-scan.sh can run at once, one per SDR
SRSUELOG=/tmp/ue.$$.log
SRSUEOUT=/tmp/ue.$$.out

srsue_timeout=30
srsue_timeout_add=30
database=/vol/output/cells.sqlite
readings_database=/vol/output/readings.sqlite
location_file=/tmp/lte_location.json

device_args=""
device_name=""
rx_gain="30"
rx_gain_high=""
known_list=""
wide_list=""
srate_args=()
ppm="0"
use_decoder=""
nof_rx=1

do_cellsearch=1
do_sweep=0
exclude_mhz=""
retries=""
retry_queue=()
declare -A tries
skip_wide=(--skip-wide)
no_requrse=0
wide_known=0
keep_decoder=0

earfcn_need_scan=()
earfcn_scanned=()

while getopts "s:e:b:a:d:g:G:r:p:t:T:hq:K:W:Swx:y:nD:R:L:XUZPA:?" opt; do
  case "$opt" in
    h|\?)
      show_help
      exit 0
      ;;
    d)  device_name=$OPTARG
      ;;
    a)  device_args=$OPTARG
      ;;
    g)  rx_gain=$OPTARG
      ;;
    G)  rx_gain_high=$OPTARG
      ;;
    K)  known_list=$OPTARG
      ;;
    W)  wide_list=$OPTARG
      ;;
    r)  srate_args=(--rf.srate "$OPTARG")
      ;;
    p)  ppm=$OPTARG
      ;;
    b)  band=$OPTARG
      ;;
    s)  start_earfcn=$OPTARG
      ;;
    e)  end_earfcn=$OPTARG
      ;;
    q)  earfcn_need_scan=($OPTARG)
      ;;
    S)  do_sweep=1
      ;;
    x)  exclude_mhz=$OPTARG
      ;;
    w)  skip_wide=()
      ;;
    y)  retries=$OPTARG
      ;;
    Z)  wide_known=1
        ;;
    P)  keep_decoder=1
        ;;
    n)  no_requrse=1
      ;;
    t)  srsue_timeout=$OPTARG
      ;;
    T)  srsue_timeout_add=$OPTARG
      ;;
    D)  database=$OPTARG
      ;;
    R)  readings_database=$OPTARG
      ;;
    L)  location_file=$OPTARG
      ;;
    X)  use_decoder=1
      ;;
    U)  use_decoder=0
      ;;
    A)  nof_rx=$OPTARG
      ;;
  esac
done


if [[ -z $use_decoder ]]; then
  use_decoder=0
  [[ ${device_name,,} == "bladerf" ]] && use_decoder=1
fi
if [[ $use_decoder -eq 1 ]] && ! command -v lte_sib_decoder >/dev/null; then
  echo "lte_sib_decoder not installed (rebuild the image): using srsue"
  use_decoder=0
fi
# scripts updated by git pull, image not rebuilt: the old decoder rejects the new
# options and every cell silently fell back to srsue (a 1:30 run took 10 min)
if [[ $use_decoder -eq 1 ]] && ! lte_sib_decoder -h 2>&1 | grep -q -- "^ *-A "; then
  echo "WARNING: lte_sib_decoder in the image is older than the scripts (no -A):"
  echo "WARNING: rebuild it (docker compose build) and restart the web app; using srsue"
  use_decoder=0
fi
if [[ $keep_decoder -eq 1 ]]; then
  if [[ $use_decoder -ne 1 ]]; then
    keep_decoder=0
  elif ! lte_sib_decoder -h 2>&1 | grep -q -- "^ *-D "; then
    echo "WARNING: lte_sib_decoder in the image has no daemon mode (-D): rebuild the image; not keeping it open"
    keep_decoder=0
  fi
fi
# bladeRF + decoder: the decoder's own cell search replaces the PSS/SSS
# pre-check of -K (bladeRF-cli captures, ~75 s for the Portugal list, which
# was most of the scan); its clock (~1 ppm) needs no correction to find cells,
# and -p auto is measured from the CFO of the cells decoded
direct_known=0
if [[ -n $known_list && $use_decoder -eq 1 && ${device_name,,} == "bladerf" ]]; then
  direct_known=1
fi
cfo_ppm=()
# bladeRF + decoder with -S: no hackrf_sweep; the band is captured in ~23 MHz
# pieces and lte_sib_decoder looks for cells on every EARFCN of each capture
wide_scan=0
if [[ $do_sweep -ne 0 && -z $known_list && $use_decoder -eq 1 && ${device_name,,} == "bladerf" ]]; then
  wide_scan=1
fi

if [[ $do_sweep -ne 0 && -z $known_list && $wide_scan -eq 0 ]]; then
  # hackrf_sweep and the calibration run on a HackRF: with another SDR for srsue,
  # the HackRF's clock correction would be applied to the wrong radio
  if [[ ${device_name,,} != "soapy" || ${device_args,,} != *driver=hackrf* ]]; then
    echo "-S needs a HackRF (-d soapy -a driver=hackrf) or a bladeRF (-d bladeRF); use -K with other SDRs"
    exit 1
  fi
fi

# register the scan first, so a scan stopped during calibration or sweep is recorded too
ppm_arg=()
[[ $ppm != "auto" ]] && ppm_arg=(--ppm "$ppm")
scan_id=$(python3 $PY_PATH/readings_db.py -d "$readings_database" new-scan \
            ${band:+--band "$band"} "${ppm_arg[@]}" --args "$*")
echo "scan id: $scan_id"
trap 'dec_stop; python3 $PY_PATH/readings_db.py -d "$readings_database" end-scan "$scan_id"; rm -f $SRSUELOG $SRSUEOUT' EXIT

if [[ $ppm == "auto" && $wide_scan -eq 1 ]]; then
  ppm=0  # measured from the decoded cells' CFO instead (bladeRF: ~1 ppm)
  ppm_from_cfo=1
fi
if [[ $ppm == "auto" && -z $known_list ]]; then
  if [[ -z $band ]]; then
    echo "-p auto needs band (-b)"
    exit 1
  fi
  echo "calibrating SDR clock error on band $band..."
  ppm=$(python3 $PY_PATH/calibrate_ppm.py -b "$band")
  if [[ $? -ne 0 || -z $ppm ]]; then
    echo "calibration failed, pass -p <ppm> by hand"
    exit 1
  fi
  echo "frequency correction: $ppm ppm"
  python3 $PY_PATH/readings_db.py -d "$readings_database" set-ppm "$scan_id" "$ppm"
fi

earfcn_to_check=()
earfcn_checked=()
if [[ -n $known_list && $direct_known -eq 1 ]]; then
  earfcn_need_scan=($known_list)
  earfcn_checked=($known_list)
  initial_task="choose_earfcn_for_srsue"
  if [[ $wide_known -eq 1 ]]; then
    earfcn_need_scan=()
    initial_task="wide_known"
  fi
  do_cellsearch=0
  if [[ $ppm == "auto" ]]; then
    ppm=0
    ppm_from_cfo=1
  fi
elif [[ -n $known_list ]]; then
  earfcn_to_check=($known_list)
  initial_task="check_known"
  do_cellsearch=0
elif [[ $wide_scan -eq 1 ]]; then
  if [[ -z $band ]]; then
    echo "-S needs band (-b)"
    exit 1
  fi
  initial_task="wide_scan"
  do_cellsearch=0
elif [[ $do_sweep -ne 0 ]]; then
  if [[ -z $band ]]; then
    echo "-S needs band (-b)"
    exit 1
  fi
  echo "sweeping band $band with hackrf_sweep..."
  refine=()
  if python3 -c "import numpy" 2>/dev/null; then
    refine=(--refine)
  fi
  earfcn_need_scan=( $(python3 $PY_PATH/sweep_candidates.py -b "$band" -p "$ppm" -v "${refine[@]}" -x "$exclude_mhz" \
                         "${skip_wide[@]}" --readings-db "$readings_database" --scan-id "$scan_id" \
                         --location-file "$location_file") )
  initial_task="choose_earfcn_for_srsue"
  do_cellsearch=0
elif [[ ${#earfcn_need_scan[@]} -eq 0 ]]; then
  initial_task="cell_search"
  do_cellsearch=1
else
  initial_task="choose_earfcn_for_srsue"
  do_cellsearch=0
fi

# check for required options
if  [[ $do_cellsearch -ne 0 ]] &&
    [[ -z $band ]] &&
    [[ -z $start_earfcn ]] &&
    [[ -z $end_earfcn ]]; then
    echo "Need at least band (-b), start earfcn (-s) or end earfcn (-e) parameter to start"
    exit 1
fi
# check for empty band
if  [[ $do_cellsearch -ne 0 ]] &&
    [[ -z $band ]] &&
    [[ ! -z $start_earfcn ]]; then
    band=$(python3 $PY_PATH/earfcn_to_band.py $start_earfcn)
fi
if  [[ $do_cellsearch -ne 0 ]] &&
    [[ -z $band ]] &&
    [[ ! -z $end_earfcn ]]; then
    band=$(python3 $PY_PATH/earfcn_to_band.py $end_earfcn)
fi
# check for empty start_earfcn
if  [[ $do_cellsearch -ne 0 ]] &&
    [[ -z $start_earfcn ]]; then
    start_earfcn=$(python3 $PY_PATH/band_to_earfcn.py $band | awk '{ print $1 }')
fi
# check for empty end_earfcn
if  [[ $do_cellsearch -ne 0 ]] &&
    [[ -z $end_earfcn ]]; then
    end_earfcn=$(python3 $PY_PATH/band_to_earfcn.py $band | awk '{ print $2 }')
fi
# check if start_earfcn and end_earfcn are from the same band
_start=$(python3 $PY_PATH/earfcn_to_band.py $start_earfcn)
_end=$(python3 $PY_PATH/earfcn_to_band.py $end_earfcn)
if  [[ $do_cellsearch -ne 0 ]] &&
    [[ $_start -ne $band || $_end -ne $band ]]; then
    echo "-s and -e must be from same band"
    exit 1
fi


# bladeRF 2.0: changing the AD9361 sample rate takes seconds (1.92 MSPS alone
# ~1.6 s), and srsue changes it twice per cell; keep the hardware at 30.72 MSPS
# and let srsue decimate in software (1.92/7.68/15.36/30.72 are integer ratios)
if [[ ${#srate_args[@]} -eq 0 && ${device_name,,} == "bladerf" ]]; then
  srate_args=(--rf.srate 30.72e6)
fi
# the decoder decimates in software like srsue: same fixed rate
dec_srate=()
[[ ${#srate_args[@]} -ne 0 ]] && dec_srate=(-r "${srate_args[1]}")

# retry srsue only where PSS/SSS confirmed a cell: -K always, -S when refined
if [[ -z $retries ]]; then
  retries=0
  if [[ -n $known_list || ${#refine[@]} -ne 0 || $wide_scan -eq 1 ]]; then
    retries=1
  fi
fi

# srsue's FFTW plans: computed once per machine and saved (see fftw-warmup.sh);
# without them srsue can't start within the per-cell timeout on a new machine
[[ $use_decoder -eq 0 ]] && /vol/helpers/fftw-warmup.sh "$SRSUECFG"

task=$initial_task
while true; do
    echo
    echo "task: "$task
    echo "earfcn (for srsue task): "$earfcn
    echo "start_earfcn (for cell_search task): "$start_earfcn
    echo "scanned earfcns: ${earfcn_scanned[@]}"
    echo "queue to scan earfcn: ${earfcn_need_scan[@]}"
    case "$task" in
        "cell_search")
            task="exit"
            while read line; do
                echo $line
                if grep -q "Found CELL ID" <<< "$line"; then
                    pid=$(pidof cell_search)
                    tail --pid=$pid -f /dev/null 2>/dev/null
                    earfcn=$(echo $line | awk '{ print $10 }')
                    start_earfcn=$(($earfcn + 1))

                    containsElement $earfcn "${earfcn_scanned[@]}"
                    if [[ $? -ne 0 ]]; then
                        earfcn_need_scan+=($earfcn)
                    fi
                    task="choose_earfcn_for_srsue"
                fi
                if grep -q "Bye" <<< $line; then
                  pid=$(pidof cell_search)
                  kill -9 $pid 2>/dev/null
                  tail --pid=$pid -f /dev/null 2>/dev/null
                  task="exit"
                fi
            done < <(cell_search -b "$band" -s "$start_earfcn" -e "$end_earfcn" -a "$device_args" -d "$device_name" -g "$rx_gain" -p "$ppm")
            continue ;;

        "choose_earfcn_for_srsue")
            # check if queue of earfcn's empty
            if [[ ${#earfcn_need_scan[@]} -eq 0 ]]; then
                if [[ ${#earfcn_to_check[@]} -ne 0 ]]; then
                  task="check_known"
                elif [[ ${#retry_queue[@]} -ne 0 ]]; then
                  # confirmed cells srsue missed: try again, at the end of the scan
                  earfcn=${retry_queue[0]}
                  retry_queue=("${retry_queue[@]:1}")
                  echo "retrying $earfcn (attempt $(( ${tries[$earfcn]} + 1 )))"
                  task="srsue"
                elif [[ $do_cellsearch -eq 0 ]]; then
                  task="exit"
                else
                  task="cell_search"
                fi
                continue
            fi

            # get first earfcn from list
            earfcn=$earfcn_need_scan
            earfcn_need_scan=("${earfcn_need_scan[@]:1}")

            # if already scanned, check next
            containsElement $earfcn "${earfcn_scanned[@]}"
            if [[ $? -eq 0 ]]; then
                task="choose_earfcn_for_srsue"
                continue
            fi

            # finally found earfcn from earfcn_need_scan which we need to parse SIB's from
            task="srsue"
            continue ;;

        "srsue")
            rm -f $SRSUELOG $SRSUEOUT
            dl_freq=$(python3 $PY_PATH/earfcn_to_freq.py $earfcn)
            freq_offset=$(python3 -c "print(round($dl_freq * $ppm * 1e-6))")
            gain=$rx_gain
            if [[ -n $rx_gain_high && $dl_freq -ge 1000000000 ]]; then
              gain=$rx_gain_high
            fi
            if [[ $use_decoder -eq 1 ]] && dec_start; then
              echo "[decoder] connecting to $earfcn"
              echo "$earfcn $gain $freq_offset $SRSUELOG" >&"${DEC[1]}"
              sdr_err=0
              while read -r -t 120 -u "${DEC[0]}" line; do
                echo "$line"
                [[ $line =~ RX\ failed|NIOS\ II|fatal\ IO|transfer\ error|Transfer\ timed ]] && sdr_err=1
                [[ $line == "done $earfcn "* ]] && break
              done
              dec_save "$earfcn" "$SRSUELOG"
              # the SDR vanished from the USB bus (e.g. a bus-power drop in a VM): every
              # carrier then fails after minutes of timeouts, so stop after 3 in a row
              if [[ $sdr_err -eq 1 ]] && ! python3 $PY_PATH/has_mib.py -R "$readings_database" -I "$scan_id" "$earfcn"; then
                sdr_fail=$((sdr_fail+1))
                sdr_failed+=($earfcn)
              else
                sdr_fail=0
                sdr_failed=()
                [[ $sdr_err -eq 0 ]] && sdr_reopen=0
              fi
              if [[ $sdr_fail -ge 3 ]]; then
                if [[ $keep_decoder -eq 1 && $sdr_reopen -lt 2 ]]; then
                  # still on the USB bus but not answering (transfer / NIOS II timeouts):
                  # closing and opening it again often brings it back; no replug needed
                  sdr_reopen=$((sdr_reopen+1))
                  echo "[sdr] USB errors on $sdr_fail carriers in a row: reopening the SDR ($sdr_reopen/2)"
                  dec_kill
                  sleep 3
                  if dec_attach; then
                    for e in "${sdr_failed[@]}"; do
                      keep=()
                      for x in "${earfcn_scanned[@]}"; do [[ $x != "$e" ]] && keep+=($x); done
                      earfcn_scanned=("${keep[@]}")
                      earfcn_need_scan+=($e)
                    done
                    sdr_fail=0
                    sdr_failed=()
                    task="choose_earfcn_for_srsue"
                    continue
                  fi
                fi
                echo "ERROR: the SDR stopped answering (USB errors on $sdr_fail carriers in a row)."
                echo "ERROR: unplug and replug it (in a VM also re-attach it), then run again."
                dec_abort
                exit 3
              fi
            else
              echo "[srsue] connecting to $earfcn"
              srsue $SRSUECFG --log.filename $SRSUELOG \
                              --expert.lte_sample_rates=true \
                              --rf.device_name "$device_name" \
                              --rf.device_args "$device_args" \
                              --rf.rx_gain "$gain" \
                              "${srate_args[@]}" \
                              --rf.freq_offset "$freq_offset" \
                              --rat.eutra.dl_earfcn "$earfcn" 1>$SRSUEOUT &
              pid=$!  # this instance's srsue (pidof would also match another SDR's)
              # parse the srsue log for MIB/SIBs; SIB5 neighbours are queued below
              python3 $PY_PATH/parse_save_sib.py -f "$SRSUELOG" -t "$srsue_timeout" -T "$srsue_timeout_add" -e "$earfcn" -d "$database" \
                  -R "$readings_database" -I "$scan_id" -L "$location_file" -o "$SRSUEOUT"
              stop_srsue $pid
              tail --pid=$pid -f /dev/null 2>/dev/null
            fi
            earfcn_scanned+=($earfcn)
            # decoded in this scan? (cells.sqlite would also count earlier scans)
            if python3 $PY_PATH/has_mib.py -R "$readings_database" -I "$scan_id" "$earfcn"; then
                # carrier found: skip the neighbouring raster candidates of the same carrier
                earfcn_scanned+=($((earfcn-2)) $((earfcn-1)) $((earfcn+1)) $((earfcn+2)))
            fi
            # success means SIB1 (the cell identity), not just the MIB
            # (srsue only: lte_sib_decoder already acquires a cell up to 4 times;
            # cells from a wide capture are queued for a live decode in wide_scan)
            if [[ $use_decoder -eq 0 ]] &&
               ! python3 $PY_PATH/has_mib.py -R "$readings_database" -I "$scan_id" --sib1 "$earfcn" &&
               [[ ${tries[$earfcn]:-0} -lt $retries ]]; then
                tries[$earfcn]=$(( ${tries[$earfcn]:-0} + 1 ))
                echo "no SIB1 on $earfcn: will retry at the end"
                retry_queue+=($earfcn)
            fi

            if [[ -n $known_list && $no_requrse -eq 0 ]]; then
              # -K: EARFCNs advertised in SIB5 are checked (cheap) before srsue
              for e in $(python3 $PY_PATH/get_neigh.py -d "$database" -e "$earfcn" 2>/dev/null); do
                if ! containsElement $e "${earfcn_checked[@]}" && ! containsElement $e "${earfcn_to_check[@]}"; then
                  if [[ $direct_known -eq 1 ]]; then
                    echo "SIB5 of $earfcn advertises $e: will decode it"
                    earfcn_checked+=($e)
                    containsElement $e "${earfcn_scanned[@]}" || earfcn_need_scan+=($e)
                  else
                    echo "SIB5 of $earfcn advertises $e: will check it"
                    earfcn_to_check+=($e)
                  fi
                fi
              done
              task="choose_earfcn_for_srsue"
              continue
            fi
            if [[ $no_requrse -ne 0 ]]; then
              task="choose_earfcn_for_srsue"
              continue
            fi
			      sib5_earfcns=( $(python3 $PY_PATH/get_neigh.py -d "$database" -e "$earfcn") )
            for e in ${sib5_earfcns[@]}; do
                    containsElement $e "${earfcn_scanned[@]}"
                    if [[ $? -ne 0 ]]; then
                        earfcn_need_scan+=($e)
                    fi
            done
            # if there is new earfcn's in earfcn_need_scan, "choose_earfcn_for_srsue" will find it
            task="choose_earfcn_for_srsue"
            continue ;;

        "wide_known")
            dec_start || exit 1
            found=()
            while read -r centre group; do
              gain=$rx_gain
              if [[ -n $rx_gain_high && $centre -ge 1000000000 ]]; then
                gain=$rx_gain_high
              fi
              cmd="wide $centre 2 $gain"
              for e in $group; do
                f=$(python3 $PY_PATH/earfcn_to_freq.py $e)
                cmd+=" $e $(python3 -c "print(round($f * $ppm * 1e-6))") /tmp/wide.$$.$e.log"
              done
              echo "[decoder] capture at $(( centre / 1000 )) kHz for EARFCN $group"
              echo "$cmd" >&"${DEC[1]}"
              n_err=0
              n_done=0
              while read -r -t 300 -u "${DEC[0]}" line; do
                echo "$line"
                if [[ $line =~ ^done\ ([0-9]+)\ ([a-z0-9]+) ]]; then
                  e=${BASH_REMATCH[1]}
                  st=${BASH_REMATCH[2]}
                  n_done=$((n_done+1))
                  if [[ $st == error ]]; then
                    n_err=$((n_err+1))
                    # not read from the capture: decode it live
                    earfcn_need_scan+=($e)
                  else
                    dec_save "$e" "/tmp/wide.$$.$e.log"
                    earfcn_scanned+=($e)
                    found+=($e)
                    if [[ $st == mib || $st == sib1 ]] && [[ $retries -gt 0 ]]; then
                      echo "$e: $st only from the capture, will decode it live at the end"
                      tries[$e]=1
                      retry_queue+=($e)
                    fi
                  fi
                  rm -f "/tmp/wide.$$.$e.log"
                fi
                [[ $line == wide:* ]] && break
              done
              # captures failing in a row: the SDR left the USB bus (see "srsue" task)
              if [[ $n_done -eq 0 || $n_err -eq $n_done ]]; then
                sdr_fail=$((sdr_fail+1))
              else
                sdr_fail=0
              fi
              if [[ $sdr_fail -ge 3 ]]; then
                echo "ERROR: the SDR stopped answering (3 captures in a row failed)."
                echo "ERROR: unplug and replug it (in a VM also re-attach it), then run again."
                dec_abort
                exit 3
              fi
            done < <(python3 $PY_PATH/known_chunks.py -r "${dec_srate[1]:-30.72e6}" $known_list)
            # SIB5 neighbours of the cells found: decoded one by one
            if [[ $no_requrse -eq 0 ]]; then
              for f in "${found[@]}"; do
                for e in $(python3 $PY_PATH/get_neigh.py -d "$database" -e "$f" 2>/dev/null); do
                  if ! containsElement $e "${earfcn_checked[@]}"; then
                    echo "SIB5 of $f advertises $e: will decode it"
                    earfcn_checked+=($e)
                    containsElement $e "${earfcn_scanned[@]}" || earfcn_need_scan+=($e)
                  fi
                done
              done
            fi
            task="choose_earfcn_for_srsue"
            continue ;;

        "wide_scan")
            dec_start || exit 1
            found=()
            while read -r centre lo hi; do
              scan_gain=$rx_gain
              if [[ -n $rx_gain_high && $centre -ge 1000000000 ]]; then
                scan_gain=$rx_gain_high
              fi
              echo "[decoder] scanning EARFCN $lo-$hi (capture at $(( centre / 100000 ))00 kHz)"
              echo "scan $centre 2 $scan_gain $lo $hi $ppm /tmp/wide.$$ $exclude_mhz" >&"${DEC[1]}"
              while read -r -t 300 -u "${DEC[0]}" line; do
                echo "$line"
                if [[ $line =~ ^done\ ([0-9]+)\ ([a-z0-9]+) ]]; then
                  e=${BASH_REMATCH[1]}
                  dec_save "$e" "/tmp/wide.$$.$e.log"
                  rm -f "/tmp/wide.$$.$e.log"
                  earfcn_scanned+=($e)
                  found+=($e)
                  # a 2 s capture allows one timing lock only: cells left at the MIB
                  # or SIB1 get a live decode (up to 4 acquisitions) at the end
                  if [[ ${BASH_REMATCH[2]} == mib || ${BASH_REMATCH[2]} == sib1 ]] && [[ $retries -gt 0 ]]; then
                    echo "$e: ${BASH_REMATCH[2]} only from the capture, will decode it live at the end"
                    tries[$e]=1
                    retry_queue+=($e)
                  fi
                fi
                [[ $line == "scan done"* ]] && break
              done
            done < <(python3 $PY_PATH/wide_chunks.py -b "$band")
            # the whole band was looked at: SIB5 neighbours outside it are decoded one by one
            if [[ $no_requrse -eq 0 ]]; then
              read -r band_lo band_hi < <(python3 $PY_PATH/band_to_earfcn.py "$band")
              for f in "${found[@]}"; do
                for e in $(python3 $PY_PATH/get_neigh.py -d "$database" -e "$f" 2>/dev/null); do
                  if (( e < band_lo || e > band_hi )) && ! containsElement $e "${earfcn_scanned[@]}" &&
                     ! containsElement $e "${earfcn_need_scan[@]}"; then
                    echo "SIB5 of $f advertises $e: will decode it"
                    earfcn_need_scan+=($e)
                  fi
                done
              done
            fi
            task="choose_earfcn_for_srsue"
            continue ;;

        "check_known")
            dec_abort  # the checks capture with bladeRF-cli, which needs the SDR
            echo "checking EARFCNs for cells: ${earfcn_to_check[*]}"
            check_sdr=(--sdr hackrf)
            if [[ ${device_name,,} == "bladerf" || ${device_args,,} == *driver=bladerf* ]]; then
              check_sdr=(--sdr bladerf --gain "$rx_gain")
            fi
            check_out=$(python3 $PY_PATH/check_earfcns.py -v -e "${earfcn_to_check[*]}" -p "$ppm" "${check_sdr[@]}" \
                          --wide "$wide_list" --readings-db "$readings_database" --scan-id "$scan_id" \
                          --location-file "$location_file")
            earfcn_checked+=("${earfcn_to_check[@]}")
            earfcn_to_check=()
            measured_ppm=$(sed -n 's/^ppm //p' <<< "$check_out")
            if [[ $ppm == "auto" && -n $measured_ppm ]]; then
              ppm=$measured_ppm
              echo "frequency correction: $ppm ppm"
              python3 $PY_PATH/readings_db.py -d "$readings_database" set-ppm "$scan_id" "$ppm"
            fi
            for e in $(grep -v '^ppm ' <<< "$check_out"); do
              containsElement $e "${earfcn_scanned[@]}" || earfcn_need_scan+=($e)
            done
            task="choose_earfcn_for_srsue"
            continue ;;

        "exit")
            if [[ ${#cfo_ppm[@]} -ne 0 ]]; then
              ppm=$(python3 -c "import statistics,sys; print(round(statistics.median(map(float, sys.argv[1:])), 2))" "${cfo_ppm[@]}")
              echo "frequency correction: $ppm ppm (measured from the cells' CFO, not applied)"
              python3 $PY_PATH/readings_db.py -d "$readings_database" set-ppm "$scan_id" "$ppm"
            fi
            echo "exiting"
            exit 0
            break ;;

        *)
            echo "unknown task" ;;
    esac
done
