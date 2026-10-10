"""A fake Ollama for the tests: an HTTP server in a thread, with the same lines as Ollama 0.32.1.

    with FakeOllama() as fake:
        fake.scenario = "tool"
        ... fake.url, fake.requests

Scenarios: hello (default), tool (asks read_file, then answers with what it
read), loop (asks a tool for ever), slow (one word per half second for a
minute), missing (the model is not there), json (a structured answer).
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ["qwen3.5:4b", "llama3.2:3b"]


class FakeOllama:
    def __init__(self):
        self.scenario = "hello"
        self.requests = []
        self.models = list(MODELS)
        self.stopped_early = threading.Event()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/tags":
                    self._json(200, {"models": [{"name": m, "details": {"parameter_size": "4.7B"}} for m in fake.models]})
                elif self.path == "/api/version":
                    self._json(200, {"version": "0.32.1"})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                if fake.scenario == "missing":
                    self._json(404, {"error": f"model '{body['model']}' not found"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                try:
                    for line in fake.lines(body):
                        self.wfile.write((json.dumps(line, ensure_ascii=False) + "\n").encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    fake.stopped_early.set()  # the hub closed the stream: a stop

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def lines(self, body):
        done = {"done": True, "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 5,
                "message": {"role": "assistant", "content": ""}}
        last = body["messages"][-1]
        if self.scenario == "slow":
            for _ in range(120):
                yield {"message": {"role": "assistant", "content": "mot "}, "done": False}
                time.sleep(0.5)
        elif self.scenario == "loop" or (self.scenario == "tool" and last["role"] != "tool"):
            yield {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "notes.txt"}}}]}, "done": False}
        elif self.scenario == "tool":
            yield {"message": {"role": "assistant", "content": f"Le fichier dit : {last['content']}"}, "done": False}
        elif self.scenario == "json":
            yield {"message": {"role": "assistant", "content": json.dumps({"summary": "plan", "subtasks": []})},
                   "done": False}
        else:
            yield {"message": {"role": "assistant", "content": "Bon"}, "done": False}
            yield {"message": {"role": "assistant", "content": "jour"}, "done": False}
        yield done

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
