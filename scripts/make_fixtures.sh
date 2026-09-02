#!/usr/bin/env bash
# Regenerates the QC test fixtures from scratch with ffmpeg.
#
# These are TEST FIXTURES with exact known utterance boundaries, which is what
# lets tests/test_qc_probes.py assert to the millisecond. They are not demo
# data: the demo runs on the real master in assets/, never on these.
set -euo pipefail
cd "$(dirname "$0")/../media/fixtures"

gen() {
  ffmpeg -y -v error -f lavfi \
    -i "aevalsrc=0.4*sin(2*PI*220*t)*($2):d=10:s=48000" -ac 1 "$1"
  echo "  wrote $1"
}

echo "generating fixtures..."
# reference master: utterances at 0.5-2.0, 3.0-5.0, 6.5-9.0 s
gen ref_scene14.wav "between(t\,0.5\,2)+between(t\,3\,5)+between(t\,6.5\,9)"
# in-tolerance dub: uniformly ~40 ms late  -> systematic drift, retime fixes it
gen dub_fr_ok.wav   "between(t\,0.54\,2.04)+between(t\,3.04\,5.04)+between(t\,6.53\,9.02)"
# overrunning dub: lines exceed their slots and start progressively later
#   -> scattered drift, retime CANNOT fix it, agent must replan to a rewrite
gen dub_de_bad.wav  "between(t\,0.5\,2.35)+between(t\,3.35\,5.7)+between(t\,7.1\,9.6)"
echo "done."
