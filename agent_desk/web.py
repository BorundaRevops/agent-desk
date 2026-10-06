"""Read-only local Desk view and reference library. It accepts no browser writes."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import sys
from urllib.parse import unquote, urlsplit

from .desk import Desk
from . import library

APP_CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
           "connect-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'none'")
DOCUMENT_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
                "base-uri 'none'; form-action 'none'; sandbox")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path in ("/", "/index.html"):
                body = files("agent_desk").joinpath("static/index.html").read_bytes()
                content_type = "text/html; charset=utf-8"
                csp = APP_CSP
            elif path == "/api/snapshot":
                body = json.dumps(Desk().execute("snapshot", {"limit": 1000, "request_history_limit": 1000})).encode()
                content_type = "application/json; charset=utf-8"
                csp = APP_CSP
            elif path == "/api/library":
                body = json.dumps(library.list_documents()).encode()
                content_type = "application/json; charset=utf-8"
                csp = APP_CSP
            elif path.startswith("/library/"):
                relative = unquote(path[len("/library/"):], encoding="utf-8", errors="strict")
                kind, body = library.read_document(relative)
                content_type = "text/html; charset=utf-8" if kind == "html" else "text/plain; charset=utf-8"
                csp = DOCUMENT_CSP
            else:
                self.send_error(404)
                return
        except (FileNotFoundError, UnicodeError):
            self.send_error(404)
            return
        except (OSError, ValueError, json.JSONDecodeError):
            self.send_error(500)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be in 1..65535")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Agent Desk: http://127.0.0.1:" + str(args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
