"""Tiny in-memory WebDAV server (PROPFIND/MKCOL/PUT/DELETE/GET) for tests.

Behaves like Synology: MKCOL on existing dir -> 405, MKCOL without parent -> 409,
PUT without parent -> 409, basic auth required.
"""
import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote


class Store:
    def __init__(self, user="u", password="p", prefix=""):
        self.dirs = {""}
        self.files = {}
        self.log = []
        self.user, self.password, self.prefix = user, password, prefix.strip("/")
        self.fail_put = set()  # paths for which PUT returns 500


def make_handler(store):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _path(self):
            p = unquote(self.path.split("?")[0]).strip("/")
            if store.prefix:
                if not (p == store.prefix or p.startswith(store.prefix + "/")):
                    return None
                p = p[len(store.prefix):].strip("/")
            return p

        def _auth(self):
            h = self.headers.get("Authorization", "")
            ok = h == "Basic " + base64.b64encode(
                ("%s:%s" % (store.user, store.password)).encode()).decode()
            if not ok:
                self._send(401)
            return ok

        def _send(self, code, body=b""):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _handle(self):
            body = self._body()
            p = self._path()
            store.log.append((self.command, self.path))
            if not self._auth():
                return
            if p is None:
                return self._send(404)
            parent = p.rpartition("/")[0]
            m = self.command
            if m == "PROPFIND":
                return self._send(207 if (p in store.dirs or p in store.files) else 404,
                                  b"<multistatus/>")
            if m == "MKCOL":
                if p in store.dirs or p in store.files:
                    return self._send(405)
                if parent not in store.dirs:
                    return self._send(409)
                store.dirs.add(p)
                return self._send(201)
            if m == "PUT":
                if p in store.fail_put:
                    return self._send(500, b"disk full")
                if parent not in store.dirs:
                    return self._send(409)
                existed = p in store.files
                store.files[p] = (body, self.headers.get("Content-Type"))
                return self._send(204 if existed else 201)
            if m == "DELETE":
                if p in store.files:
                    del store.files[p]
                    return self._send(204)
                return self._send(404)
            if m == "GET":
                if p in store.files:
                    return self._send(200, store.files[p][0])
                return self._send(404)
            self._send(405)

        do_PROPFIND = do_MKCOL = do_PUT = do_DELETE = do_GET = _handle

    return H


def serve(store, port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


if __name__ == "__main__":
    import sys
    s = Store(prefix=sys.argv[2] if len(sys.argv) > 2 else "")
    srv = serve(s, int(sys.argv[1]))
    print("listening", srv.server_address, flush=True)
    threading.Event().wait()
