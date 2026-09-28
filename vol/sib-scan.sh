#!/bin/bash

show_help () {
  echo """usage: sib-scan.sh [OPTION]...
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
          example: -q \"1300 1301 1302 1303\"
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

do_cellsearch=1
do_sweep=0
exclude_mhz=""
retries=""
retry_queue=()
declare -A tries
skip_wide=(--skip-wide)
no_requrse=0

earfcn_need_scan=()
earfcn_scanned=()

while getopts "s:e:b:a:d:g:G:r:p:t:T:hq:K:W:Swx:y:nD:R:L:?" opt; do
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
  esac
done


if [[ $do_sweep -ne 0 && -z $known_list ]]; then
  # hackrf_sweep and the calibration run on a HackRF: with another SDR for srsue,
  # the HackRF's clock correction would be applied to the wrong radio
  if [[ ${device_name,,} != "soapy" || ${device_args,,} != *driver=hackrf* ]]; then
    echo "-S needs a HackRF (-d soapy -a driver=hackrf); use -K with other SDRs"
    exit 1
  fi
fi

# register the scan first, so a scan stopped during calibration or sweep is recorded too
ppm_arg=()
[[ $ppm != "auto" ]] && ppm_arg=(--ppm "$ppm")
scan_id=$(python3 $PY_PATH/readings_db.py -d "$readings_database" new-scan \
            ${band:+--band "$band"} "${ppm_arg[@]}" --args "$*")
echo "scan id: $scan_id"
trap 'python3 $PY_PATH/readings_db.py -d "$readings_database" end-scan "$scan_id"; rm -f $SRSUELOG $SRSUEOUT' EXIT

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
if [[ -n $known_list ]]; then
  earfcn_to_check=($known_list)
  initial_task="check_known"
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

# retry srsue only where PSS/SSS confirmed a cell: -K always, -S when refined
if [[ -z $retries ]]; then
  retries=0
  if [[ -n $known_list || ${#refine[@]} -ne 0 ]]; then
    retries=1
  fi
fi

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
            echo "[srsue] connecting to $earfcn"
            rm -f $SRSUELOG $SRSUEOUT
            dl_freq=$(python3 $PY_PATH/earfcn_to_freq.py $earfcn)
            freq_offset=$(python3 -c "print(round($dl_freq * $ppm * 1e-6))")
            gain=$rx_gain
            if [[ -n $rx_gain_high && $dl_freq -ge 1000000000 ]]; then
              gain=$rx_gain_high
            fi
			      srsue $SRSUECFG --log.filename $SRSUELOG \
                            --expert.lte_sample_rates=true \
                            --rf.device_name "$device_name" \
                            --rf.device_args "$device_args" \
                            --rf.rx_gain "$gain" \
                            "${srate_args[@]}" \
                            --rf.freq_offset "$freq_offset" \
                            --rat.eutra.dl_earfcn "$earfcn" 1>$SRSUEOUT &
            pid=$!  # this instance's srsue (pidof would also match another SDR's)
            # here we need to parse /tmp/ue.log to get SIB's from it
            # next we need to add earfcn's from SIB5 (if found) to earfcn_need_scan, if they already not in earfcn_scanned
            python3 $PY_PATH/parse_save_sib.py -f "$SRSUELOG" -t "$srsue_timeout" -T "$srsue_timeout_add" -e "$earfcn" -d "$database" \
                -R "$readings_database" -I "$scan_id" -L "$location_file" -o "$SRSUEOUT"
            kill -9 $pid 2>/dev/null
			      tail --pid=$pid -f /dev/null 2>/dev/null
            earfcn_scanned+=($earfcn)
            # decoded in this scan? (cells.sqlite would also count earlier scans)
            if python3 $PY_PATH/has_mib.py -R "$readings_database" -I "$scan_id" "$earfcn"; then
                # carrier found: skip the neighbouring raster candidates of the same carrier
                earfcn_scanned+=($((earfcn-2)) $((earfcn-1)) $((earfcn+1)) $((earfcn+2)))
            fi
            # success means SIB1 (the cell identity), not just the MIB
            if ! python3 $PY_PATH/has_mib.py -R "$readings_database" -I "$scan_id" --sib1 "$earfcn" &&
               [[ ${tries[$earfcn]:-0} -lt $retries ]]; then
                tries[$earfcn]=$(( ${tries[$earfcn]:-0} + 1 ))
                echo "no SIB1 on $earfcn: will retry at the end"
                retry_queue+=($earfcn)
            fi

            if [[ -n $known_list && $no_requrse -eq 0 ]]; then
              # -K: EARFCNs advertised in SIB5 are checked (cheap) before srsue
              for e in $(python3 $PY_PATH/get_neigh.py -d "$database" -e "$earfcn" 2>/dev/null); do
                if ! containsElement $e "${earfcn_checked[@]}" && ! containsElement $e "${earfcn_to_check[@]}"; then
                  echo "SIB5 of $earfcn advertises $e: will check it"
                  earfcn_to_check+=($e)
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

        "check_known")
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
            echo "exiting"
            exit 0
            break ;;

        *)
            echo "unknown task" ;;
    esac
done
