
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 8765
VOICE = "Daniel"
SPEECH_RATE = "170"

ALLOWED_ORIGINS = {
    "http://localhost:5173",
    "http://localhost:5174",
}

speak_lock = threading.Lock()
stage_lock = threading.Lock()
current_stage = "LISTENING"


class VoiceHandler(BaseHTTPRequestHandler):

    def send_cors(self):
        origin = self.headers.get("Origin", "")

        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS"
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type"
        )

    def send_json(self, status, data):
        message = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(message)))
        self.send_cors()
        self.end_headers()
        self.wfile.write(message)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/status":
            with stage_lock:
                stage = current_stage

            self.send_json(200, {"stage": stage})
        else:
            self.send_error(404, "Unknown endpoint")

    def do_POST(self):

        if self.path == "/stage":
            global current_stage

            try:
                length = int(self.headers.get("Content-Length", "0"))

                if length <= 0 or length > 10000:
                    raise ValueError("Invalid request size")

                body = json.loads(self.rfile.read(length))
                stage = str(body.get("stage", "")).upper()

                allowed = {
                    "LISTENING",
                    "HEARING",
                    "THINKING",
                    "SPEAKING",
                    "STANDBY"
                }

                if stage not in allowed:
                    raise ValueError("Invalid stage")

                with stage_lock:
                    current_stage = stage

                print(f"JARVIS stage: {stage}", flush=True)
                self.send_json(200, {"stage": stage})

            except Exception as error:
                self.send_json(400, {"error": str(error)})

            return

        if self.path != "/speak":
            self.send_error(404, "Unknown endpoint")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))

            if length <= 0 or length > 100000:
                raise ValueError("Invalid request size")

            body = json.loads(self.rfile.read(length))
            text = str(body.get("text", "")).strip()

            if not text:
                raise ValueError("No text supplied")

            if len(text) > 3000:
                raise ValueError("Text is too long")

            print(f"JARVIS speaking: {text[:100]}", flush=True)

            with speak_lock:
                temp_path = None

                try:
                    with tempfile.NamedTemporaryFile(
                        suffix=".wav",
                        delete=False
                    ) as temp_file:
                        temp_path = temp_file.name

                    subprocess.run(
                        [
                            "say",
                            "-v", VOICE,
                            "-r", SPEECH_RATE,
                            "-o", temp_path,
                            "--file-format=WAVE",
                            "--data-format=LEI16@24000",
                            text
                        ],
                        check=True,
                        capture_output=True,
                        text=True
                    )

                    with open(temp_path, "rb") as audio_file:
                        audio_bytes = audio_file.read()

                finally:
                    if temp_path and os.path.exists(temp_path):
                        os.remove(temp_path)

            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(audio_bytes)))
            self.send_cors()
            self.end_headers()
            self.wfile.write(audio_bytes)

        except Exception as error:
            print(f"Voice error: {error}", flush=True)
            self.send_json(400, {"error": str(error)})

    def log_message(self, format, *args):
        print("Voice server:", format % args)


server = ThreadingHTTPServer((HOST, PORT), VoiceHandler)

print("JARVIS macOS voice engine ready.", flush=True)
print(f"Voice: {VOICE} | Rate: {SPEECH_RATE}", flush=True)
print(f"Voice server running at http://{HOST}:{PORT}", flush=True)

try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\nStopping JARVIS voice server...")
finally:
    server.server_close()
