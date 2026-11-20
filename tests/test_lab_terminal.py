import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from scripts import lab_terminal as terminal


def test_report_keeps_statuses_and_excludes_chat_contents():
    run_id = "a" * 32
    requests = []

    class Site(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/":
                self.send_error(502, "Private diagnostic")
                return
            data = {
                "/api/config": {"profile": "simulation"},
                "/api/ai/status": {"status": "available", "detail": "private provider details"},
                f"/api/chat/runs/{run_id}": {
                    "run_id": run_id, "status": "failed", "actor": "private actor",
                    "result": {"error_code": "MODEL_FAILED", "reply": "private conversation"},
                },
            }[self.path]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def log_message(self, *_args):
            pass

    with HTTPServer(("127.0.0.1", 0), Site) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            report = terminal.web_report(f"http://127.0.0.1:{server.server_port}", run_id=run_id)
        finally:
            server.shutdown()
            thread.join()
    assert report["website"] == {"http_status": 502}
    assert report["model_health"]["status"] == "available"
    assert report["chat"]["error_code"] == "MODEL_FAILED"
    assert "private" not in json.dumps(report).lower()
    assert requests == ["/", "/api/config", "/api/ai/status", f"/api/chat/runs/{run_id}"]


def test_model_error_is_matched_without_exposing_session_content(tmp_path):
    conversation = "b" * 32
    path = tmp_path / f"lab-{conversation}" / "session.v4.jsonl"
    path.parent.mkdir()
    time = "2026-11-20 09:00:00"
    start = terminal.timestamp_ms(time)
    events = [
        {"type": "turn/start", "time": start + 1},
        {"type": "user/message", "time": start + 2, "data": "private conversation"},
        {"type": "turn/end", "time": start + 3, "data": {
            "reason": {"kind": "error", "error": {
                "code": "AUTH", "message": "school API error (401): private upstream response",
            }},
        }},
    ]
    run = {"conversation_id": conversation, "started_at": time, "completed_at": time}
    path.write_text("\n".join(map(json.dumps, events)), encoding="utf-8")
    assert terminal.session_failure(run, tmp_path) == {
        "status": "matched", "error_code": "AUTH", "upstream_http_status": 401,
    }
    path.write_text("\n".join(map(json.dumps, events + [events[0]])), encoding="utf-8")
    assert terminal.session_failure(run, tmp_path) == {"status": "ambiguous_time_window"}
