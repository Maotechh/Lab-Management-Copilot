import httpx
import pytest

from consumable_assistant import chat, chat_runtime, dsh_client
from consumable_assistant.database import connect, init_db


def mock_client(monkeypatch, body):
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=body))
    client_type = dsh_client.httpx.Client

    def client(**kwargs):
        kwargs["transport"] = transport
        return client_type(**kwargs)

    monkeypatch.setattr(
        dsh_client.httpx,
        "Client",
        client,
    )


def test_run_dsh_accepts_a_completed_result(monkeypatch):
    mock_client(monkeypatch, {"runId": "run-1", "status": "completed", "reply": "ok"})

    assert dsh_client.run_dsh({"runId": "run-1"})["status"] == "completed"


def test_run_dsh_preserves_a_safe_failure_status(monkeypatch):
    result = {"runId": "run-1", "status": "MODEL_FAILED", "reply": ""}
    mock_client(monkeypatch, result)

    assert dsh_client.run_dsh({"runId": "run-1"}) == result
    assert dsh_client.result_error_code(result) == "MODEL_FAILED"


def test_run_dsh_rejects_a_result_without_status(monkeypatch):
    mock_client(monkeypatch, {"runId": "run-1", "reply": "ok"})

    with pytest.raises(dsh_client.DSHUnavailable) as error:
        dsh_client.run_dsh({"runId": "run-1"})

    assert error.value.code == "DSH_INVALID_RESPONSE"


def test_process_run_persists_model_failure_code(tmp_path, monkeypatch):
    with connect(tmp_path / "chat.db") as db:
        init_db(db)
        conversation = chat.new_conversation(db, "device")
        request = chat.ChatMessageRequest(message_id="message-1", text="查询库存")
        queued = chat_runtime.enqueue(db, conversation["conversation_id"], request, "Teacher", "device")
        run = chat_runtime.claim(db, queued["run_id"])
        monkeypatch.setattr(
            chat_runtime,
            "run_dsh",
            lambda payload: {"runId": payload["runId"], "status": "MODEL_FAILED", "reply": ""},
        )

        response = chat_runtime.process_run(db, run, lambda *_args: None, lambda *_args: [])

        assert response["status"] == "failed"
        assert response["error_code"] == "MODEL_FAILED"
        assert db.execute("SELECT error FROM chat_runs WHERE id=?", (run["id"],)).fetchone()[0] == "MODEL_FAILED"
def test_voice_repackages_wav_metadata_and_rejects_wrong_format():
    import io
    import wave

    import pytest

    from consumable_assistant.dsh_client import canonical_voice_wave

    output = io.BytesIO()
    with wave.open(output, "wb") as source:
        source.setparams((1, 2, 16000, 2, "NONE", ""))
        source.writeframes(b"\x01\x00\x02\x00")
    canonical = output.getvalue()
    extra = b"LIST\x04\x00\x00\x00INFO"
    with_metadata = canonical[:4] + (len(canonical) + len(extra) - 8).to_bytes(4, "little") + canonical[8:36] + extra + canonical[36:]
    assert canonical_voice_wave(with_metadata) == canonical
    with pytest.raises(ValueError, match="不完整"):
        canonical_voice_wave(canonical[:-2])
