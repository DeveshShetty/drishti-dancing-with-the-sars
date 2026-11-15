"""Serve Drishti (the viewer) at http://localhost:8000 and tell the browser not to cache, so edits show up on a normal reload.

    cd viewer
    ..\\.nisar\\Scripts\\python.exe serve.py          (optional: a port number, e.g. serve.py 8001)
"""
import http.server
import os
import sys


class NoCache(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):      # quiet: errors only
        if len(args) > 1 and str(args[1])[:1] in "45":
            super().log_message(fmt, *args)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f"Drishti: http://localhost:{port}   (Ctrl+C to stop)")
    http.server.ThreadingHTTPServer(("", port), NoCache).serve_forever()
