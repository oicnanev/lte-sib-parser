#!/bin/bash
# Debug helper: run srsue for 15 s on one EARFCN with verbose logs (HackRF via Soapy).
# Prints the best PSS peak and any decoded MIB/SIB lines.
# usage: srsue-debug.sh earfcn gain [extra srsue args...]
e=$1; g=$2; shift 2
rm -f /tmp/ue.log
(sleep 30 | srsue /vol/helpers/ue.conf --log.filename /tmp/ue.log --log.all_level info --expert.lte_sample_rates=true --rf.device_name soapy --rf.device_args "driver=hackrf" --rf.rx_gain $g --rat.eutra.dl_earfcn $e "$@" > /tmp/ue.out 2>&1 &)
sleep 15; pkill -x -INT srsue; sleep 6; pkill -x -9 srsue; sleep 1
echo "== earfcn=$e gain=$g $*"
grep -o "peak_value=[0-9.]*" /tmp/ue.log | cut -d= -f2 | sort -n | tail -1 | sed 's/^/max peak: /'
grep -E "Found|MIB|Content|SIB" /tmp/ue.log | cut -c1-160 | head -5
