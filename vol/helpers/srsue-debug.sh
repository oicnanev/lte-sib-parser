#!/bin/bash
# Debug helper: run srsue for SECS seconds (default 15) on one EARFCN with verbose logs.
# Prints the best PSS peak and any decoded MIB/SIB lines.
# usage: [DEV=bladeRF] [ARGS=...] [SECS=25] srsue-debug.sh earfcn gain [extra srsue args...]
#   default device: HackRF via Soapy (DEV=soapy ARGS=driver=hackrf); a bladeRF
#   needs longer (SECS=25: it takes ~10 s more to start)
e=$1; g=$2; shift 2
rm -f /tmp/ue.log
(sleep $(( ${SECS:-15} + 15 )) | srsue /vol/helpers/ue.conf --log.filename /tmp/ue.log --log.all_level info --expert.lte_sample_rates=true --rf.device_name "${DEV:-soapy}" --rf.device_args "${ARGS-driver=hackrf}" --rf.rx_gain $g --rat.eutra.dl_earfcn $e "$@" > /tmp/ue.out 2>&1 &)
sleep "${SECS:-15}"; pkill -x -INT srsue; sleep 6; pkill -x -9 srsue; sleep 1
echo "== earfcn=$e gain=$g $*"
grep -o "peak_value=[0-9.]*" /tmp/ue.log | cut -d= -f2 | sort -n | tail -1 | sed 's/^/max peak: /'
grep -E "Found|MIB|Content|SIB" /tmp/ue.log | cut -c1-160 | head -5
