"""Private, fail-closed transport authentication primitives for COW_DESKTOP.

This module deliberately has no dependency on :mod:`channel.web.web_channel`.
Callers opt in by wrapping their WSGI application with
``DesktopRequestAuthMiddleware`` and by using the control-pipe helpers during
desktop process startup.
"""

import base64
import hashlib
import hmac
import io
import json
import os
import re
import struct
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from tempfile import mkstemp


MAX_CONTROL_FRAME_BYTES = 4096
# Desktop uploads are authenticated before parsing.  Keep the pre-dispatch
# buffer deliberately bounded but high enough for the existing file/knowledge
# workflows; callers may choose a lower limit for a constrained deployment.
MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024
DEFAULT_AUTH_TTL_SECONDS = 30
DEFAULT_REPLAY_CACHE_ENTRIES = 10000
# This leaf is pinned and delivered only over fd 3, so it is not a public Web
# PKI certificate.  It must nevertheless outlive the supported continuous
# desktop runtime; a one-day validity caused long-running desktops to lose
# their private transport while their child was otherwise healthy.
DESKTOP_TLS_CERT_VALIDITY_DAYS = 397
MAX_ORIGIN_FORM_BYTES = 8192

CONTROL_BOOTSTRAP = "bootstrap"
CONTROL_READY = "ready"

HEADER_LAUNCH_ID = "HTTP_X_COW_DESKTOP_LAUNCH_ID"
HEADER_TIMESTAMP = "HTTP_X_COW_DESKTOP_TIMESTAMP"
HEADER_NONCE = "HTTP_X_COW_DESKTOP_NONCE"
HEADER_MAC = "HTTP_X_COW_DESKTOP_MAC"
AUTH_HEADER_NAMES = frozenset(
    (HEADER_LAUNCH_ID, HEADER_TIMESTAMP, HEADER_NONCE, HEADER_MAC)
)

_LAUNCH_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]{0,15})\Z")


class DesktopProtocolError(ValueError):
    """Raised for malformed or unauthenticated desktop protocol input."""


def _validate_secret(secret):
    if not isinstance(secret, bytes) or len(secret) != 32:
        raise ValueError("desktop transport secret must be exactly 32 bytes")


def _validate_launch_id(launch_id):
    if not isinstance(launch_id, str) or not _LAUNCH_ID_RE.fullmatch(launch_id):
        raise DesktopProtocolError("invalid launch_id")


def _canonical_json(value):
    """Return the unique UTF-8 representation signed by this protocol."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise DesktopProtocolError("value cannot be canonically encoded") from exc
    return encoded


def _json_object_no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise DesktopProtocolError("duplicate JSON field")
        value[key] = item
    return value


def _decode_control_frame(raw):
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_json_object_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, DesktopProtocolError) as exc:
        raise DesktopProtocolError("invalid control JSON") from exc
    if not isinstance(value, dict):
        raise DesktopProtocolError("control frame must be a JSON object")
    if _canonical_json(value) != raw:
        raise DesktopProtocolError("control frame is not canonical JSON")
    _validate_control_message(value)
    return value


def _validate_control_message(message):
    if not isinstance(message, dict) or set(message) - {
        "type",
        "launch_id",
        "secret",
        "port",
        "certificate",
        "proof",
    }:
        raise DesktopProtocolError("invalid control frame schema")
    frame_type = message.get("type")
    if frame_type == CONTROL_BOOTSTRAP:
        if set(message) != {"type", "launch_id", "secret"}:
            raise DesktopProtocolError("invalid bootstrap schema")
        _validate_launch_id(message["launch_id"])
        if not isinstance(message["secret"], str):
            raise DesktopProtocolError("invalid bootstrap secret")
        try:
            secret = base64.urlsafe_b64decode(message["secret"] + "==")
        except (ValueError, UnicodeEncodeError) as exc:
            raise DesktopProtocolError("invalid bootstrap secret") from exc
        if len(secret) != 32 or _encode_secret(secret) != message["secret"]:
            raise DesktopProtocolError("invalid bootstrap secret")
    elif frame_type == CONTROL_READY:
        if set(message) != {"type", "launch_id", "port", "certificate", "proof"}:
            raise DesktopProtocolError("invalid ready schema")
        _validate_launch_id(message["launch_id"])
        if (
            not isinstance(message["port"], int)
            or isinstance(message["port"], bool)
            or not 1 <= message["port"] <= 65535
        ):
            raise DesktopProtocolError("invalid ready port")
        if (
            not isinstance(message["certificate"], str)
            or len(message["certificate"]) < 64
            or len(message["certificate"]) > MAX_CONTROL_FRAME_BYTES
            or not message["certificate"].startswith("-----BEGIN CERTIFICATE-----")
            or not message["certificate"].rstrip().endswith("-----END CERTIFICATE-----")
        ):
            raise DesktopProtocolError("invalid ready certificate")
        if not isinstance(message["proof"], str) or not _HEX_RE.fullmatch(message["proof"]):
            raise DesktopProtocolError("invalid ready proof")
    else:
        raise DesktopProtocolError("unknown control frame type")


def _encode_secret(secret):
    return base64.urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")


def write_control_frame(stream, message):
    """Write one canonical, length-prefixed control frame to a binary stream."""
    _validate_control_message(message)
    payload = _canonical_json(message)
    if not payload or len(payload) > MAX_CONTROL_FRAME_BYTES:
        raise DesktopProtocolError("control frame exceeds size limit")
    _write_all(stream, struct.pack("!I", len(payload)) + payload)
    flush = getattr(stream, "flush", None)
    if flush is not None:
        flush()


def _write_all(stream, data):
    offset = 0
    while offset < len(data):
        if isinstance(stream, int):
            written = os.write(stream, data[offset:])
        else:
            written = stream.write(data[offset:])
            # Buffered binary file objects may use None to mean the full buffer
            # was accepted.
            if written is None:
                written = len(data) - offset
        if not isinstance(written, int) or written <= 0:
            raise DesktopProtocolError("failed to write control frame")
        offset += written


def _read_exact(stream, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = os.read(stream, remaining) if isinstance(stream, int) else stream.read(remaining)
        if not chunk:
            raise DesktopProtocolError("truncated control frame")
        if not isinstance(chunk, bytes):
            raise DesktopProtocolError("control stream must return bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_control_frame(stream):
    """Read one bounded canonical JSON frame from a binary control pipe."""
    length = struct.unpack("!I", _read_exact(stream, 4))[0]
    if not 1 <= length <= MAX_CONTROL_FRAME_BYTES:
        raise DesktopProtocolError("invalid control frame length")
    return _decode_control_frame(_read_exact(stream, length))


def make_bootstrap_frame(launch_id, secret):
    """Build the only accepted parent-to-child startup message."""
    _validate_launch_id(launch_id)
    _validate_secret(secret)
    return {"type": CONTROL_BOOTSTRAP, "launch_id": launch_id, "secret": _encode_secret(secret)}


def verify_bootstrap_frame(message, expected_launch_id, expected_secret):
    """Verify the startup capability using constant-time secret comparison."""
    _validate_control_message(message)
    _validate_launch_id(expected_launch_id)
    _validate_secret(expected_secret)
    if message["type"] != CONTROL_BOOTSTRAP:
        raise DesktopProtocolError("expected bootstrap frame")
    supplied_secret = base64.urlsafe_b64decode(message["secret"] + "==")
    if not hmac.compare_digest(message["launch_id"], expected_launch_id) or not hmac.compare_digest(
        supplied_secret, expected_secret
    ):
        raise DesktopProtocolError("bootstrap verification failed")
    return True


def read_bootstrap_credentials(message):
    """Extract an authenticated launch identity from a structurally valid frame.

    The inherited control pipe itself is the parent/child capability.  A child
    cannot know the secret before it reads this one frame, so this function
    intentionally validates framing/schema rather than pretending to verify a
    secret the child has not yet received.
    """
    _validate_control_message(message)
    if message["type"] != CONTROL_BOOTSTRAP:
        raise DesktopProtocolError("expected bootstrap frame")
    return message["launch_id"], base64.urlsafe_b64decode(message["secret"] + "==")


def _ready_proof(launch_id, secret, port, certificate):
    signed = _canonical_json(
        {
            "certificate_sha256": hashlib.sha256(certificate.encode("utf-8")).hexdigest(),
            "launch_id": launch_id,
            "port": port,
            "type": CONTROL_READY,
        }
    )
    return hmac.new(secret, signed, hashlib.sha256).hexdigest()


def make_ready_frame(launch_id, secret, port, certificate):
    """Create a readiness acknowledgement bound to port and TLS certificate."""
    _validate_launch_id(launch_id)
    _validate_secret(secret)
    message = {
        "type": CONTROL_READY,
        "launch_id": launch_id,
        "port": port,
        "certificate": certificate,
        "proof": _ready_proof(launch_id, secret, port, certificate),
    }
    _validate_control_message(message)
    return message


def verify_ready_frame(message, expected_launch_id, expected_secret):
    """Verify that a ready acknowledgement proves possession of the bootstrap secret."""
    _validate_control_message(message)
    _validate_launch_id(expected_launch_id)
    _validate_secret(expected_secret)
    if message["type"] != CONTROL_READY:
        raise DesktopProtocolError("expected ready frame")
    expected_proof = _ready_proof(
        expected_launch_id,
        expected_secret,
        message["port"],
        message["certificate"],
    )
    if not hmac.compare_digest(message["launch_id"], expected_launch_id) or not hmac.compare_digest(
        message["proof"], expected_proof
    ):
        raise DesktopProtocolError("ready proof verification failed")
    return True


@dataclass(frozen=True)
class EphemeralTLSMaterial:
    """Short-lived PEM files used only while Cheroot loads an SSLContext."""

    certificate_pem: str
    certificate_path: str
    private_key_path: str

    def cleanup(self):
        for path in (self.certificate_path, self.private_key_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def create_ephemeral_tls_material(launch_id):
    """Create a one-launch self-signed TLS leaf for the private control channel.

    The certificate is delivered to Electron exclusively over the inherited
    control pipe and pinned by its SHA-256 identity.  Private key material is
    written to owner-only temporary files solely because the standard Cheroot
    adapter requires file paths; callers must delete both paths immediately
    after constructing the adapter/SSL context.
    """
    _validate_launch_id(launch_id)
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise DesktopProtocolError("desktop TLS dependency is unavailable") from exc

    now = datetime.now(UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"smart-assistant-desktop-{launch_id}")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=DESKTOP_TLS_CERT_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    private_key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_fd, cert_path = mkstemp(prefix="smart-assistant-desktop-cert-", suffix=".pem")
    key_fd, key_path = mkstemp(prefix="smart-assistant-desktop-key-", suffix=".pem")
    try:
        for fd, data in ((cert_fd, certificate_pem), (key_fd, private_key_pem)):
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                # Windows ACL inheritance owns access control; mkstemp creates a
                # non-shared file and the subsequent close preserves that ACL.
                pass
            _write_all(fd, data)
            os.close(fd)
        return EphemeralTLSMaterial(certificate_pem.decode("ascii"), cert_path, key_path)
    except Exception:
        for fd in (cert_fd, key_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        for path in (cert_path, key_path):
            try:
                os.remove(path)
            except OSError:
                pass
        raise


def _raw_origin_form(path, query):
    """Validate and return the raw ASCII origin-form split into path/query.

    ``PATH_INFO`` is intentionally not involved here: Cheroot decodes it
    before WSGI dispatch, while Electron signs the exact bytes sent on the
    wire.  Keeping this representation ASCII also makes the HMAC transcript
    independent of WSGI's URL decoding policy.
    """
    if not isinstance(path, str) or not isinstance(query, str):
        raise DesktopProtocolError("invalid request target")
    if "?" in path:
        raise DesktopProtocolError("invalid request path")
    target = path + ("?" + query if query else "")
    try:
        raw = target.encode("ascii")
    except UnicodeEncodeError as exc:
        raise DesktopProtocolError("request target must be raw ASCII") from exc
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "#" in target
        or "\\x00" in target
        or not 1 <= len(raw) <= MAX_ORIGIN_FORM_BYTES
    ):
        raise DesktopProtocolError("invalid request target")
    decoded = bytearray()
    index = 0
    while index < len(target):
        char = target[index]
        if ord(char) < 0x21 or ord(char) > 0x7E:
            raise DesktopProtocolError("request target must be raw ASCII")
        if char == "%":
            if index + 2 >= len(target) or not re.fullmatch(r"[0-9A-Fa-f]{2}", target[index + 1:index + 3]):
                raise DesktopProtocolError("invalid percent encoding")
            decoded.append(int(target[index + 1:index + 3], 16))
            index += 3
            continue
        decoded.append(ord(char))
        index += 1
    try:
        # RFC 3986 percent-encoded UTF-8 is the only encoding emitted by the
        # desktop URL producer.  Reject malformed byte sequences rather than
        # allowing a server-specific replacement decode into the transcript.
        decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DesktopProtocolError("invalid percent encoding") from exc
    return path, query


def raw_origin_form_from_environ(environ):
    """Extract Cheroot's trustworthy raw request target, fail-closed.

    Cheroot sets ``REQUEST_URI`` from ``req.uri`` before it decodes
    ``PATH_INFO``.  Other WSGI servers are deliberately rejected in desktop
    mode until they provide an equivalently raw field; reconstructing a target
    from decoded WSGI values would silently change the signed bytes.
    """
    raw_target = environ.get("REQUEST_URI")
    if not isinstance(raw_target, str):
        raise DesktopProtocolError("raw REQUEST_URI is required")
    if raw_target.startswith(("http://", "https://")):
        raise DesktopProtocolError("absolute request target is forbidden")
    path, separator, query = raw_target.partition("?")
    if not separator:
        query = ""
    elif not query:
        # Keep one canonical spelling for an empty query so the exact raw
        # origin-form cannot be changed from "/path" to "/path?" without a
        # different authenticated request target.
        raise DesktopProtocolError("empty query delimiter is forbidden")
    path, query = _raw_origin_form(path, query)
    raw_query = environ.get("QUERY_STRING", "")
    if not isinstance(raw_query, str) or not hmac.compare_digest(raw_query, query):
        raise DesktopProtocolError("raw request target/query mismatch")
    return path, query


def canonical_request_bytes(method, path, query, body, timestamp, nonce, launch_id):
    """Build the complete request transcript covered by the desktop request MAC."""
    if not isinstance(method, str) or not re.fullmatch(r"[A-Z]{1,16}", method):
        raise DesktopProtocolError("invalid request method")
    path, query = _raw_origin_form(path, query)
    if not isinstance(body, bytes):
        raise DesktopProtocolError("request body must be bytes")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0:
        raise DesktopProtocolError("invalid timestamp")
    if not isinstance(nonce, str) or not _NONCE_RE.fullmatch(nonce):
        raise DesktopProtocolError("invalid nonce")
    _validate_launch_id(launch_id)
    return _canonical_json(
        {
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "launch_id": launch_id,
            "method": method,
            "nonce": nonce,
            "path": path,
            "query": query,
            "timestamp": timestamp,
            "version": 1,
        }
    )


def sign_request(method, path, query, body, timestamp, nonce, launch_id, secret):
    """Return the lowercase SHA-256 HMAC used in the four desktop auth headers."""
    _validate_secret(secret)
    return hmac.new(
        secret,
        canonical_request_bytes(method, path, query, body, timestamp, nonce, launch_id),
        hashlib.sha256,
    ).hexdigest()


class TTLReplayCache:
    """Bounded, thread-safe replay cache which fails closed when saturated."""

    def __init__(self, ttl_seconds=DEFAULT_AUTH_TTL_SECONDS, max_entries=DEFAULT_REPLAY_CACHE_ENTRIES):
        if ttl_seconds <= 0 or max_entries <= 0:
            raise ValueError("replay cache bounds must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._entries = {}
        self._lock = threading.Lock()

    def check_and_store(self, launch_id, nonce, now):
        key = (launch_id, nonce)
        with self._lock:
            expired = [entry for entry, expiry in self._entries.items() if expiry <= now]
            for entry in expired:
                del self._entries[entry]
            if key in self._entries or len(self._entries) >= self._max_entries:
                return False
            self._entries[key] = now + self._ttl_seconds
            return True


class DesktopRequestAuthMiddleware:
    """Wrap a WSGI app with replay-resistant private desktop request authentication."""

    def __init__(
        self,
        app,
        launch_id,
        secret,
        ttl_seconds=DEFAULT_AUTH_TTL_SECONDS,
        max_body_bytes=MAX_REQUEST_BODY_BYTES,
        replay_cache=None,
        clock=time.time,
    ):
        _validate_launch_id(launch_id)
        _validate_secret(secret)
        if ttl_seconds <= 0 or max_body_bytes < 0:
            raise ValueError("authentication limits must be positive")
        self.app = app
        self.launch_id = launch_id
        self.secret = secret
        self.ttl_seconds = ttl_seconds
        self.max_body_bytes = max_body_bytes
        self.replay_cache = replay_cache or TTLReplayCache(ttl_seconds=ttl_seconds)
        self.clock = clock

    @staticmethod
    def _deny(start_response):
        start_response("401 Unauthorized", [("Content-Length", "0")])
        return [b""]

    def _header_values(self, environ):
        supplied = {key for key in environ if key.startswith("HTTP_X_COW_DESKTOP_")}
        if supplied != AUTH_HEADER_NAMES:
            raise DesktopProtocolError("missing or extra desktop authentication header")
        values = {key: environ[key] for key in AUTH_HEADER_NAMES}
        if any(not isinstance(value, str) for value in values.values()):
            raise DesktopProtocolError("invalid desktop authentication header")
        return values

    def _read_body(self, environ):
        if environ.get("HTTP_TRANSFER_ENCODING"):
            raise DesktopProtocolError("chunked requests are forbidden")
        content_length = environ.get("CONTENT_LENGTH")
        if not isinstance(content_length, str) or not _DECIMAL_RE.fullmatch(content_length):
            raise DesktopProtocolError("invalid content length")
        declared = int(content_length)
        if declared > self.max_body_bytes:
            raise DesktopProtocolError("request body too large")
        stream = environ.get("wsgi.input")
        if stream is None or not hasattr(stream, "read"):
            raise DesktopProtocolError("missing wsgi input")
        body = stream.read(declared + 1)
        if not isinstance(body, bytes) or len(body) != declared:
            raise DesktopProtocolError("request body length mismatch")
        environ["wsgi.input"] = io.BytesIO(body)
        return body

    def __call__(self, environ, start_response):
        try:
            headers = self._header_values(environ)
            method = environ.get("REQUEST_METHOD")
            path, query = raw_origin_form_from_environ(environ)
            if not isinstance(headers[HEADER_TIMESTAMP], str) or not _DECIMAL_RE.fullmatch(
                headers[HEADER_TIMESTAMP]
            ):
                raise DesktopProtocolError("invalid timestamp")
            timestamp = int(headers[HEADER_TIMESTAMP])
            nonce = headers[HEADER_NONCE]
            launch_id = headers[HEADER_LAUNCH_ID]
            mac = headers[HEADER_MAC]
            if not _NONCE_RE.fullmatch(nonce) or not _HEX_RE.fullmatch(mac):
                raise DesktopProtocolError("invalid request authentication field")
            if not hmac.compare_digest(launch_id, self.launch_id):
                raise DesktopProtocolError("invalid launch id")
            body = self._read_body(environ)
            now = self.clock()
            if not isinstance(now, (int, float)) or abs(now - timestamp) > self.ttl_seconds:
                raise DesktopProtocolError("expired timestamp")
            expected_mac = sign_request(method, path, query, body, timestamp, nonce, launch_id, self.secret)
            if not hmac.compare_digest(mac, expected_mac):
                raise DesktopProtocolError("invalid request MAC")
            if not self.replay_cache.check_and_store(launch_id, nonce, now):
                raise DesktopProtocolError("request replay")
        except (DesktopProtocolError, OSError, TypeError, ValueError, OverflowError):
            return self._deny(start_response)
        return self.app(environ, start_response)
