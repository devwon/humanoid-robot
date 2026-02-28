"""Gemini audio module - TTS (speech synthesis) and STT (transcription) via Gemini API."""

import base64
import logging

import httpx

logger = logging.getLogger(__name__)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_TTS_ENDPOINT = f"{GEMINI_API_URL}/gemini-2.5-flash-preview-tts:generateContent"
GEMINI_STT_MODEL = "gemini-2.5-flash"

VOICES = {
    "default": "Kore",
    "male": "Charon",
    "bright": "Zephyr",
    "upbeat": "Puck",
}


async def generate_tts(text: str, api_key: str, voice: str = "Kore") -> bytes | None:
    """Generate TTS audio via Gemini API. Returns audio bytes or None."""
    voice_name = VOICES.get(voice, voice)

    payload = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": voice_name}
                }
            },
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{GEMINI_TTS_ENDPOINT}?key={api_key}",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()

        data = resp.json()
        inline = data["candidates"][0]["content"]["parts"][0].get("inlineData", {})
        audio_b64 = inline.get("data")
        if not audio_b64:
            logger.warning("No audio data in Gemini TTS response")
            return None

        return base64.b64decode(audio_b64)
    except Exception as e:
        logger.error("Gemini TTS error: %s", e)
        return None


async def generate_tts_base64(
    text: str, api_key: str, voice: str = "Kore"
) -> str | None:
    """Generate TTS and return as base64-encoded audio for WebSocket."""
    voice_name = VOICES.get(voice, voice)

    payload = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": voice_name}
                }
            },
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{GEMINI_TTS_ENDPOINT}?key={api_key}",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()

        data = resp.json()
        part = data["candidates"][0]["content"]["parts"][0]
        inline = part.get("inlineData", {})
        audio_b64 = inline.get("data")
        mime_type = inline.get("mimeType", "audio/wav")

        if not audio_b64:
            logger.warning("No audio data in Gemini TTS response")
            return None

        return f"data:{mime_type};base64,{audio_b64}"
    except Exception as e:
        logger.error("Gemini TTS error: %s", e)
        return None


# --- STT (Speech-to-Text) ---


_STT_PROMPT = (
    "You are a speech-to-text transcriber. "
    "Listen to this audio and output ONLY the spoken words in Korean. "
    "Rules:\n"
    "- Output ONLY the transcription, nothing else.\n"
    "- If the audio is silent or contains only noise, output exactly: [SILENCE]\n"
    "- Do not add any explanation, punctuation notes, or commentary.\n"
    "- Transcribe exactly what was said."
)

# Fragments that indicate Gemini echoed the prompt or hallucinated
_NOISE_PATTERNS = [
    "전사해", "텍스트만 출력", "다른 설명은",
    "[SILENCE]", "[silence]", "silence", "SILENCE",
    "transcri", "speech-to-text", "spoken words",
    "오디오", "음성이", "들리지 않", "알아들을 수 없",
]


async def transcribe_audio(
    audio_b64: str, api_key: str, mime_type: str = "audio/webm"
) -> str | None:
    """Transcribe audio via Gemini API. Returns Korean text or None."""
    url = f"{GEMINI_API_URL}/{GEMINI_STT_MODEL}:generateContent?key={api_key}"

    payload = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": mime_type, "data": audio_b64}},
                    {"text": _STT_PROMPT},
                ]
            }
        ],
        "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.0},
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url, json=payload, headers={"Content-Type": "application/json"}
            )
            if resp.status_code != 200:
                logger.error("Gemini STT error %d: %s", resp.status_code, resp.text[:300])
                return None

            data = resp.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            text = " ".join(p["text"] for p in parts if "text" in p).strip()
            if not text:
                return None

            logger.info("STT raw result: %s", text[:200])

            # Filter noise/hallucination
            if any(f in text.lower() for f in _NOISE_PATTERNS):
                logger.info("STT filtered as noise: %s", text[:100])
                return None

            # Too short (likely noise)
            cleaned = text.strip()
            if len(cleaned) < 2:
                logger.info("STT too short, filtered: %s", cleaned)
                return None

            return cleaned
    except Exception as e:
        logger.error("Gemini STT error: %s", e)
        return None
