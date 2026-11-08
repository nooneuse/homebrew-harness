"""Local GitHub and notification protocol fixture for managed run tests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, urlunsplit


class RunServiceFixture:
    def __init__(self, head):
        self.head, self.comments, self.reviews, self.checks = head, [], [], []
        self.notifications, self.requests = [], []
        self.fail_page, self.notification_status, self.drop_notification = (
            None,
            200,
            False,
        )
        self.pages = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def emit(self, value, status=200, headers=None):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, item in (headers or {}).items():
                    self.send_header(key, item)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                uri = urlsplit(self.path)
                query = parse_qs(uri.query)
                page = int(query.get("page", ["1"])[0])
                outer.requests.append(
                    {
                        "method": "GET",
                        "path": uri.path,
                        "page": page,
                        "query": query,
                        "headers": dict(self.headers),
                    }
                )
                if outer.fail_page == (uri.path.rsplit("/", 1)[-1], page):
                    self.emit({"error": "fixture failure"}, 503)
                elif uri.path.endswith("/pulls/1"):
                    self.emit({"head": {"sha": outer.head}})
                elif uri.path.endswith("/comments"):
                    if outer.pages is not None:
                        data = outer.pages.get(page, [])
                        headers = (
                            {"Link": '<next>; rel="next"'}
                            if page + 1 in outer.pages
                            else {}
                        )
                    else:
                        since = query.get("since", [""])[0]
                        data = [
                            row for row in outer.comments if row["updated_at"] > since
                        ]
                        headers = {}
                    self.emit(data, headers=headers)
                elif uri.path.endswith("/reviews"):
                    self.emit(outer.reviews)
                elif uri.path.endswith("/check-runs"):
                    self.emit({"check_runs": outer.checks})
                else:
                    self.emit({"error": "unknown fixture route"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.notifications.append(
                    {"path": self.path, "payload": body, "headers": dict(self.headers)}
                )
                if outer.drop_notification:
                    self.close_connection = True
                    return
                self.emit({}, outer.notification_status)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = urlunsplit(
            ("http", "127.0.0.1:" + str(self.server.server_port), "/", "", "")
        )

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
