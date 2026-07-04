from http.server import SimpleHTTPRequestHandler, HTTPServer
import os
import json
import uuid

PORT = 7001
OUTPUT_DIR = "/app/output"
CHARTS_DIR = "/app/analytics-charts"


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        p = path.split("?")[0]
        if p.startswith("/files/"):
            return os.path.join(OUTPUT_DIR, p[len("/files/"):])
        if p.startswith("/charts/"):
            return os.path.join(CHARTS_DIR, p[len("/charts/"):])
        return super().translate_path(path)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if self.path == "/save-chart":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            html = body.get("html", "")
            if not html:
                self._json(400, {"error": "html field is required"})
                return
            os.makedirs(CHARTS_DIR, exist_ok=True)
            chart_id = uuid.uuid4().hex[:12]
            filename = f"chart-{chart_id}.html"
            with open(os.path.join(CHARTS_DIR, filename), "w", encoding="utf-8") as f:
                f.write(html)
            self._json(200, {"url": f"/charts/{filename}"})
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, status, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # suppress per-request stdout noise


server = HTTPServer(("0.0.0.0", PORT), Handler)
print(f"Serving on http://localhost:{PORT}  — /files/  /charts/  POST /save-chart")
server.serve_forever()
