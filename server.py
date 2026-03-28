from __future__ import annotations

from http.server import ThreadingHTTPServer

from app.web import AddRefHandler


def main() -> None:
    host = "0.0.0.0"
    port = 14785
    server = ThreadingHTTPServer((host, port), AddRefHandler)
    print(f"AddRef server listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
