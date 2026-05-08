#!/usr/bin/env python3
# Tiny HTTP responder used by the proxy-plugin case as a TCP backend.
#
# socat's SYSTEM addresses use commas as option separators which mangles
# HTTP headers, so the responder lives in its own script. http.server is
# the right size for the test.
import http.server
import socketserver
import sys


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Directory listing\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


# SO_REUSEADDR so a TIME_WAIT 4-tuple from a prior run doesn't block
# the new bind. The proxy worker dials this backend; on case teardown
# the connection goes to TIME_WAIT for ~60s, which without REUSEADDR
# blocks the next run from binding.
class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


port = int(sys.argv[1])
with ReusableTCPServer(("127.0.0.1", port), H) as srv:
    srv.serve_forever()
