#!/bin/bash
# Download computer_use_turns.jsonl.gz (2.5 GB) into .hf/ with byte-level resume and stall detection.
# Long single HTTP streams from Hugging Face get dropped; curl -C - resumes exactly where it stopped.
set -u
cd "$(dirname "$0")/.." && mkdir -p .hf && cd .hf
URL="https://huggingface.co/datasets/aidigestorg/ai-village/resolve/main/computer_use_turns.jsonl.gz"
OUT="computer_use_turns.jsonl.gz"
TOKEN=${HF_TOKEN:-$(cat ~/.cache/huggingface/token 2>/dev/null)}
[ -z "$TOKEN" ] && { echo "No Hugging Face token. Run: hf auth login (after access to aidigestorg/ai-village is approved)"; exit 1; }
WANT=$(curl -sIL -H "Authorization: Bearer $TOKEN" "$URL" | awk 'tolower($1)=="content-length:"{v=$2} END{gsub("\r","",v); print v}')
[ -f "$OUT" ] && [ "$(stat -f%z "$OUT" 2>/dev/null || stat -c%s "$OUT")" = "$WANT" ] && { echo "already downloaded"; exit 0; }
for i in $(seq 1 60); do
  HAVE=$(stat -f%z "$OUT.part" 2>/dev/null || stat -c%s "$OUT.part" 2>/dev/null || echo 0)
  if [ -n "$WANT" ] && [ "$HAVE" -ge "$WANT" ]; then mv "$OUT.part" "$OUT"; gzip -t "$OUT" && echo "done: $OUT ($WANT bytes, gzip OK)"; exit 0; fi
  echo "attempt $i: resuming at $HAVE / $WANT bytes"
  curl -sSL -H "Authorization: Bearer $TOKEN" -C - --speed-limit 50000 --speed-time 30 --connect-timeout 20 -o "$OUT.part" "$URL" || true
  sleep 2
done
echo "gave up after 60 attempts"; exit 1
