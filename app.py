"""
Live Translation & Transcription App
Uses faster-whisper for speech recognition and Claude Opus 4.6 for translation.
"""

import asyncio
import io
import json
import logging
import os
import struct
import tempfile
import wave
from pathlib import Path

import anthropic
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Live Translation & Transcription")

# Mount static files
static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Initialize Whisper model (base is a good balance of speed and accuracy)
# Downloads ~145 MB on first run; subsequent runs use cache
logger.info("Loading Whisper model...")
whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
logger.info("Whisper model loaded.")

# Initialize Anthropic client
claude_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SAMPLE_RATE = 16000  # Hz - Whisper expects 16 kHz
CHUNK_SAMPLES = SAMPLE_RATE * 3  # 3-second chunks


def pcm_bytes_to_float32(pcm_bytes: bytes) -> np.ndarray:
    """Convert raw 16-bit PCM bytes to float32 array in range [-1, 1]."""
    count = len(pcm_bytes) // 2
    samples = struct.unpack(f"<{count}h", pcm_bytes)
    audio = np.array(samples, dtype=np.float32) / 32768.0
    return audio


def audio_to_wav_bytes(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Convert float32 numpy array to WAV bytes for Whisper."""
    pcm = (audio * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


async def transcribe_audio(audio: np.ndarray) -> dict:
    """
    Run Whisper transcription in a thread pool (CPU-bound).
    Returns dict with 'text', 'language', 'language_probability'.
    """
    loop = asyncio.get_event_loop()

    def _transcribe():
        # Write to temp WAV file — faster-whisper can accept file path
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_to_wav_bytes(audio))
            tmp_path = f.name

        try:
            segments, info = whisper_model.transcribe(
                tmp_path,
                beam_size=5,
                language=None,  # auto-detect
                task="transcribe",
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            return {
                "text": text,
                "language": info.language,
                "language_probability": round(info.language_probability, 3),
            }
        finally:
            os.unlink(tmp_path)

    return await loop.run_in_executor(None, _transcribe)


async def translate_to_english(text: str, source_language: str) -> str:
    """
    Use Claude Opus 4.6 with streaming to translate text to English.
    Returns the translated text.
    """
    loop = asyncio.get_event_loop()

    def _translate():
        full_text = ""
        with claude_client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=1024,
            thinking={"type": "adaptive"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Translate the following {source_language} text to English. "
                        f"Return ONLY the English translation, nothing else:\n\n{text}"
                    ),
                }
            ],
        ) as stream:
            for chunk in stream.text_stream:
                full_text += chunk
        return full_text.strip()

    return await loop.run_in_executor(None, _translate)


@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = Path("templates/index.html")
    return HTMLResponse(content=html_path.read_text())


@app.websocket("/ws/audio")
async def audio_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for live audio streaming.

    Protocol (client → server):
      Binary frames: raw 16-bit PCM at 16 kHz, mono

    Protocol (server → client):
      JSON text frames:
        {"type": "processing"}
        {"type": "result", "transcription": "...", "translation": "...",
         "source_language": "fr", "language_probability": 0.99,
         "is_english": false}
        {"type": "error", "message": "..."}
    """
    await websocket.accept()
    logger.info("WebSocket client connected")

    audio_buffer = np.array([], dtype=np.float32)

    try:
        while True:
            # Receive audio chunk (binary PCM)
            try:
                data = await asyncio.wait_for(websocket.receive_bytes(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning("No audio received for 30s, closing connection")
                break

            if not data:
                continue

            # Decode and accumulate
            chunk = pcm_bytes_to_float32(data)
            audio_buffer = np.concatenate([audio_buffer, chunk])

            # Process when we have enough audio (~3 seconds)
            if len(audio_buffer) < CHUNK_SAMPLES:
                continue

            audio_to_process = audio_buffer[:CHUNK_SAMPLES]
            audio_buffer = audio_buffer[CHUNK_SAMPLES:]

            # Skip near-silent chunks
            rms = float(np.sqrt(np.mean(audio_to_process ** 2)))
            if rms < 0.005:
                continue

            await websocket.send_text(json.dumps({"type": "processing"}))

            try:
                result = await transcribe_audio(audio_to_process)
                text = result["text"]
                lang = result["language"]
                lang_prob = result["language_probability"]

                if not text:
                    continue

                is_english = lang == "en"
                translation = text if is_english else await translate_to_english(text, lang)

                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "result",
                            "transcription": text,
                            "translation": translation,
                            "source_language": lang,
                            "language_probability": lang_prob,
                            "is_english": is_english,
                        }
                    )
                )
            except Exception as exc:
                logger.exception("Processing error")
                await websocket.send_text(
                    json.dumps({"type": "error", "message": str(exc)})
                )

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception:
        logger.exception("Unexpected WebSocket error")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
