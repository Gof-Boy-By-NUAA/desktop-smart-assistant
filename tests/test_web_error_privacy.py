"""异常页面不能公开堆栈和局部变量；在独立进程验证默认配置。"""

import os
import subprocess
import sys
from pathlib import Path


def test_web_errors_use_generic_response(tmp_path):
    script = tmp_path / "error_probe.py"
    script.write_text('''
import threading
from http.client import HTTPConnection
from wsgiref.simple_server import make_server
import web
from channel.web import web_channel

class FailureHandler:
    def GET(self):
        synthetic_secret = "AUDIT_ERROR_LOCAL_SECRET_2719"
        raise RuntimeError("injected error for privacy verification")

application = web.application(("/probe", "FailureHandler"), globals(), autoreload=False)
server = make_server("127.0.0.1", 0, application.wsgifunc())
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
try:
    connection.request("GET", "/probe")
    response = connection.getresponse()
    body = response.read()
    assert response.status == 500
    assert b"AUDIT_ERROR_LOCAL_SECRET_2719" not in body, "response disclosed locals"
    assert body == b"internal server error", "response was not generic"
finally:
    connection.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
''', encoding="utf-8")
    env = os.environ.copy()
    env["COW_DATA_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, "-X", "utf8", str(script)],
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr[-1500:]
