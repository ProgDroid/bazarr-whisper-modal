"""Local HTTP shim: Bazarr's Whisper provider <-> Modal GPU function.

Why this exists:
  1. Bazarr's Whisper provider only accepts a plain ``http://`` endpoint
     ("Wrong URL Base" on an ``https://`` Modal URL).
  2. Modal *web* endpoints cap a request at 150 s; a 2-hour movie far exceeds
     that. So we don't proxy HTTP at all — we call the deployed Modal *function*
     over the SDK (``Cls.from_name`` + ``.remote()``), which runs up to the
     function's own 3600 s timeout.

This service speaks exactly the subset of the whisper-asr-webservice API that
Bazarr uses (POST /asr, POST /detect-language), so Bazarr thinks it's talking to
a normal local Whisper container. Run it on the same Docker network as Bazarr.

Auth: the Modal SDK reads MODAL_TOKEN_ID / MODAL_TOKEN_SECRET from the env.
"""

import os
import tempfile

import modal
import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response

APP_NAME = os.environ.get("MODAL_APP_NAME", "bazarr-whisper")
CLASS_NAME = "Whisper"
SAMPLE_RATE = 16000
DETECT_SECONDS = 30  # language detection only needs a short head of the audio
READ_CHUNK = 1 << 22  # 4 MiB of PCM read per loop; bounds peak memory

api = FastAPI(title="bazarr-whisper-shim")

_whisper = None


def _get_whisper():
    """Resolve the deployed Modal class lazily so import never hits the network."""
    global _whisper
    if _whisper is None:
        _whisper = modal.Cls.from_name(APP_NAME, CLASS_NAME)()
    return _whisper


async def _upload_to_flac(
    audio_file: UploadFile, max_bytes: int | None = None
) -> bytes:
    """Stream the s16le/mono/16 kHz upload straight into FLAC on disk.

    Bazarr ships raw headerless PCM (~230 MB for a 2 h movie). Reading that whole
    stream into one ``bytes`` and handing it to Modal made the SDK copy it 3-4x
    (read + pickle + gRPC), peaking ~1 GiB and OOM-killing this 1 GiB container.
    Instead we encode it — from a bounded read loop, so peak RSS is ~one chunk —
    into FLAC (lossless, ~2x smaller) written to a temp file on disk, then return
    just that small blob. Modal decodes it on the GPU box, which has the RAM to
    spare. ``max_bytes`` caps the read for language detection, which only needs
    the first DETECT_SECONDS of audio.
    """
    read_total = 0
    carry = b""  # keeps a trailing odd byte so int16 frames stay aligned
    out = tempfile.TemporaryFile()
    try:
        with sf.SoundFile(
            out,
            mode="w",
            samplerate=SAMPLE_RATE,
            channels=1,
            format="FLAC",
            subtype="PCM_16",
        ) as snd:
            while max_bytes is None or read_total < max_bytes:
                want = READ_CHUNK
                if max_bytes is not None:
                    want = min(want, max_bytes - read_total)
                chunk = await audio_file.read(want)
                if not chunk:
                    break
                read_total += len(chunk)
                buf = carry + chunk
                aligned = len(buf) - (len(buf) % 2)
                carry = buf[aligned:]
                if aligned:
                    snd.write(np.frombuffer(buf, dtype=np.int16, count=aligned // 2))
        out.seek(0)
        return out.read()
    finally:
        out.close()


@api.get("/")
async def root():
    return {"status": "ok", "service": "bazarr-whisper-shim", "modal_app": APP_NAME}


@api.get("/status")
async def status():
    # Bazarr's "Test Connection" GETs /status and reads result.json()['version'].
    # A 200 with a `version` key turns the button green; a 404 shows
    # "Connected but no version found". (bazarr/app/ui.py::proxy)
    return {"version": "bazarr-whisper-modal shim (faster-whisper large-v3 on Modal)"}


@api.post("/asr")
async def asr(request: Request, audio_file: UploadFile = File(...)):
    q = request.query_params
    task = q.get("task", "transcribe")
    language = q.get("language") or None
    output = q.get("output", "srt")
    flac = await _upload_to_flac(audio_file)

    subtitle = await _get_whisper().transcribe.remote.aio(flac, task, language, output)
    # Bazarr reads the raw response body as the subtitle file (r.content).
    return Response(content=subtitle, media_type="text/plain; charset=utf-8")


@api.post("/detect-language")
async def detect_language(request: Request, audio_file: UploadFile = File(...)):
    # Language ID only needs the first DETECT_SECONDS, so cap the read there
    # (s16le mono @16 kHz = 2 bytes/sample) — no need to ship a whole 2 h PCM
    # stream just to identify the language. _upload_to_flac streams the capped
    # prefix into a tiny FLAC instead of buffering raw PCM in memory.
    flac = await _upload_to_flac(audio_file, max_bytes=SAMPLE_RATE * DETECT_SECONDS * 2)
    result = await _get_whisper().detect_language.remote.aio(flac)
    return JSONResponse(result)
