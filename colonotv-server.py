# ============================================================================
#  Colono TV - Servidor (version PYTHON, sin instalar nada)
# ============================================================================
#
#  COMO ARRANCARLO (con doble clic en colonotv-iniciar.bat, o a mano):
#      python colonotv-server.py
#    Escucha en el puerto 8787. Para cambiar puerto:
#      set PORT=9000
#      python colonotv-server.py
#
#  EN EL BOT (pestana "Colono TV"):
#    Servidor (URL):  http://127.0.0.1:8787     (tu PC)
#                     http://TU-IP-LAN:8787     (para tus amigos en la misma red)
#    Sala:            MI-ALIANZA
#    Contrasena:      la misma para todos (opcional, cifra los datos)
#
#  Los datos se guardan solo en memoria (se borran al reiniciar; se reenvian solos).
# ============================================================================

import hashlib
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

PORT = 8787
HOST = "0.0.0.0"
ROOM_TTL_MS = 3 * 60 * 60 * 1000        # olvida a un jugador si no envia en 3 h
MAX_BODY = 512 * 1024                   # 512 KB por envio
MAX_ROOM_LEN = 80
MAX_SENDER_LEN = 80

try:
    PORT = int(__import__("os").environ.get("PORT") or PORT)
except Exception:
    pass
try:
    HOST = __import__("os").environ.get("HOST") or HOST
except Exception:
    pass

# rooms: { roomName: { senderId: {"data": str, "ts": int} } }
rooms = {}
owners = {}          # roomName -> senderId del creador
owner_names = {}     # roomName -> nombre del creador
room_keys = {}       # roomName -> sha256 del codigo especial (para eliminar)
lock = threading.Lock()


def clean_room_locked(room):
    r = rooms.get(room)
    if not r:
        return
    now = int(time.time() * 1000)
    for sender in list(r.keys()):
        val = r.get(sender)
        if (not val) or (now - val["ts"] > ROOM_TTL_MS):
            r.pop(sender, None)
    if not r:
        rooms.pop(room, None)
        owners.pop(room, None)
        owner_names.pop(room, None)
        room_keys.pop(room, None)


class Handler(BaseHTTPRequestHandler):
    server_version = "ColonoTV/1.0"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _room_from_path(self, path):
        if path.startswith("/room/"):
            raw = path[len("/room/"):]
            room = unquote(raw)
            if not room or len(room) > MAX_ROOM_LEN:
                return None
            return room[:MAX_ROOM_LEN]
        return None

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/health"):
            with lock:
                members = sum(len(r) for r in rooms.values())
                self._json(200, {
                    "ok": True, "service": "colonotv",
                    "rooms": len(rooms), "members": members,
                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
            return
        room = self._room_from_path(path)
        if room is None:
            self._json(404, {"ok": False, "error": "ruta no encontrada"})
            return
        with lock:
            clean_room_locked(room)
            r = rooms.get(room, {})
            senders = [{"sender": s, "data": v["data"], "ts": v["ts"]} for s, v in r.items()]
            owner = owners.get(room, "")
            owner_name = owner_names.get(room, "")
        self._json(200, {"ok": True, "room": room, "count": len(senders), "owner": owner, "ownerName": owner_name, "senders": senders})

    def do_POST(self):
        path = urlparse(self.path).path
        room = self._room_from_path(path)
        if room is None:
            self._json(404, {"ok": False, "error": "ruta no encontrada"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._json(413, {"ok": False, "error": "tamano invalido"})
            return
        raw = b""
        try:
            raw = self.rfile.read(length)
        except Exception:
            raw = b""
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = None
        if not isinstance(data, dict):
            self._json(400, {"ok": False, "error": "json invalido"})
            return
        sender = str(data.get("sender") or "")[:MAX_SENDER_LEN]
        if not sender:
            self._json(400, {"ok": False, "error": "falta sender"})
            return
        name = str(data.get("name") or "")[:MAX_SENDER_LEN]
        key = data.get("key")
        payload = data.get("data")
        if not isinstance(payload, str):
            payload = json.dumps(payload or {})
        with lock:
            r = rooms.setdefault(room, {})
            if room not in owners:
                owners[room] = sender
                owner_names[room] = name
            if isinstance(key, str) and key.strip() and owners.get(room) == sender:
                room_keys[room] = hashlib.sha256(key.strip().encode("utf-8")).hexdigest()
            r[sender] = {"data": payload, "ts": int(time.time() * 1000)}
            members = len(r)
        self._json(200, {"ok": True, "room": room, "members": members})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        room = self._room_from_path(parsed.path)
        if room is None:
            self._json(404, {"ok": False, "error": "ruta no encontrada"})
            return
        q = parse_qs(parsed.query)
        sender = str((q.get("sender") or [""])[0])[:MAX_SENDER_LEN]
        key = str((q.get("key") or [""])[0])
        if not sender or not key:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if 0 < length <= MAX_BODY:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    if isinstance(body, dict):
                        sender = sender or str(body.get("sender") or "")[:MAX_SENDER_LEN]
                        key = key or str(body.get("key") or "")
            except Exception:
                pass
        with lock:
            if room not in rooms:
                self._json(404, {"ok": False, "error": "la sala no existe"})
                return
            stored = room_keys.get(room)
            if stored:
                provided = hashlib.sha256((key or "").encode("utf-8")).hexdigest()
                if provided != stored:
                    self._json(403, {"ok": False, "error": "codigo especial incorrecto"})
                    return
            else:
                owner = owners.get(room)
                if owner and sender != owner:
                    self._json(403, {"ok": False, "error": "solo el creador puede eliminar la sala"})
                    return
            rooms.pop(room, None)
            owners.pop(room, None)
            owner_names.pop(room, None)
            room_keys.pop(room, None)
        self._json(200, {"ok": True, "deleted": room})

    def log_message(self, fmt, *args):
        try:
            sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), (fmt % args)))
        except Exception:
            pass


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("=" * 64)
    print(" Colono TV - servidor iniciado")
    print("=" * 64)
    print(" En tu PC (bot):        http://127.0.0.1:%d" % PORT)
    print(" Para tus amigos (LAN): http://%s:%d" % (lan_ip(), PORT))
    print(" Estado:                http://127.0.0.1:%d/" % PORT)
    print(" Sala de ejemplo:       http://127.0.0.1:%d/room/MI-ALIANZA" % PORT)
    print(" Deja esta ventana ABIERTA mientras juegas. Ctrl+C para parar.")
    print("=" * 64)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")


if __name__ == "__main__":
    main()
