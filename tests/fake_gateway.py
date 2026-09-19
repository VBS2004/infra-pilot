"""A stdlib OpenAI-compatible gateway for offline end-to-end tests.

Serves /v1/models, /v1/chat/completions, /v1/embeddings and /v1/rerank on a
random localhost port. Chat replies come from a script callable so each test
decides what the "model" says; every request is recorded on `.requests`.

    gw = FakeGateway(script=lambda kind, messages: "inputs = {}")
    gw.start()          # gw.base -> http://127.0.0.1:<port>
    ...
    gw.stop()

`kind` passed to the script is one of:
    "intent"    - the NL -> JSON intent parser call
    "edit"      - the LLM edit-planner call (returns JSON text)
    "generate"  - whole-file generation (returns HCL text)
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def classify(messages):
    system = (messages[0].get("content") or "") if messages else ""
    if "STRICT JSON" in system and "intent" in system:
        return "intent"
    if "edit planner" in system:
        return "edit"
    return "generate"


class FakeGateway:
    def __init__(self, script=None, model_id="fake-model"):
        self.script = script or (lambda kind, messages: "")
        self.model_id = model_id
        self.requests = []          # [{"method","path","body"}]
        self._server = None
        self._thread = None
        self.base = ""

    # ---- introspection helpers ----
    def chat_calls(self, kind=None):
        out = []
        for r in self.requests:
            if r["path"].endswith("/chat/completions"):
                k = classify(r["body"].get("messages", []))
                if kind is None or k == kind:
                    out.append(r)
        return out

    def paths(self):
        return [r["path"] for r in self.requests]

    # ---- lifecycle ----
    def start(self):
        gw = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload):
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                gw.requests.append({"method": "GET", "path": self.path, "body": {}})
                if self.path.endswith("/models"):
                    return self._send(200, {"data": [{"id": gw.model_id}]})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                gw.requests.append({"method": "POST", "path": self.path, "body": body,
                                    "auth": self.headers.get("Authorization")})
                if self.path.endswith("/chat/completions"):
                    msgs = body.get("messages", [])
                    try:
                        text = gw.script(classify(msgs), msgs)
                    except Exception as e:  # surface script bugs as a 500
                        return self._send(500, {"error": repr(e)})
                    if isinstance(text, (dict, list)):
                        text = json.dumps(text)
                    return self._send(200, {"choices": [{"message": {"role": "assistant",
                                                                     "content": text}}]})
                if self.path.endswith("/embeddings"):
                    data = [{"index": i, "embedding": [float(len(t) % 7), 1.0, 0.5]}
                            for i, t in enumerate(body.get("input", []))]
                    return self._send(200, {"data": data})
                if self.path.endswith("/rerank"):
                    docs = body.get("documents", [])
                    q = set((body.get("query") or "").lower().split())
                    res = [{"index": i, "relevance_score":
                            float(len(q & set(str(d).lower().split())))}
                           for i, d in enumerate(docs)]
                    return self._send(200, {"results": res})
                self._send(404, {"error": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
