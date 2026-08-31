# Incident: the shim was being OOM-killed on long films

**Date:** 2026-06-29 · **Fixed in:** `3943d1d` · **Status:** resolved

## Symptom

Long transcriptions failed. Bazarr reported a `ConnectionError` and then
**throttled the provider for 24 hours**, so a single failure cost a day of
service rather than one job. Short clips worked fine, which is what made it look
intermittent rather than systematic.

## What was actually happening

The `whisper-shim` container is capped at **~1 GiB RAM**. That limit is *not* in
`docker-compose.yml` — it is imposed by the host/LXC, which had only ~2 GiB free
in total and was not easily expandable.

The original path read the whole upload into memory and handed the bytes to
Modal. **Passing large `bytes` through Modal's SDK `.remote()` arguments
amplifies memory roughly 3–4×**: the read copy, then pickle serialisation, then
the gRPC framing buffer, all live at once.

A two-hour film is about **230 MB of raw PCM**. Times three to four, that path
peaked near 1 GiB, tripped the container's memory-cgroup OOM killer, and uvicorn
was SIGKILLed. The container then restarted silently, so from Bazarr's side the
connection simply dropped.

## The diagnostic trap

`docker inspect` reported **`OOMKilled=false`**.

That flag is unreliable for host or global kills, and `inspect` only reports the
*latest* exit rather than the killed ones, so it actively pointed away from the
real cause. The decisive evidence came from the kernel instead:

```bash
dmesg -T | grep -i oom
# constraint=CONSTRAINT_MEMCG ... Killed process (uvicorn)
```

`CONSTRAINT_MEMCG` names the cgroup as the limit that was hit, which is what
distinguishes this from the host simply running out of memory.

## The fix

Stream-encode the upload to **FLAC before the Modal call** and decode it on the
Modal side via faster-whisper/PyAV. Lossless, so transcription quality is
untouched, and peak RSS drops to about **0.6 GiB** with headroom under the cap.

## The rule that came out of it

**Never push large payloads through Modal function arguments.** Compress first
(FLAC by default, Opus if a very long film still gets close), or use a Modal
Volume or object store. Keep the shim's transport small.

The general form: a serialisation boundary is a memory multiplier, not a
pass-through. Budget for the peak across the whole encode path, not for the size
of the thing you think you are sending.

## What changed structurally, so it cannot recur

The transport is now bounded by the compressed size rather than by the source
media's duration, so the failure mode does not scale back in with longer input.
The previous design had no such bound: it would have failed again on any file
large enough, and the 24-hour provider throttle meant each recurrence was
expensive to even observe.
