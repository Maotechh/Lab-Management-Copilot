"""Private DSH transport. No business execution capability is sent to DSH."""
from __future__ import annotations

import io
import os
import wave
from urllib.parse import urlsplit

import httpx

DSH_FAILURE_CODES = frozenset({
    "MODEL_FAILED", "BUSINESS_CALLBACK_FAILED", "RUN_LIMIT", "DSH_UNAVAILABLE",
    "DSH_INVALID_RESPONSE", "DSH_CONFIGURATION",
})


def result_error_code(result: dict) -> str:
    status = result.get("status") if isinstance(result, dict) else None
    return status if isinstance(status, str) and status in DSH_FAILURE_CODES else "DSH_FAILED"


class DSHUnavailable(RuntimeError):
    """The local agent did not return a validated, persisted result."""

    def __init__(self, message: str, code: str = "DSH_UNAVAILABLE"):
        super().__init__(message)
        self.code = code


def gateway_url() -> str:
    value = os.environ.get("LAB_DSH_URL", "http://127.0.0.1:8766")
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path not in ("", "/") or parsed.username:
        raise ValueError("DSH 地址必须是服务器本机 HTTP 接口")
    return value.rstrip("/")


def run_dsh(payload: dict) -> dict:
    try:
        with httpx.Client(trust_env=False, timeout=httpx.Timeout(315, connect=5)) as client:
            response = client.post(gateway_url() + "/runs", json=payload)
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise DSHUnavailable("DSH 返回内容未通过校验", "DSH_INVALID_RESPONSE")
        if not isinstance(result, dict) or result.get("runId") != payload["runId"] or not isinstance(result.get("reply", ""), str):
            raise DSHUnavailable("DSH 返回内容未通过校验")
        return result
    except (httpx.HTTPError, ValueError) as exc:
        raise DSHUnavailable("校内对话服务暂时不可用，消息已保存。可以在服务恢复后发送继续说明。") from exc


def cancel_dsh(run_id: str) -> None:
    try:
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.post(gateway_url() + f"/runs/{run_id}/cancel")
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise DSHUnavailable("取消请求已保存，服务器会拒绝该运行后续的工具调用。") from exc


def canonical_voice_wave(audio: bytes) -> bytes:
    try:
        if not audio.startswith(b"RIFF"):
            raise ValueError("录音格式必须是 WAV")
        with wave.open(io.BytesIO(audio), "rb") as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
                raise ValueError("录音必须是 16 kHz 单声道 PCM16 WAV")
            frames = source.getnframes()
            if not 0 < frames <= 16000 * 120:
                raise ValueError("录音必须短于两分钟且包含音频")
            pcm = source.readframes(frames)
            if len(pcm) != frames * 2:
                raise ValueError("录音文件不完整，请重新录音")
    except (wave.Error, EOFError) as exc:
        raise ValueError("录音格式无效，请重新录音") from exc
    result = io.BytesIO()
    with wave.open(result, "wb") as destination:
        destination.setparams((1, 2, 16000, frames, "NONE", "not compressed"))
        destination.writeframes(pcm)
    return result.getvalue()


def transcribe(audio: bytes) -> dict:
    try:
        with httpx.Client(trust_env=False, timeout=125) as client:
            response = client.post(gateway_url() + "/voice", content=audio, headers={"Content-Type": "audio/wav"})
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise DSHUnavailable("语音识别结果未通过校验，请重新录音或输入文字")
        return result
    except (httpx.HTTPError, ValueError) as exc:
        raise DSHUnavailable("语音识别暂时不可用，请输入文字") from exc
