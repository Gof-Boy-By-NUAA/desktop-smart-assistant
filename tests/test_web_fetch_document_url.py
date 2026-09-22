"""无扩展名文档地址必须按原始 URL 下载，不得改写服务端路由。"""

import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pypdf import PdfWriter

from agent.tools.web_fetch.web_fetch import WebFetch


def test_content_type_document_preserves_url_and_query(tmp_path, monkeypatch):
    payload = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(payload)
    body = payload.getvalue()
    requested = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            if self.path != "/report?id=42":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    monkeypatch.setenv("WEB_SECURITY_SSRF_PROTECTION", "false")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = WebFetch({"cwd": str(tmp_path)}).execute(
            {"url": f"http://127.0.0.1:{server.server_port}/report?id=42"}
        )
        assert result.status == "success", result.result
        assert requested and all(url == "/report?id=42" for url in requested)
        assert list((tmp_path / "tmp").glob("*.pdf"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
