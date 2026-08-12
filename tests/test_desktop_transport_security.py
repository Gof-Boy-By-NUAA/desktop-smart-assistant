import io
import os
from datetime import timedelta

import pytest

from channel.web.desktop_protocol import (
    DesktopProtocolError,
    DESKTOP_TLS_CERT_VALIDITY_DAYS,
    DesktopRequestAuthMiddleware,
    HEADER_LAUNCH_ID,
    HEADER_MAC,
    HEADER_NONCE,
    HEADER_TIMESTAMP,
    create_ephemeral_tls_material,
    make_bootstrap_frame,
    make_ready_frame,
    read_bootstrap_credentials,
    read_control_frame,
    sign_request,
    verify_bootstrap_frame,
    verify_ready_frame,
    write_control_frame,
)


LAUNCH_ID = "desktop_launch_id_123456"
SECRET = b"s" * 32
NONCE = "nonce_1234567890"
NOW = 1_700_000_000


def _app(environ, start_response):
    start_response("200 OK", [("Content-Type", "application/octet-stream")])
    return [environ["wsgi.input"].read()]


def _request(body=b"payload", method="POST", path="/api/chat", query="a=1", timestamp=NOW, nonce=NONCE):
    mac = sign_request(method, path, query, body, timestamp, nonce, LAUNCH_ID, SECRET)
    return {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "REQUEST_URI": path + ("?" + query if query else ""),
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
        HEADER_LAUNCH_ID: LAUNCH_ID,
        HEADER_TIMESTAMP: str(timestamp),
        HEADER_NONCE: nonce,
        HEADER_MAC: mac,
    }


def _call(middleware, environ):
    response = {}

    def start_response(status, headers):
        response["status"] = status
        response["headers"] = headers

    response["body"] = b"".join(middleware(environ, start_response))
    return response


def _middleware():
    return DesktopRequestAuthMiddleware(_app, LAUNCH_ID, SECRET, clock=lambda: NOW)


def test_valid_request_is_authenticated_and_body_is_preserved_for_downstream_app():
    result = _call(_middleware(), _request(b'{"message":"hello"}'))

    assert result["status"] == "200 OK"
    assert result["body"] == b'{"message":"hello"}'


@pytest.mark.parametrize("field,value", [("REQUEST_METHOD", "GET"), ("REQUEST_URI", "/api/other?a=1")])
def test_tampered_method_or_path_is_rejected(field, value):
    environ = _request()
    environ[field] = value

    result = _call(_middleware(), environ)

    assert result["status"] == "401 Unauthorized"


def test_raw_ascii_percent_encoded_origin_form_is_authenticated_without_signing_path_info():
    path = "/preview/%E4%B8%AD%E6%96%87%20file.txt"
    query = "name=%E4%B8%AD%E6%96%87%20file.txt&download=1"
    environ = _request(path=path, query=query, nonce="nonce_raw_origin_1")
    # Cheroot presents this decoded value to the application; only REQUEST_URI
    # is the signing source, so this mismatch must not reject a valid request.
    environ["PATH_INFO"] = "/preview/中文 file.txt"

    assert _call(_middleware(), environ)["status"] == "200 OK"

    tampered = _request(path=path, query=query, nonce="nonce_raw_origin_2")
    tampered["REQUEST_URI"] = "/preview/%E4%B8%AD%E6%96%87%20other.txt?" + query
    assert _call(_middleware(), tampered)["status"] == "401 Unauthorized"


@pytest.mark.parametrize("target", ["/api/%", "/api/%GG", "/api/%FF", "https://localhost/api/health", "/api/health#fragment"])
def test_missing_or_invalid_raw_request_uri_is_rejected_fail_closed(target):
    environ = _request(nonce="nonce_invalid_uri_" + str(len(target)))
    environ["REQUEST_URI"] = target
    assert _call(_middleware(), environ)["status"] == "401 Unauthorized"

    missing = _request(nonce="nonce_missing_uri_123")
    del missing["REQUEST_URI"]
    assert _call(_middleware(), missing)["status"] == "401 Unauthorized"


def test_tampered_body_and_stale_timestamp_are_rejected():
    changed_body = _request(b"original")
    changed_body["wsgi.input"] = io.BytesIO(b"altered!")
    result = _call(_middleware(), changed_body)
    assert result["status"] == "401 Unauthorized"

    stale = _request(timestamp=NOW - 31, nonce="nonce_stale_12345")
    result = _call(_middleware(), stale)
    assert result["status"] == "401 Unauthorized"


def test_replay_is_rejected_after_a_valid_request():
    middleware = _middleware()
    first = _call(middleware, _request())
    second = _call(middleware, _request())

    assert first["status"] == "200 OK"
    assert second["status"] == "401 Unauthorized"


def test_malformed_or_length_mismatched_request_is_rejected_fail_closed():
    missing = _request()
    del missing[HEADER_MAC]
    assert _call(_middleware(), missing)["status"] == "401 Unauthorized"

    chunked = _request()
    chunked["HTTP_TRANSFER_ENCODING"] = "chunked"
    assert _call(_middleware(), chunked)["status"] == "401 Unauthorized"

    too_long = _request(b"body")
    too_long["CONTENT_LENGTH"] = "3"
    assert _call(_middleware(), too_long)["status"] == "401 Unauthorized"


def test_control_pipe_round_trip_and_ready_ack_proof():
    read_fd, write_fd = os.pipe()
    try:
        write_control_frame(write_fd, make_bootstrap_frame(LAUNCH_ID, SECRET))
        bootstrap = read_control_frame(read_fd)
        assert verify_bootstrap_frame(bootstrap, LAUNCH_ID, SECRET) is True
    finally:
        os.close(read_fd)
        os.close(write_fd)

    ready = make_ready_frame(
        LAUNCH_ID,
        SECRET,
        4444,
        "-----BEGIN CERTIFICATE-----\n" + "A" * 64 + "\n-----END CERTIFICATE-----\n",
    )
    assert verify_ready_frame(ready, LAUNCH_ID, SECRET) is True
    ready["proof"] = "0" * 64
    with pytest.raises(DesktopProtocolError):
        verify_ready_frame(ready, LAUNCH_ID, SECRET)


def test_bootstrap_credentials_are_available_only_after_valid_framing():
    launch_id, secret = read_bootstrap_credentials(make_bootstrap_frame(LAUNCH_ID, SECRET))
    assert launch_id == LAUNCH_ID
    assert secret == SECRET


def test_invalid_control_frames_are_rejected():
    with pytest.raises(DesktopProtocolError):
        read_control_frame(io.BytesIO(b"\\x00\\x00\\x10\\x01"))

    noncanonical = b'{"launch_id": "desktop_launch_id_123456", "secret": "c3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3M", "type": "bootstrap"}'
    raw = len(noncanonical).to_bytes(4, "big") + noncanonical
    with pytest.raises(DesktopProtocolError):
        read_control_frame(io.BytesIO(raw))


def test_desktop_tls_certificate_validity_covers_supported_continuous_runtime():
    assert DESKTOP_TLS_CERT_VALIDITY_DAYS >= 365
    material = create_ephemeral_tls_material(LAUNCH_ID)
    try:
        from cryptography import x509

        certificate = x509.load_pem_x509_certificate(material.certificate_pem.encode("ascii"))
        assert certificate.not_valid_after_utc - certificate.not_valid_before_utc >= timedelta(
            days=DESKTOP_TLS_CERT_VALIDITY_DAYS
        )
    finally:
        material.cleanup()
