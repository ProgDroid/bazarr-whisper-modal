"""Modal-hosted faster-whisper transcription backend for Bazarr.

This is the GPU half of the system. It exposes a plain Modal *function* (a class
method), NOT a web endpoint — so it is bounded by ``timeout`` (3600 s) rather
than Modal's 150 s web-request ceiling, which is what makes long-movie
transcription possible. The local ``shim/`` service invokes this over the Modal
SDK (``Cls.from_name`` + ``.remote()``) and presents a plain-HTTP, Bazarr-shaped
API on the other side.

Audio contract: the shim re-encodes Bazarr's raw PCM (s16le, mono, 16 kHz) to
FLAC and sends that — lossless but ~2x smaller, which keeps the memory-tight shim
container from OOMing on the multi-hundred-MB upload. faster-whisper decodes the
FLAC here (via PyAV) where RAM is plentiful. Any PyAV-decodable container works.

Deploy:
    modal deploy modal_whisper.py
"""

import io

import modal

APP_NAME = "bazarr-whisper"
MODEL = "large-v3"  # best quality; ~3 GB in float16, fits a 16 GB T4
GPU = "T4"  # matches the proven podcast-transcriber rig; cheapest that fits
CACHE_DIR = "/cache"  # persisted model cache (downloads once)
SAMPLE_RATE = 16000

# CUDA 12.8 + cuDNN runtime base — same CUDA/cuDNN lineage as the working
# podcast-transcriber app, which is what ctranslate2 (faster-whisper's backend)
# needs on the GPU. Bundling it here avoids a cuDNN-not-found loop. `runtime`
# (not `devel`) since plain faster-whisper needs no toolkit/torch.
image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.8.0-cudnn-runtime-ubuntu22.04", add_python="3.12"
    )
    .entrypoint([])  # drop the base image's noisy entrypoint
    # `requests` is imported by faster-whisper 1.1.1's utils.py but missing from
    # its declared deps, so we add it explicitly.
    .pip_install("faster-whisper==1.1.1", "numpy<2", "requests")
)

app = modal.App(APP_NAME, image=image)
model_cache = modal.Volume.from_name("whisper-model-cache", create_if_missing=True)

with image.imports():
    from faster_whisper import WhisperModel


# --- subtitle formatting (pure-python, runs in the container) ----------------
def _ts(seconds: float, sep: str = ",") -> str:
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _format(segments, output: str) -> str:
    output = (output or "srt").lower()
    if output == "txt":
        return "".join(seg.text for seg in segments).strip() + "\n"
    if output == "vtt":
        lines = ["WEBVTT", ""]
        for seg in segments:
            lines.append(f"{_ts(seg.start, '.')} --> {_ts(seg.end, '.')}")
            lines.append(seg.text.strip())
            lines.append("")
        return "\n".join(lines)
    # default: SRT (the format Bazarr always requests)
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{_ts(seg.start)} --> {_ts(seg.end)}")
        lines.append(seg.text.strip())
        lines.append("")
    return "\n".join(lines)


_LANG_NAMES = {
    "en": "english",
    "es": "spanish",
    "fr": "french",
    "de": "german",
    "it": "italian",
    "pt": "portuguese",
    "nl": "dutch",
    "ja": "japanese",
    "zh": "chinese",
    "ko": "korean",
    "ru": "russian",
    "ar": "arabic",
    "hi": "hindi",
    "tr": "turkish",
    "pl": "polish",
    "sv": "swedish",
    "da": "danish",
    "no": "norwegian",
    "fi": "finnish",
    "cs": "czech",
}


@app.cls(
    gpu=GPU,
    volumes={CACHE_DIR: model_cache},
    scaledown_window=300,  # stay warm 5 min so a scan batch reuses one GPU
    timeout=3600,  # ceiling for a single (long-movie) transcription
)
class Whisper:
    @modal.enter()
    def load(self):
        # Loaded once per container, reused across requests while warm.
        self.model = WhisperModel(
            MODEL, device="cuda", compute_type="float16", download_root=CACHE_DIR
        )

    @modal.method()
    def transcribe(
        self,
        audio_bytes: bytes,
        task: str = "transcribe",
        language: str | None = None,
        output: str = "srt",
    ) -> str:
        # The shim sends FLAC (lossless); faster-whisper decodes + resamples it
        # via PyAV, so we just hand it the bytes as a file-like object.
        segments, _info = self.model.transcribe(
            io.BytesIO(audio_bytes),
            task=task,
            language=language or None,
            vad_filter=True,
        )
        return _format(list(segments), output)

    @modal.method()
    def detect_language(self, audio_bytes: bytes) -> dict:
        # faster-whisper resolves the language eagerly during transcribe() and
        # returns it on `info`; segment generation stays lazy, so this is cheap.
        # The shim already trimmed to the first DETECT_SECONDS before encoding.
        _segments, info = self.model.transcribe(io.BytesIO(audio_bytes), language=None)
        return {
            "language_code": info.language,
            "detected_language": _LANG_NAMES.get(info.language, info.language),
            "confidence": float(info.language_probability),
        }


@app.local_entrypoint()
def smoke():
    """`modal run modal_whisper.py` — 1s of silence, just proves the GPU path loads."""
    import wave

    # Wrap 1 s of s16le silence in a WAV header so PyAV can decode it, matching
    # the now-encoded audio contract (stdlib `wave` keeps this dependency-free).
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(b"\x00\x00" * SAMPLE_RATE)
    silence = buf.getvalue()
    print("detect:", Whisper().detect_language.remote(silence))
    print("srt:", repr(Whisper().transcribe.remote(silence)))
