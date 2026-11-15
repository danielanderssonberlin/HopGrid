"""
HopGrid – gemeinsames Netzwerkprotokoll für Host und Client.

Ablauf einer Verbindung:

    1. Host  -> Client  (Klartext)  CHALLENGE: Salt, Iterationen, Nonce, Host-Schlüssel (X25519)
    2. Client -> Host   (Klartext)  AUTH_RESPONSE: Benutzername, Client-Schlüssel (X25519),
                                    Beweis = HMAC(PBKDF2(Passwort), "client" + Transkript)
    3. Host  -> Client  (Klartext)  AUTH_RESULT: ok + Gegenbeweis HMAC(..., "host" + Transkript)
    4. Ab hier ist alles mit AES-256-GCM verschlüsselt.

Das Transkript enthält Nonce, beide öffentlichen Schlüssel und den Benutzernamen.
Der Sitzungsschlüssel wird aus dem X25519-Geheimnis UND dem Passwort-Schlüssel abgeleitet:
  - Das Passwort geht nie über das Netz.
  - Wer das Passwort nicht kennt, kann den Datenverkehr weder lesen noch verändern
    (auch nicht als "Mann in der Mitte"), und der Client erkennt einen falschen Host.
  - Jede Verbindung hat neue Schlüssel (Forward Secrecy).
"""

import hashlib
import hmac
import ipaddress
import json
import socket
import struct
import threading
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PROTOCOL_ID = b"HopGrid-v2"
DEFAULT_PORT = 8080

# Suche im LAN per UDP-Broadcast: Client ruft, jeder Host antwortet mit Alias und Port.
# So findet der Client einen Host über seinen Namen, auch wenn sich die IP ändert.
DISCOVERY_PORT = 48080
DISCOVERY_REQUEST = b"HOPGRID-DISCOVER-1"
MAX_ALIAS_LEN = 32

PBKDF2_ITERATIONS = 200_000
MIN_ITERATIONS = 100_000
MAX_ITERATIONS = 2_000_000

# Klartext-Nachrichten (nur während des Logins): 1 Byte Typ + 4 Byte Länge + JSON
PLAIN_HEADER = struct.Struct("!BI")
MSG_CHALLENGE = 1
MSG_AUTH_RESPONSE = 2
MSG_AUTH_RESULT = 3
MAX_PLAIN_MSG = 4096

# Verschlüsselte Nachrichten (nach dem Login)
MSG_FRAME = 10   # Host -> Client: JPEG-Bild
MSG_INPUT = 11   # Client -> Host: JSON-Eingabeereignis

SECURE_HEADER = struct.Struct("!I")
GCM_TAG_SIZE = 16


class ProtocolError(ConnectionError):
    """Unerwartete, fehlerhafte oder manipulierte Daten."""


# ---------------------------------------------------------------------------
# Hilfsfunktionen für Sockets
# ---------------------------------------------------------------------------
def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 1 << 20))
        if not chunk:
            raise ConnectionError("Verbindung von der Gegenseite geschlossen")
        buf += chunk
    return bytes(buf)


def send_plain(sock, msg_type, obj):
    payload = json.dumps(obj).encode("utf-8")
    sock.sendall(PLAIN_HEADER.pack(msg_type, len(payload)) + payload)


def recv_plain(sock, expected_type):
    msg_type, length = PLAIN_HEADER.unpack(recv_exact(sock, PLAIN_HEADER.size))
    if length > MAX_PLAIN_MSG:
        raise ProtocolError("Login-Nachricht zu groß")
    payload = recv_exact(sock, length)
    if msg_type != expected_type:
        raise ProtocolError("Unerwarteter Nachrichtentyp (andere Programmversion?)")
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise ProtocolError("Ungültige Login-Nachricht") from exc
    if not isinstance(data, dict):
        raise ProtocolError("Ungültige Login-Nachricht")
    return data


def hex_field(data, name, length):
    """Liest ein hex-kodiertes Feld fester Länge aus einer Login-Nachricht."""
    try:
        value = bytes.fromhex(str(data[name]))
    except (KeyError, ValueError) as exc:
        raise ProtocolError(f"Feld '{name}' fehlt oder ist ungültig") from exc
    if len(value) != length:
        raise ProtocolError(f"Feld '{name}' hat die falsche Länge")
    return value


# ---------------------------------------------------------------------------
# Kryptografie
# ---------------------------------------------------------------------------
def derive_password_key(password, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def build_transcript(nonce, host_pub, client_pub, username):
    user = username.encode("utf-8")
    return PROTOCOL_ID + nonce + host_pub + client_pub + len(user).to_bytes(2, "big") + user


def make_proof(password_key, role, transcript):
    """role = b'client' oder b'host' – verhindert, dass ein Beweis zurückgespielt wird."""
    return hmac.new(password_key, role + transcript, hashlib.sha256).digest()


class KeyExchange:
    """Kurzlebiges X25519-Schlüsselpaar für genau eine Verbindung."""

    def __init__(self):
        self._private = X25519PrivateKey.generate()
        self.public = self._private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def session_keys(self, peer_public, password_key, transcript):
        """Liefert (Schlüssel Host->Client, Schlüssel Client->Host)."""
        try:
            shared = self._private.exchange(X25519PublicKey.from_public_bytes(peer_public))
        except ValueError as exc:
            raise ProtocolError("Ungültiger Schlüssel der Gegenseite") from exc
        okm = HKDF(
            algorithm=hashes.SHA256(),
            length=64,
            salt=None,
            info=PROTOCOL_ID + b" session " + hashlib.sha256(transcript).digest(),
        ).derive(shared + password_key)
        return okm[:32], okm[32:]


class SecureChannel:
    """Verschlüsselter Kanal über einen TCP-Socket.

    Jede Richtung hat einen eigenen Schlüssel und einen eigenen Zähler als Nonce.
    Dadurch wird keine Nonce doppelt verwendet, und vertauschte, wiederholte oder
    veränderte Nachrichten fallen beim Entschlüsseln auf."""

    def __init__(self, sock, send_key, recv_key, max_recv):
        self.sock = sock
        self._send_aead = AESGCM(send_key)
        self._recv_aead = AESGCM(recv_key)
        self._send_counter = 0
        self._recv_counter = 0
        self._send_lock = threading.Lock()
        self._max_recv = max_recv + 1 + GCM_TAG_SIZE

    def send(self, msg_type, payload):
        with self._send_lock:
            nonce = self._send_counter.to_bytes(12, "big")
            self._send_counter += 1
            ciphertext = self._send_aead.encrypt(nonce, bytes([msg_type]) + payload, None)
            self.sock.sendall(SECURE_HEADER.pack(len(ciphertext)) + ciphertext)

    def recv(self):
        """Nur aus einem Thread aufrufen. Liefert (Typ, Nutzdaten)."""
        (length,) = SECURE_HEADER.unpack(recv_exact(self.sock, SECURE_HEADER.size))
        if not GCM_TAG_SIZE < length <= self._max_recv:
            raise ProtocolError(f"Ungültige Nachrichtengröße ({length} Bytes)")
        ciphertext = recv_exact(self.sock, length)
        nonce = self._recv_counter.to_bytes(12, "big")
        self._recv_counter += 1
        try:
            plaintext = self._recv_aead.decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            raise ProtocolError("Manipulierte oder ungültige Daten empfangen") from exc
        return plaintext[0], plaintext[1:]


# ---------------------------------------------------------------------------
# Netzwerk-Hilfen und Host-Suche im LAN
# ---------------------------------------------------------------------------
def local_ip():
    """Ermittelt die LAN-IP. UDP-connect sendet kein Paket, es wählt nur die Route."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def is_local_address(ip):
    addr = ipaddress.ip_address(ip)
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return addr.is_private or addr.is_loopback or addr.is_link_local


def clean_alias(alias):
    """Alias kürzen und Steuerzeichen entfernen."""
    alias = "".join(ch for ch in str(alias) if ch.isprintable()).strip()
    return alias[:MAX_ALIAS_LEN]


def discovery_reply(alias, port):
    return json.dumps({"hopgrid": 2, "alias": alias, "port": port}).encode("utf-8")


def discover(timeout=1.5):
    """Sucht HopGrid-Hosts im LAN. Liefert [{'alias', 'ip', 'port'}, ...].

    Gesendet wird an 255.255.255.255 und zusätzlich an die /24-Broadcast-Adresse
    des eigenen Netzes (manche Systeme schicken 255.255.255.255 nur über eine
    Schnittstelle). Die Antwort ist nicht authentifiziert. Das ist unkritisch,
    weil beim Login beide Seiten das Passwort beweisen müssen."""
    targets = {"255.255.255.255"}
    own_ip = local_ip()
    if not own_ip.startswith("127."):
        targets.add(own_ip.rsplit(".", 1)[0] + ".255")

    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _attempt in range(2):  # UDP kann verloren gehen
            for target in targets:
                try:
                    sock.sendto(DISCOVERY_REQUEST, (target, DISCOVERY_PORT))
                except OSError:
                    pass
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                break
            except OSError:
                continue
            try:
                info = json.loads(data)
                alias = clean_alias(info["alias"])
                port = int(info["port"])
            except (ValueError, KeyError, TypeError):
                continue
            if info.get("hopgrid") != 2 or not alias or not 1 <= port <= 65535:
                continue
            found[(addr[0], port)] = {"alias": alias, "ip": addr[0], "port": port}
    return sorted(found.values(), key=lambda h: (h["alias"].casefold(), h["ip"]))
