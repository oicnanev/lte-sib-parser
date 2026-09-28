#!/bin/bash
# Compute and save srsue's FFTW plans ($HOME/.srsran_fftwisdom) without an SDR.
#
# srsue plans its FFTs with FFTW_MEASURE at start-up and saves them only when it
# exits cleanly; sib-scan kills srsue with -9, so a machine without the wisdom
# file recomputed them for every cell and srsue never got past "Waiting PHY to
# initialize" (~20 min on an ARM64 VM, >15 s on x86). This runs srsue once with
# the "file" RF device reading /dev/zero until the PHY is up, then stops it with
# SIGINT so it writes the wisdom. With the wisdom in place it takes ~1 s.
# usage: fftw-warmup.sh [ue.conf]
cfg=${1:-/vol/helpers/ue.conf}
out=/tmp/fftw-warmup.$$.out
log=/tmp/fftw-warmup.$$.log

srsue "$cfg" --log.filename "$log" --expert.lte_sample_rates=true \
      --rf.device_name file --rf.device_args "rx_file=/dev/zero,base_srate=1920000" \
      --rat.eutra.dl_earfcn 6200 >"$out" 2>&1 &
pid=$!
t0=$SECONDS
warned=0
while kill -0 $pid 2>/dev/null && ! grep -q "Attaching UE" "$out"; do
  if [[ $warned -eq 0 && $((SECONDS - t0)) -ge 5 ]]; then
    echo "[fftw] computing srsue FFTW plans (first run on this machine; can take ~20 min on ARM64)"
    warned=1
  fi
  sleep 1
done
if ! kill -0 $pid 2>/dev/null; then
  echo "[fftw] srsue exited before the PHY was up:"
  grep -v governor "$out" | tail -5
  rm -f "$out" "$log"
  exit 1
fi
kill -INT $pid
# srsue saves the wisdom on exit (also on its forced exit after 5 s)
for _ in $(seq 20); do kill -0 $pid 2>/dev/null || break; sleep 1; done
kill -9 $pid 2>/dev/null
wait $pid 2>/dev/null
[[ $warned -eq 1 ]] && echo "[fftw] FFTW plans saved after $((SECONDS - t0)) s"
rm -f "$out" "$log"
exit 0
