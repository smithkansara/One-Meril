from http.server import SimpleHTTPRequestHandler, HTTPServer
import os

PORT = 8000
OUTPUT_DIR = "/app/output"

class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        if path.startswith("/files/"):
            return os.path.join(OUTPUT_DIR, path[len("/files/"):]) #coverted into files into output folder
        return super().translate_path(path) 

server = HTTPServer(("0.0.0.0", PORT), Handler)
print(f"Serving output folder at http://localhost:{PORT}/files/")
server.serve_forever()