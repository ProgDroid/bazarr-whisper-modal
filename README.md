# bazarr-whisper-modal

Serverless [Whisper](https://github.com/SYSTRAN/faster-whisper) subtitle
generation for [Bazarr](https://www.bazarr.media/), backed by a
[Modal](https://modal.com) GPU that **scales to zero** between jobs.

Bazarr's Whisper provider expects a plain-HTTP service speaking the
`whisper-asr-webservice` API. We don't run that service on an always-on GPU box.
Instead:

```
Bazarr ──http POST /asr──▶ whisper-shim ──Modal SDK .remote()──▶ Modal GPU (faster-whisper, large-v3)
       ◀──── SRT ────────  (local, same   ◀──────── SRT ────────  T4, scale-to-zero
                            Docker network)
```

- **`modal_whisper.py`** — a Modal GPU **function** (not a web endpoint) running
  faster-whisper `large-v3` on a T4. As a function it's bounded by its 3600 s
  `timeout`, not Modal's 150 s web-request ceiling — which is what makes
  full-length movies work.
- **`shim/`** — a tiny FastAPI service you run next to Bazarr. It speaks the
  Bazarr-shaped HTTP API on plain `http://`, and forwards to the Modal function
  over the SDK. Published to GHCR by CI so you just pull it.

## Why a shim (and not point Bazarr straight at Modal)?

Two hard blockers, both solved by the shim:

1. **Bazarr rejects `https://`** ("Wrong URL Base") — it wants `http://`. The
   shim is local plain-HTTP.
2. **Modal web endpoints time out at 150 s**, then hand back a 303 poll-redirect
   that Bazarr can't follow. Calling the Modal *function* over the SDK has no
   such limit — it runs up to the function `timeout`.

---

## Part 1 — Deploy the Modal backend

From a machine with the Modal CLI authed:

```bash
pip install -r requirements.txt
modal token new          # first time only
modal deploy modal_whisper.py
```

Optional sanity check (spins a T4, downloads the model into the cache volume
once, transcribes 1 s of silence):

```bash
modal run modal_whisper.py
# -> detect: {'language_code': 'en', ...}
# -> srt: ''
```

The app deploys as **`bazarr-whisper`**; the shim looks it up by that name.

## Part 2 — Run the shim next to Bazarr

The shim authenticates to Modal with a token. Create one at
**<https://modal.com/settings/tokens>**, then drop it in a `.env` beside the
compose file:

```dotenv
MODAL_TOKEN_ID=ak-...
MODAL_TOKEN_SECRET=as-...
```

Pull and start it (image is published to GHCR by CI):

```bash
docker compose pull
docker compose up -d
```

**Networking:** the shim must share a Docker network with Bazarr.
- *Easiest:* paste the `whisper-shim` service block into your existing Bazarr
  `docker-compose.yml`. Then Bazarr reaches it at `http://whisper-shim:9000`.
- *Standalone:* uncomment the `networks:` block in `docker-compose.yml`, set it
  to your Bazarr stack's network (`docker network ls`), and use the same URL.
- *By host IP:* keep the published `9000:9000` port and use
  `http://<server-ip>:9000`.

## Part 3 — Configure Bazarr

**Settings → Providers → enable "Whisper":**

| Field | Value |
| --- | --- |
| **Whisper Model endpoint** | `http://whisper-shim:9000` |
| **Connection/response timeout (s)** | `60` |
| **Transcription/translation timeout (s)** | `3600` |
| **Pass video name to Whisper** | off (the shim ignores it) |
| **Logging level** | `INFO` |

Then **Settings → Subtitles**: put your target languages in a profile and enable
the deep media-analysis option so Bazarr detects audio tracks correctly. Bazarr
extracts the audio itself (it needs its own `ffmpeg`) and sends raw 16 kHz PCM —
the shim and Modal handle it from there.

**Notes**

- The **first** request after a deploy is slow (cold start + one-time model
  download into the Modal volume); subsequent cold starts are ~15–30 s. The
  `3600 s` transcription timeout covers a long movie plus a cold start.
- Whisper is a **last-resort** provider — Bazarr only calls it when its normal
  subtitle sources come up empty, so real-world volume (and cost) is low.
- **Memory:** Bazarr sends the whole audio track as raw PCM (~230 MB for a 2 h
  movie). The shim stream-encodes it to FLAC (lossless, ~half the size) before
  handing it to Modal, so its peak RSS stays well under ~1 GB instead of the
  ~1 GB the old raw-PCM path hit — which was OOM-killing the container on
  memory-tight hosts and tripping Bazarr's 24 h provider throttle. If your host
  caps the container's memory, give it ≥ 1 GB.

---

## CI / GHCR

`.github/workflows/build-shim.yml` builds `shim/` for `linux/amd64` and
`linux/arm64` and pushes to **`ghcr.io/<owner>/bazarr-whisper-shim:latest`** on
every push to `main` that touches `shim/` (or via manual *Run workflow*). The
package may default to private — make it public, or `docker login ghcr.io` on
your server with a PAT that has `read:packages`.

## Tuning

`modal_whisper.py` constants:

| Constant | Default | Notes |
| --- | --- | --- |
| `GPU` | `"T4"` | Cheapest that fits `large-v3`; `"L4"`/`"A10G"` are faster. |
| `MODEL` | `"large-v3"` | `"large-v2"` or `"medium"` for faster/cheaper runs. |
| `scaledown_window` | `300` | Seconds kept warm after a request (fewer cold starts vs more idle GPU). |
| `timeout` | `3600` | Max seconds for one transcription. |

Redeploy after changes: `modal deploy modal_whisper.py`.

## Security & cost

- The shim talks to Modal with **your token** and is reachable only on your
  Docker network — it is not exposed to the internet (unlike a raw Modal web
  URL). Keep the `.env` token out of git (it's gitignored).
- **Idle cost: $0** (scale-to-zero). Active: a couple of cents of T4 time per
  movie — comfortably inside Modal's monthly free credits for fallback usage.

## Operational notes

Running this against full-length films surfaced a failure worth reading before
you deploy it, because the fix shaped the current architecture:

- **[Incident: the shim was being OOM-killed on long films](docs/incident-2026-06-29-shim-oom.md)**
  — passing large `bytes` through Modal's `.remote()` arguments amplifies memory
  3-4x, which tripped the shim container's memory cgroup on a two-hour film.
  Includes the diagnostic trap that `docker inspect` reports `OOMKilled=false`
  for host-level kills, so `dmesg -T` is the decisive evidence.

The practical rule it produced: **never push large payloads through Modal
function arguments.** Compress first, or use a Volume. That is why the shim
stream-encodes to FLAC rather than sending raw PCM.

## Licence

MIT. See [LICENSE](LICENSE).
