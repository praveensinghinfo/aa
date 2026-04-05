"""
Live Translation & Transcription App
Uses the browser's Web Speech API for transcription and Claude Opus 4.6 for translation.
No external model downloads required.
"""

import asyncio
import json
import logging
import os

import anthropic
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Live Translation & Transcription")

static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Initialize Anthropic client
claude_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


async def translate_text(text: str) -> dict:
    """
    Ask Claude to detect the language and translate to English.
    Returns dict: {translated, source_language, is_english}
    Uses streaming internally but collects the full result.
    """
    loop = asyncio.get_event_loop()

    def _call_claude():
        full = ""
        with claude_client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=(
                "You are a translation assistant. "
                "When given text, respond with a JSON object only — no markdown, no explanation. "
                'Schema: {"source_language": "<ISO 639-1 code>", "source_language_name": "<English name>", '
                '"is_english": <true|false>, "translation": "<English text or original if already English>"}'
            ),
            messages=[{"role": "user", "content": text}],
        ) as stream:
            for chunk in stream.text_stream:
                full += chunk
        return full.strip()

    raw = await loop.run_in_executor(None, _call_claude)

    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
        raw = raw.rstrip("`").strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Claude returned non-JSON: %s", raw)
        return {
            "source_language": "?",
            "source_language_name": "Unknown",
            "is_english": False,
            "translation": raw,
        }


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content=Path("templates/index.html").read_text())


@app.websocket("/ws/translate")
async def translate_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for live translation.

    Client → Server (JSON):
      {"type": "translate", "text": "bonjour le monde"}

    Server → Client (JSON):
      {"type": "result", "original": "...", "translation": "...",
       "source_language": "fr", "source_language_name": "French", "is_english": false}
      {"type": "error", "message": "..."}
    """
    await websocket.accept()
    logger.info("WebSocket client connected")

    try:
        while True:
            data = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
            msg = json.loads(data)

            if msg.get("type") != "translate" or not msg.get("text", "").strip():
                continue

            text = msg["text"].strip()
            logger.info("Translating: %s", text[:80])

            try:
                result = await translate_text(text)
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "result",
                            "original": text,
                            "translation": result.get("translation", text),
                            "source_language": result.get("source_language", "?"),
                            "source_language_name": result.get("source_language_name", "Unknown"),
                            "is_english": result.get("is_english", True),
                        }
                    )
                )
            except Exception as exc:
                logger.exception("Translation error")
                await websocket.send_text(
                    json.dumps({"type": "error", "message": str(exc)})
                )

    except asyncio.TimeoutError:
        logger.info("WebSocket timed out")
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception:
        logger.exception("Unexpected WebSocket error")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
