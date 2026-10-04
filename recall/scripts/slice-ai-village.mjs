#!/usr/bin/env node
// Extract a time window from a downloaded AI Village table so it can be imported into RECALL.
// The dataset is gated: request access at https://huggingface.co/datasets/aidigestorg/ai-village,
// then e.g. `huggingface-cli download aidigestorg/ai-village chat_messages.jsonl.gz --repo-type dataset`.
//
// Usage: node scripts/slice-ai-village.mjs <table.jsonl.gz> <fromISO> <toISO> [out.jsonl]
import { createReadStream, createWriteStream } from 'node:fs';
import { createGunzip } from 'node:zlib';
import { createInterface } from 'node:readline';

const [file, from, to, out = 'ai-village-slice.jsonl'] = process.argv.slice(2);
if (!file || !from || !to) {
  console.error('Usage: node scripts/slice-ai-village.mjs <table.jsonl.gz> <fromISO> <toISO> [out.jsonl]');
  process.exit(1);
}
const lo = Date.parse(from);
const hi = Date.parse(to);
const input = file.endsWith('.gz') ? createReadStream(file).pipe(createGunzip()) : createReadStream(file);
const sink = createWriteStream(out);
let kept = 0;
let seen = 0;
for await (const line of createInterface({ input, crlfDelay: Infinity })) {
  if (!line.trim()) continue;
  seen++;
  const row = JSON.parse(line);
  const t = Date.parse(row.created_at ?? row.timestamp ?? row.started_at ?? row.data?.timestamp ?? '');
  if (t >= lo && t < hi) { sink.write(line + '\n'); kept++; }
}
sink.end();
console.log(`kept ${kept} of ${seen} rows → ${out}`);
