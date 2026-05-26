# -*- coding: utf-8 -*-
import os
import json
import numpy as np
import sounddevice as sd
import serial
import time
import crcmod
from flask import Flask, render_template, request, jsonify, Response
import threading
import platform
import collections
import queue
import re as _re

default_port = "/dev/ttyUSB0" if platform.system() == "Linux" else "COM3"

DEFAULT_CONFIG = {
    "callsign": "F4XXX-9",
    "serial_port": default_port,
    "audio_device_tx": None,
    "audio_device_rx": None,
    "baud": 1200,
    "tx_delay_ms": 300,
    "ptt_delay_ms": 250,
    "ptt_mode": "RTS",
    "volume": 0.5,
    "path": "WIDE1-1,WIDE2-1",
    "station_comment": "",
    "station_status": "",
    "symbol_table": "/",
    "symbol_code": "[",
    "maidenhead": "",
    "geo_mode": "locator",
    "lat_manual": "",
    "lon_manual": "",
    "beacon_interval": 0,
    "beacon_type": "station",
    "beacon_schedules": {},
    # ── iGate APRS-IS ────────────────────────────────────────────────────────
    "igate_enabled":  False,
    "igate_server":   "rotate.aprs2.net",
    "igate_port":     14580,
    "igate_passcode": "-1",
    "igate_filter":   "r/46.5/1.5/200",   # filtre région Centre-Val de Loire
    "igate_rx_only":  True,                # True = RX-iGate seulement, False = TX aussi
    # ── Alertes passage ISS ──────────────────────────────────────────────────
    "iss_alert": {
        "enabled":      False,
        "advance_min":  10,    # minutes avant le passage pour envoyer l'alerte
    },
    # ── Synchronisation Wavelog ──────────────────────────────────────────────
    "wavelog": {
        "enabled":       False,
        "url":           "",          # ex: https://monwavelog.example.com
        "api_key":       "",          # clé API Wavelog (Paramètres → API)
        "station_id":    1,           # ID station Wavelog (défaut 1)
        "sync_rx":       True,        # synchroniser les trames RX reçues
        "sync_tx":       True,        # synchroniser les trames TX émises
        "sync_interval": 5,           # minutes entre chaque synchro automatique
        "only_qso":      True,        # ne synchroniser que les messages/QSO (pas tous les beacons)
        "last_sync_id":  0,           # dernier id de logbook synchronisé
    },
}

app = Flask(__name__)

# ── Version applicative ──────────────────────────────────────────────────────
APP_VERSION = "2.2"
APP_VERSION_DATE = "2025-05-25"
APP_CHANGELOG = [
    {"version": "2.2", "date": "2025-05-25", "label": "current", "changes": [
        "Liens QRZ.com sur tous les indicatifs (console, QSO, MAP, stats)",
        "Navigation mobile : barre fixe en bas d'écran (bottom nav)",
        "Viewport mobile-first, touch targets 44 px, zoom iOS désactivé",
        "Carte MAP hauteur dynamique sur smartphone",
        "Suppression du bloc Alerte Proximité",
        "Section Aide : versioning et changelog intégrés",
    ]},
    {"version": "2.1", "date": "2025-04-10", "label": "", "changes": [
        "iGate APRS-IS : RX-iGate et Full-iGate avec reconnexion automatique",
        "Alertes météo : 6 types (température, vent, rafales, pluie, pression, WMO)",
        "Widget Propagation VHF : SFI / Kp / A-index NOAA en temps réel",
        "Passages ISS : widget compact dans TRAFIC + alerte avant passage",
        "Web Push Notifications (Notification API, sans VAPID)",
        "Statistiques 24 h avec top 10 stations (onglet STATS)",
        "Persistance stats via /stats/save et /stats/load",
    ]},
    {"version": "2.0", "date": "2025-02-20", "label": "", "changes": [
        "Architecture Flask + Direwolf KISS TCP (remplacement sounddevice/serial)",
        "Carte MAP Leaflet avec marqueurs PHG, vitesse, altitude",
        "Chat QSO persistant (chat.json) avec ACK automatique",
        "Beacons automatiques : Station, ISS, Météo, Propagation",
        "Onglet ISS avec OrbTrack iframe",
        "Décodage Mic-E, PHG, RNG, positions compressées",
        "Interface Tailwind CSS dark, responsive desktop",
    ]},
    {"version": "1.x", "date": "2024", "label": "", "changes": [
        "Version initiale : modem AFSK 1200 baud pur Python",
        "Émission/réception via sounddevice + PTT série RTS/DTR",
        "Interface HTML basique, console de trames brutes",
    ]},
]

class APRSConfig:
    def __init__(self):
        self.data = DEFAULT_CONFIG.copy()
        self.load()

    def load(self):
        if os.path.exists('config.json'):
            try:
                with open('config.json', 'r') as f:
                    self.data.update(json.load(f))
            except: pass

    def save(self, new_data):
        self.data.update(new_data)
        with open('config.json', 'w') as f:
            json.dump(self.data, f)

config_manager = APRSConfig()

# ── Gestionnaire de conversations APRS ──────────────────────────────────────
CHAT_FILE = "chat.json"

class APRSChat:
    def __init__(self):
        self.conversations = {}
        self.msg_counter   = 1
        self._lock         = threading.Lock()
        self._load()

    # ── Persistance ──────────────────────────────────────────────────────────

    def _load(self):
        """Charge les conversations depuis chat.json au démarrage."""
        if not os.path.exists(CHAT_FILE):
            return
        try:
            with open(CHAT_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.conversations = data.get("conversations", {})
            self.msg_counter   = data.get("msg_counter", 1)
            print("[QSO] %d conversation(s) restaurée(s) depuis %s" % (
                len(self.conversations), CHAT_FILE))
        except Exception as e:
            print("[QSO] Impossible de charger %s : %s" % (CHAT_FILE, e))

    def _save(self):
        """Sauvegarde les conversations dans chat.json (appelé sous self._lock)."""
        try:
            tmp = CHAT_FILE + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({
                    "conversations": self.conversations,
                    "msg_counter":   self.msg_counter,
                }, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CHAT_FILE)   # remplacement atomique
        except Exception as e:
            print("[QSO] Erreur sauvegarde %s : %s" % (CHAT_FILE, e))

    # ── API ───────────────────────────────────────────────────────────────────

    def _next_msgno(self):
        n = self.msg_counter
        self.msg_counter = (self.msg_counter % 99999) + 1
        return str(n)

    def add_incoming(self, src, text, msgno=None):
        with self._lock:
            src = src.upper().split(',')[0]
            if src not in self.conversations:
                self.conversations[src] = []
            self.conversations[src].append({
                "dir": "rx", "from": src, "text": text,
                "ts": time.strftime("%d/%m %H:%M"), "msgno": msgno, "read": False
            })
            self._save()
        return msgno

    def add_outgoing(self, dest, text, msgno=None):
        with self._lock:
            dest = dest.upper()
            if dest not in self.conversations:
                self.conversations[dest] = []
            self.conversations[dest].append({
                "dir": "tx", "from": config_manager.data.get("callsign", "?"),
                "text": text, "ts": time.strftime("%d/%m %H:%M"), "msgno": msgno
            })
            self._save()

    def mark_ack(self, msgno):
        with self._lock:
            changed = False
            for msgs in self.conversations.values():
                for m in msgs:
                    if m.get("msgno") == msgno and m["dir"] == "tx" and not m.get("acked"):
                        m["acked"] = True
                        changed = True
            if changed:
                self._save()

    def get_history(self, callsign):
        with self._lock:
            return list(self.conversations.get(callsign.upper(), []))

    def get_contacts(self):
        with self._lock:
            result = []
            for cs, msgs in self.conversations.items():
                if msgs:
                    last   = msgs[-1]
                    unread = sum(1 for m in msgs if m["dir"] == "rx" and not m.get("read"))
                    result.append({"callsign": cs, "last": last["text"][:40],
                                   "ts": last["ts"], "unread": unread})
            return sorted(result, key=lambda x: x["ts"], reverse=True)

    def mark_read(self, callsign):
        with self._lock:
            changed = any(
                not m.get("read")
                for m in self.conversations.get(callsign.upper(), [])
                if m["dir"] == "rx"
            )
            for m in self.conversations.get(callsign.upper(), []):
                m["read"] = True
            if changed:
                self._save()

chat_manager = APRSChat()


# ── Carnet de Trafic APRS ────────────────────────────────────────────────────
LOGBOOK_FILE = "logbook.json"

class APRSLogbook:
    """
    Carnet de trafic APRS dédié.
    Enregistre automatiquement chaque trame RX/TX avec ses métadonnées.
    Permet le filtrage, la recherche, l'export CSV et ADIF.
    """
    def __init__(self):
        self.entries  = []          # liste de dicts triés par ts desc
        self._lock    = threading.Lock()
        self._counter = 1
        self._load()

    def _load(self):
        if not os.path.exists(LOGBOOK_FILE):
            return
        try:
            with open(LOGBOOK_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.entries  = data.get("entries", [])
            self._counter = data.get("counter", 1)
            print("[LOG] %d entrée(s) chargée(s) depuis %s" % (len(self.entries), LOGBOOK_FILE))
        except Exception as e:
            print("[LOG] Erreur chargement logbook : %s" % e)

    def _save(self):
        try:
            tmp = LOGBOOK_FILE + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({"entries": self.entries, "counter": self._counter},
                          f, ensure_ascii=False, indent=2)
            os.replace(tmp, LOGBOOK_FILE)
        except Exception as e:
            print("[LOG] Erreur sauvegarde logbook : %s" % e)

    def add(self, frame, direction="RX"):
        """Ajoute une entrée dans le carnet depuis une frame APRS."""
        extra = frame.get("extra", {}) or {}
        # Pour les messages TX (QSO envoyés), le callsign pertinent est le
        # destinataire du message (msg_dest) et non l'indicatif propre (src).
        _is_tx_msg = (direction == "TX" and
                      frame.get("aprs_type") == "Message" and
                      extra.get("msg_dest"))
        if _is_tx_msg:
            _callsign = (extra.get("msg_dest", "") or "").upper().strip()
            _dest     = (frame.get("src", "") or "").upper().strip()
        else:
            _callsign = (frame.get("src", "") or "").upper().strip()
            _dest     = (frame.get("dest", "") or "").upper().strip()
        with self._lock:
            entry = {
                "id":         self._counter,
                "ts":         time.strftime("%Y-%m-%d %H:%M:%S"),
                "date":       time.strftime("%Y-%m-%d"),
                "time":       time.strftime("%H:%M:%S"),
                "direction":  direction,
                "callsign":   _callsign,
                "dest":       _dest,
                "path":       frame.get("path", "") or "",
                "aprs_type":  frame.get("aprs_type", "") or "",
                "payload":    (frame.get("payload", "") or "")[:200],
                "comment":    (extra.get("comment", "") or "")[:100],
                "lat":        extra.get("lat"),
                "lon":        extra.get("lon"),
                "speed_kmh":  extra.get("speed_kmh"),
                "alt_m":      extra.get("alt_m"),
                "symbol":     extra.get("symbol", ""),
                "source":     frame.get("_source", "RF"),
                "freq":       "144.800",
                "band":       "2m",
                "mode":       "APRS",
                "note":       "",
            }
            self._counter += 1
            self.entries.insert(0, entry)   # plus récent en premier
            # Limite à 5000 entrées pour ne pas exploser la RAM
            if len(self.entries) > 5000:
                self.entries = self.entries[:5000]
            # Sauvegarde différée toutes les 10 nouvelles entrées
            if self._counter % 10 == 0:
                self._save()
        return entry

    def update_note(self, entry_id, note):
        with self._lock:
            for e in self.entries:
                if e["id"] == entry_id:
                    e["note"] = note[:200]
                    self._save()
                    return True
        return False

    def delete(self, entry_id):
        with self._lock:
            before = len(self.entries)
            self.entries = [e for e in self.entries if e["id"] != entry_id]
            if len(self.entries) < before:
                self._save()
                return True
        return False

    def clear_all(self):
        with self._lock:
            self.entries = []
            self._save()

    def get_entries(self, page=1, per_page=50, search="", direction="", aprs_type=""):
        with self._lock:
            result = list(self.entries)
        # Filtres
        if search:
            s = search.upper()
            result = [e for e in result if
                      s in e.get("callsign","").upper() or
                      s in e.get("dest","").upper() or
                      s in e.get("comment","").upper() or
                      s in e.get("payload","").upper()]
        if direction:
            result = [e for e in result if e.get("direction","") == direction]
        if aprs_type:
            result = [e for e in result if aprs_type.lower() in e.get("aprs_type","").lower()]
        total = len(result)
        start = (page - 1) * per_page
        return result[start:start+per_page], total

    def export_csv(self, search="", direction="", aprs_type=""):
        """Génère le contenu CSV du carnet."""
        import io, csv as _csv
        entries, _ = self.get_entries(page=1, per_page=99999,
                                      search=search, direction=direction, aprs_type=aprs_type)
        buf = io.StringIO()
        fieldnames = ["id","date","time","direction","callsign","dest","path",
                      "aprs_type","comment","lat","lon","speed_kmh","alt_m",
                      "symbol","source","freq","band","mode","note","payload"]
        w = _csv.DictWriter(buf, fieldnames=fieldnames, extrasaction='ignore')
        w.writeheader()
        for e in entries:
            w.writerow({k: e.get(k,"") for k in fieldnames})
        return buf.getvalue()

    def export_adif(self, search="", direction="", aprs_type=""):
        """Génère un fichier ADIF (Amateur Data Interchange Format) standard."""
        entries, _ = self.get_entries(page=1, per_page=99999,
                                      search=search, direction=direction, aprs_type=aprs_type)
        lines = [
            "ADIF Export — Py-APRS Logbook",
            "<ADIF_VER:5>3.1.1",
            "<PROGRAMID:8>Py-APRS",
            "<EOH>",
        ]
        my_call = config_manager.data.get("callsign", "N0CALL").upper()
        for e in entries:
            cs   = e.get("callsign","") or ""
            if not cs or cs in ("APRS","BEACON","CQ","?"):
                continue
            date_adif = (e.get("date","") or "").replace("-","")
            time_adif = (e.get("time","") or "").replace(":","")[:6]
            band      = e.get("band","2m") or "2m"
            mode      = e.get("mode","APRS") or "APRS"
            freq      = e.get("freq","144.800") or "144.800"
            comment   = e.get("comment","") or ""
            note      = e.get("note","") or ""
            rst       = "599"
            fields = [
                "<CALL:%d>%s" % (len(cs), cs),
                "<QSO_DATE:%d>%s" % (len(date_adif), date_adif),
                "<TIME_ON:%d>%s" % (len(time_adif), time_adif),
                "<BAND:%d>%s" % (len(band), band),
                "<FREQ:%d>%s" % (len(freq), freq),
                "<MODE:%d>%s" % (len(mode), mode),
                "<RST_SENT:%d>%s" % (len(rst), rst),
                "<RST_RCVD:%d>%s" % (len(rst), rst),
                "<STATION_CALLSIGN:%d>%s" % (len(my_call), my_call),
            ]
            if comment:
                fields.append("<COMMENT:%d>%s" % (len(comment), comment))
            if note:
                fields.append("<NOTES:%d>%s" % (len(note), note))
            fields.append("<EOR>")
            lines.append(" ".join(fields))
        return "\n".join(lines)

    def get_stats(self):
        """Statistiques rapides du carnet."""
        with self._lock:
            entries = list(self.entries)
        total   = len(entries)
        rx      = sum(1 for e in entries if e.get("direction") == "RX")
        tx      = sum(1 for e in entries if e.get("direction") == "TX")
        calls   = {e["callsign"] for e in entries if e.get("callsign") and e["callsign"] not in ("","APRS","BEACON","CQ","?")}
        types   = {}
        for e in entries:
            t = e.get("aprs_type","?") or "?"
            types[t] = types.get(t, 0) + 1
        top_type = sorted(types.items(), key=lambda x: -x[1])[:5]
        return {
            "total":      total,
            "rx":         rx,
            "tx":         tx,
            "unique_calls": len(calls),
            "top_types":  top_type,
        }


logbook = APRSLogbook()


def _clean_comment(s):
    """Nettoie un commentaire APRS brut :
    - supprime les caractères de contrôle et substituts (·) résiduels
    - supprime les caractères non imprimables Latin-1 (0x80-0x9F)
    - supprime les marqueurs Mic-E résiduels (}, |, ~)
    - normalise les espaces multiples
    """
    if not s:
        return ""
    out = []
    for c in s:
        cp = ord(c)
        if cp == 0x7D or cp == 0x7C or cp == 0x7E:
            continue
        if cp < 32 or (0x80 <= cp <= 0x9F) or c == '·':
            continue
        out.append(c)
    return ' '.join(''.join(out).split())


# Table puissance PHG : P² watts (0=0W, 1=1W, 2=4W, 3=9W… 9=81W)
_PHG_POWER  = [0, 1, 4, 9, 16, 25, 36, 49, 64, 81]
# Hauteur PHG : 10×2^h pieds
_PHG_HEIGHT = [10, 20, 40, 80, 160, 320, 640, 1280, 2560, 5120]
# Directivité PHG : 0=omni, 1=NE, 2=E, 3=SE, 4=S, 5=SW, 6=W, 7=NW, 8=N
_PHG_DIR    = ['Omni','NE','E','SE','S','SO','O','NO','N']

def _extract_phg(comment):
    """Extrait et retire PHG/RNG du commentaire APRS.
    Retourne (commentaire_nettoyé, dict_phg_ou_None).
    PHG format : PHGphgd  (4 chiffres après PHG)
    RNG format : RNGrrrr  (rayon en miles)
    """
    import re as _re2
    phg = {}

    # PHG : 4 chiffres encodant puissance/hauteur/gain/direction
    m = _re2.search(r'PHG(\d)(\d)(\d)(\d)', comment)
    if m:
        p, h, g, d = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        phg['phg_power_w']  = _PHG_POWER[p]  if p < len(_PHG_POWER)  else p*p
        phg['phg_height_ft']= _PHG_HEIGHT[h] if h < len(_PHG_HEIGHT) else 10*(2**h)
        phg['phg_height_m'] = round(phg['phg_height_ft'] * 0.3048)
        phg['phg_gain_db']  = g
        phg['phg_dir']      = _PHG_DIR[d] if d < len(_PHG_DIR) else str(d)
        comment = _re2.sub(r'PHG\d{4}', '', comment).strip()

    # RNG : rayon de portée radio en miles
    m = _re2.search(r'RNG(\d{4})', comment)
    if m:
        phg['rng_miles'] = int(m.group(1))
        phg['rng_km']    = round(int(m.group(1)) * 1.609)
        comment = _re2.sub(r'RNG\d{4}', '', comment).strip()

    return (' '.join(comment.split()), phg if phg else None)


class APRSModem:
    def __init__(self, cfg):
        self.cfg = cfg
        self.crc_func = crcmod.predefined.mkCrcFun('x-25')
        self.ser = None
        self.rx_buffer = collections.deque(maxlen=100)
        self.is_rx_running = False
        self.tx_lock = threading.Lock()
        self.init_hardware()

    def init_hardware(self):
        """Aucun port serie a ouvrir — Dire Wolf gere la PTT via direwolf.conf."""
        self.ser = None
        print("[HW] init_hardware : PTT geree par Dire Wolf (cf. direwolf.conf)")

    # Dernière erreur TX (exposée par /rx_test)
    tx_last_error = ""
    tx_last_ok    = ""

    # ── Socket TX KISS persistante ────────────────────────────────────────────
    _tx_sock       = None
    _tx_sock_lock  = threading.Lock()

    @classmethod
    def _get_tx_sock(cls):
        """
        Retourne la socket TX KISS persistante, en créant ou reconnectant si nécessaire.
        Appelé sous tx_lock — pas besoin de verrouillage supplémentaire.
        """
        import socket as _sock
        # Tester si la socket existante est encore vivante
        if cls._tx_sock is not None:
            try:
                # MSG_DONTWAIT + MSG_PEEK : lève une exception si la connexion est morte
                cls._tx_sock.recv(1, _sock.MSG_DONTWAIT | _sock.MSG_PEEK)
            except BlockingIOError:
                pass   # Rien à lire mais socket vivante
            except Exception:
                # Socket morte — fermer proprement
                try: cls._tx_sock.shutdown(_sock.SHUT_RDWR)
                except: pass
                try: cls._tx_sock.close()
                except: pass
                cls._tx_sock = None

        if cls._tx_sock is None:
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            s.setsockopt(_sock.SOL_SOCKET,  _sock.SO_REUSEADDR, 1)
            s.setsockopt(_sock.SOL_SOCKET,  _sock.SO_KEEPALIVE, 1)
            # TCP keepalive : 30s idle, 5s intervalle, 3 probes avant drop
            try:
                s.setsockopt(_sock.IPPROTO_TCP, _sock.TCP_KEEPIDLE,  30)
                s.setsockopt(_sock.IPPROTO_TCP, _sock.TCP_KEEPINTVL,  5)
                s.setsockopt(_sock.IPPROTO_TCP, _sock.TCP_KEEPCNT,    3)
            except AttributeError:
                pass  # Windows ne supporte pas ces options
            s.settimeout(3)
            s.connect((cls.DIREWOLF_HOST, cls.DIREWOLF_KISS_PORT))
            s.settimeout(None)
            cls._tx_sock = s
            print("[TX] Nouvelle connexion KISS persistante établie")
        return cls._tx_sock

    def send_packet(self, dest, payload, custom_path=None):
        """
        Envoie une trame APRS via Dire Wolf (port KISS TCP).
        Utilise une connexion TCP persistante — pas de reconnexion à chaque trame.
        """
        import socket as _sock
        with self.tx_lock:
            # ── Encodage AX.25 (sans CRC — Dire Wolf l'ajoute) ───────────────
            def encode_call(call, last=False):
                parts = call.upper().split('-')
                base  = parts[0].ljust(6)
                ssid  = int(parts[1]) if len(parts) > 1 else 0
                res   = [(ord(c) << 1) for c in base]
                res.append((ssid << 1) | (0x61 if last else 0x60))
                return res

            source    = self.cfg.get('callsign', 'N0CALL')
            path_str  = custom_path if custom_path else self.cfg.get('path', 'WIDE1-1,WIDE2-1')
            path_list = [p.strip() for p in path_str.split(',') if p.strip()]

            frame  = encode_call(dest)
            frame += encode_call(source, last=(len(path_list) == 0))
            for i, digi in enumerate(path_list):
                frame += encode_call(digi, last=(i == len(path_list) - 1))
            frame += [0x03, 0xF0]
            frame += [ord(c) for c in payload]

            ax25 = bytes(frame)

            # ── Encapsulation KISS ────────────────────────────────────────────
            FEND  = 0xC0
            FESC  = 0xDB
            TFEND = 0xDC
            TFESC = 0xDD

            def kiss_escape(data):
                out = bytearray()
                for b in data:
                    if b == FEND:   out += bytes([FESC, TFEND])
                    elif b == FESC: out += bytes([FESC, TFESC])
                    else:           out.append(b)
                return bytes(out)

            kiss_frame = bytes([FEND, 0x00]) + kiss_escape(ax25) + bytes([FEND])

            print("[TX] -> dest=%s payload=%s (%d octets AX.25)" % (
                dest, payload[:50], len(ax25)))

            # ── Envoi via connexion persistante — 1 retry si socket morte ────
            for attempt in range(2):
                try:
                    s = APRSModem._get_tx_sock()
                    s.settimeout(4)
                    s.sendall(kiss_frame)
                    s.settimeout(None)
                    APRSModem.tx_last_ok    = time.strftime("%H:%M:%S")
                    APRSModem.tx_last_error = ""
                    print("[TX] Trame KISS envoyée OK (%d octets)" % len(kiss_frame))
                    break
                except Exception as e:
                    # Forcer la fermeture pour provoquer une reconnexion au prochain appel
                    try: APRSModem._tx_sock.close()
                    except: pass
                    APRSModem._tx_sock = None
                    if attempt == 0:
                        print("[TX] Socket morte — reconnexion...")
                    else:
                        APRSModem.tx_last_error = str(e)
                        print("[TX] ERREUR envoi KISS : %s" % e)
                        raise

    def start_rx(self):
        if self.is_rx_running: return
        self.is_rx_running = True
        APRSModem.rx_thread_alive = True
        threading.Thread(target=self._rx_loop, daemon=True, name="rx-decoder").start()

    rx_queue       = queue.Queue(maxsize=200)
    rx_energy_ema  = 0.0
    rx_bit_count   = 0
    rx_thread_alive = False

    @staticmethod
    def _parse_callsign(raw7):
        call = "".join(chr(b >> 1) for b in raw7[:6]).strip()
        ssid = (raw7[6] >> 1) & 0x0F
        has_been_repeated = bool(raw7[6] & 0x80)
        last = bool(raw7[6] & 0x01)
        label = "%s-%d" % (call, ssid) if ssid else call
        return label, last, has_been_repeated

    APRS_SYMBOLS = {
        '/!': '🚔 Police', '/$': '📞 Tel', '/\'': '✈️ Avion', '/(': '📱 Mobile',
        '/)': '⭕ Cercle', '/*': '❄️ Neige', '/+': '🏥 Hopital', '/-': '🏠 Maison',
        '/.': '❌ X', '//': '🚗 Voiture', '/0': '⭕ Cercle', '/>': '🚗 Voiture',
        '/?': '❓ Inconnu', '/@': '⛈️ Orage', '/A': '🚑 Ambulance', '/B': '⛵ Bateau',
        '/C': '📡 Antenne', '/E': '✈️ Avion', '/F': '🚒 Pompier', '/H': '🏨 Hotel',
        '/J': '🚲 Velo', '/K': '🏫 Ecole', '/O': '🎈 Ballon', '/P': '🚓 Police',
        '/R': '📡 Repeteur', '/S': '🚢 Bateau', '/T': '🚛 Camion', '/U': '🚌 Bus',
        '/V': '🚐 Van', '/W': '💧 Eau', '/X': '🚁 Helico', '/Y': '⛵ Voilier',
        '/[': '🚶 Pieton', '/^': '✈️ Avion', '/_': '🌦️ Meteo', '/a': '🚑 Ambulance',
        '/b': '🚲 Velo', '/c': '🏙️ Ville', '/d': '🔥 Feu', '/f': '🚒 Pompier',
        '/h': '🏠 Maison', '/j': '🚗 Voiture', '/k': '🚙 4x4', '/l': '📍 Point',
        '/r': '📡 Antenne', '/s': '🚢 Navire', '/t': '🌡️ Thermometre',
        '/u': '🚗 Voiture', '/v': '🚐 Van', '/x': '🚁 Helico',
    }

    @staticmethod
    def _nmea_to_dd(val, hemi):
        try:
            val = float(val)
            deg = int(val / 100)
            minutes = val - deg * 100
            dd = deg + minutes / 60.0
            if hemi in ('S', 'W'):
                dd = -dd
            return round(dd, 6)
        except Exception:
            return None

    @staticmethod
    def _parse_aprs_position(info):
        result = {}
        m = _re.search(r'(\d{4}\.\d+)([NS])(.)(\d{5}\.\d+)([EW])(.)', info)
        if m:
            lat = APRSModem._nmea_to_dd(m.group(1), m.group(2))
            sym_table = m.group(3)
            lon = APRSModem._nmea_to_dd(m.group(4), m.group(5))
            sym_code = m.group(6)
            if lat is not None and lon is not None:
                result['lat'] = lat
                result['lon'] = lon
                sym_key = sym_table + sym_code
                result['symbol'] = APRSModem.APRS_SYMBOLS.get(sym_key, sym_table + sym_code)
                result['symbol_raw'] = sym_key
                after = info[m.end():]
                cse_spd = _re.match(r'^(\d{3})/(\d{3})', after)
                if cse_spd:
                    result['course'] = int(cse_spd.group(1))
                    result['speed_kt'] = int(cse_spd.group(2))
                    result['speed_kmh'] = round(int(cse_spd.group(2)) * 1.852, 1)
                    after = after[7:]
                alt_m = _re.search(r'/A=(\d+)', after)
                if alt_m:
                    result['alt_ft'] = int(alt_m.group(1))
                    result['alt_m']  = round(int(alt_m.group(1)) * 0.3048, 0)
                    after = _re.sub(r'/A=\d+', '', after)
                after_clean = _clean_comment(after)
                after_clean, phg = _extract_phg(after_clean)
                if phg: result.update(phg)
                result['comment'] = after_clean
        elif len(info) >= 13 and info[0] in ('!', '=', '/', '@'):
            offset = 1
            if info[0] in ('/', '@'):
                offset = 8
            compressed = info[offset:offset+12]
            if len(compressed) == 12:
                try:
                    sym_table = compressed[0]
                    lat_c = compressed[1:5]
                    sym_code  = compressed[9]
                    lon_c = compressed[5:9]
                    lat_val = 90 - (ord(lat_c[0])-33)*753571/190000 \
                                 - (ord(lat_c[1])-33)*8281/190000   \
                                 - (ord(lat_c[2])-33)*91/190000     \
                                 - (ord(lat_c[3])-33)/190000
                    lon_val = -180 + (ord(lon_c[0])-33)*753571/190000 \
                                   + (ord(lon_c[1])-33)*8281/190000   \
                                   + (ord(lon_c[2])-33)*91/190000     \
                                   + (ord(lon_c[3])-33)/190000
                    result['lat'] = round(lat_val, 6)
                    result['lon'] = round(lon_val, 6)
                    sym_key = sym_table + sym_code
                    result['symbol'] = APRSModem.APRS_SYMBOLS.get(sym_key, sym_key)
                    result['symbol_raw'] = sym_key
                    result['comment'] = _clean_comment(info[offset+12:])
                except Exception:
                    pass
        return result

    @staticmethod
    def _decode_aprs_payload(info, dest=""):
        if not info:
            return "Payload vide", {}
        dti = info[0]
        extra = {}

        if dti in ('!', '=', '/', '@'):
            if dti == '!':   name = "Position"
            elif dti == '=': name = "Position+Msg"
            elif dti == '@': name = "Position+TS+Msg"
            else:            name = "Position+TS"
            extra = APRSModem._parse_aprs_position(info)
            # Si le symbole est _ (weather station), parser aussi les données météo
            if extra.get('symbol_raw', '').endswith('_'):
                wx = APRSModem._parse_wx_data(extra.get('comment', ''))
                extra.update(wx)
                name = "Meteo"

        elif dti == ':':
            name = "Message"
            if len(info) >= 11 and info[10] == ':':
                extra['msg_dest'] = info[1:10].strip()
                body = info[11:]
                extra['msg_text'] = body
                # ACK ou REJ (pas de texte après)
                ack_match = _re.match(r'^(ack|rej)([A-Za-z0-9]+)', body, _re.IGNORECASE)
                if ack_match:
                    extra['msg_ack']   = ack_match.group(1).upper()
                    extra['msg_ackno'] = ack_match.group(2)
                else:
                    # Numéro de message {alphanum} en fin de texte (ex: {Iy} ou {123})
                    mn = _re.search(r'\{([A-Za-z0-9]+)\}', body)
                    if mn:
                        extra['msg_msgno'] = mn.group(1)
                        extra['msg_text']  = body[:mn.start()].strip()

        elif dti == '>':
            name = "Statut"
            extra['comment'] = _clean_comment(info[1:])

        elif dti == ';':
            name = "Objet"
            if len(info) >= 11:
                extra['obj_name'] = info[1:10].strip()
                # info[10] est '*' (objet actif) ou '_' (objet tué)
                # info[11:] peut commencer par un horodatage DDHHMMz/h (7 chars)
                # suivi de la position, ou directement la position.
                pos_body = info[11:]
                ts_m = _re.match(r'^\d{6}[zZhH/]', pos_body)
                if ts_m:
                    extra['obj_ts'] = pos_body[:7]
                    pos_body = pos_body[7:]
                # Préfixer '!' pour que _parse_aprs_position traite le format non-horodaté
                extra.update(APRSModem._parse_aprs_position('!' + pos_body))

        elif dti in ('`', "'", '"'):
            name = "Mic-E"
            # ── Décodage Mic-E ────────────────────────────────────────────────
            # La position, le cap et la vitesse sont encodés dans le champ DEST
            # AX.25 (6 caractères avant le SSID) + les octets 1-8 du champ info.
            #
            # Référence : APRS Protocol Reference 1.0.1 § 10
            #
            # Chaque caractère du dest encode un chiffre de latitude + 3 bits
            # de message (A/B/C) via la table suivante :
            #   '0'–'9' → chiffre 0–9, bit=0
            #   'A'–'J' → chiffre 0–9, bit=1  (custom msg)
            #   'K','L' → 0 (ambiguïté), bit=1
            #   'P'–'Y' → chiffre 0–9, bit=1  (standard msg)
            #   'Z'     → 0 (ambiguïté), bit=0
            #   'S'     → 0 (ambiguïté)  (Sud si dest[3])
            #   'W'     → Ouest si dest[4]
            try:
                # ── Table de décodage ─────────────────────────────────────────
                _MICE_LAT = {
                    '0':0,'1':1,'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9,
                    'A':0,'B':1,'C':2,'D':3,'E':4,'F':5,'G':6,'H':7,'I':8,'J':9,
                    'K':0,'L':0,
                    'P':0,'Q':1,'R':2,'S':3,'T':4,'U':5,'V':6,'W':7,'X':8,'Y':9,
                    'Z':0,
                }
                _MICE_MSG_BIT = {
                    '0':0,'1':0,'2':0,'3':0,'4':0,'5':0,'6':0,'7':0,'8':0,'9':0,
                    'A':1,'B':1,'C':1,'D':1,'E':1,'F':1,'G':1,'H':1,'I':1,'J':1,
                    'K':1,'L':1,
                    'P':1,'Q':1,'R':1,'S':1,'T':1,'U':1,'V':1,'W':1,'X':1,'Y':1,
                    'Z':0,
                }

                # Dest = 6 caractères (SSID exclu, on ne prend que la base)
                d = dest.split('-')[0].upper()
                if len(d) < 6:
                    raise ValueError("dest trop court")

                # Latitude : 6 chiffres encodés dans dest[0..5]
                lat_digits = [_MICE_LAT.get(c, 0) for c in d[:6]]
                lat_deg  = lat_digits[0] * 10 + lat_digits[1]
                lat_min  = lat_digits[2] * 10 + lat_digits[3] + \
                           (lat_digits[4] * 10 + lat_digits[5]) / 100.0
                lat_dd   = lat_deg + lat_min / 60.0

                # Hémisphère S si dest[3] in 'SLQZ' (bit de signe)
                south = d[3] in ('S','L','Z','K')  # standard : S,L = South
                if south: lat_dd = -lat_dd

                # Longitude : octets info[1..3]
                if len(info) < 4:
                    raise ValueError("info trop court pour longitude")
                lon_raw_deg = ord(info[1]) - 28
                lon_raw_min = ord(info[2]) - 28
                lon_raw_frc = ord(info[3]) - 28
                # Offset d'ambiguïté longitude : dest[4]
                if d[4] in ('P','Q','R','S','T','U','V','W','X','Y'):
                    lon_raw_deg += 100
                if lon_raw_deg >= 180 and lon_raw_deg <= 189:
                    lon_raw_deg -= 80
                elif lon_raw_deg >= 190 and lon_raw_deg <= 199:
                    lon_raw_deg -= 190
                lon_min = lon_raw_min + lon_raw_frc / 100.0
                lon_dd  = lon_raw_deg + lon_min / 60.0
                west = d[4] in ('L','W')  # bit Ouest
                if west: lon_dd = -lon_dd

                extra['lat'] = round(lat_dd, 6)
                extra['lon'] = round(lon_dd, 6)

                # Vitesse et cap : octets info[4..6]
                if len(info) >= 7:
                    sp_raw  = ord(info[4]) - 28
                    dc_raw  = ord(info[5]) - 28
                    se_raw  = ord(info[6]) - 28
                    speed_kt = sp_raw * 10 + dc_raw // 10
                    course   = (dc_raw % 10) * 100 + se_raw
                    if speed_kt >= 800: speed_kt -= 800
                    if course  >= 400: course   -= 400
                    extra['speed_kt']  = speed_kt
                    extra['speed_kmh'] = round(speed_kt * 1.852, 1)
                    extra['course']    = course

                # Symbole : octets info[7] et info[8]
                if len(info) >= 9:
                    sym_code  = info[7]
                    sym_table = info[8]
                    sym_key   = sym_table + sym_code
                    extra['symbol']     = APRSModem.APRS_SYMBOLS.get(sym_key, sym_table + sym_code)
                    extra['symbol_raw'] = sym_key

                # Statut Mic-E (3 bits A/B/C dans dest)
                a = _MICE_MSG_BIT.get(d[0], 0)
                b = _MICE_MSG_BIT.get(d[1], 0)
                c = _MICE_MSG_BIT.get(d[2], 0)
                _MICE_STATUS = {
                    (1,1,1): "En route",     (1,1,0): "En réunion",
                    (1,0,1): "Inconnu",      (1,0,0): "En route",
                    (0,1,1): "Au repos",     (0,1,0): "Urgence APRS !",
                    (0,0,1): "Priorité",     (0,0,0): "Urgence",
                }
                extra['mice_status'] = _MICE_STATUS.get((a, b, c), "")

                # Commentaire texte libre (après les 9 octets de données)
                # Certains émetteurs (Kenwood, Yaesu) insèrent un octet optionnel
                # de type radio après le symbole (info[9]) : souvent '}' + modèle.
                # On le saute pour ne garder que le texte lisible.
                tail = info[9:] if len(info) > 9 else ""
                # Sauter l'octet de type radio (0x1D..0x1F ou '}' = 0x7D)
                if tail and (ord(tail[0]) < 32 or tail[0] == '}'):
                    tail = tail[1:]
                tail = _clean_comment(tail)
                # Extraire PHG/RNG du commentaire si présent
                tail, phg = _extract_phg(tail)
                if phg: extra.update(phg)
                if tail:
                    extra['comment'] = tail

            except Exception as _mic_err:
                # En cas d'échec du décodage, garder le payload brut
                extra['comment'] = _clean_comment(info[1:])
                print("[MIC-E] Erreur décodage : %s" % _mic_err)

        elif dti == '$':
            name = "NMEA"
            extra['comment'] = _clean_comment(info[:40])

        elif dti == 'T':
            name = "Telemetrie"
            # Format APRS télémétrie : T#SSS,AAA,BBB,CCC,DDD,EEE,DDDDDDDD
            #   SSS         = numéro de séquence (0-999 ou MIC)
            #   AAA-EEE     = 5 canaux analogiques bruts (0-255)
            #   DDDDDDDD    = 8 bits numériques (0/1)
            rest = info[1:]  # retirer 'T'
            if rest.startswith('#'):
                rest = rest[1:]  # retirer '#'
            parts = rest.split(',')
            try:
                extra['telem_seq'] = parts[0] if parts else '?'
                analog_raw = []
                for i in range(1, min(6, len(parts))):
                    try:    analog_raw.append(float(parts[i]))
                    except: analog_raw.append(None)
                extra['telem_analog']     = analog_raw
                extra['telem_analog_raw'] = list(analog_raw)
                if len(parts) >= 7:
                    extra['telem_bits'] = parts[6].strip()
            except Exception:
                pass
            # rawLine désactivé pour ce type (redondant avec les badges)

        elif dti == '_':
            name = "Meteo"
            extra.update(APRSModem._parse_wx_data(info[1:]))

        elif dti == '#':
            name = "Meteo Peet"
            extra['comment'] = _clean_comment(info[1:])

        else:
            name = "Type %s" % dti
            extra['comment'] = _clean_comment(info[1:40])

        return name, extra

    @staticmethod
    def _parse_wx_data(raw):
        """
        Parse les champs météo APRS depuis la partie data (après position+symbole ou DTI _).
        Format APRS : cCSE/SPDgGGGtTTTrRRRpPPPhHHbBBBBBL...
          c = wind direction (deg)
          s = sustained wind speed (knots)
          g = wind gust (knots)
          t = temperature (°F), peut être négatif ("-12")
          r = rain last hour (1/100 inch)
          p = rain last 24h (1/100 inch)
          P = rain since midnight (1/100 inch)
          h = humidity (%, 00 = 100%)
          b = barometric pressure (1/10 mbar)
          L = luminosity (W/m²) < 1000
          l = luminosity (W/m²) >= 1000
          s (après b/h) = snowfall (inch/24h)
        """
        wx = {}
        raw = raw.strip()

        def _int(s):
            try:
                v = int(s)
                return None if s.strip('.') == '.' * len(s.strip()) else v
            except Exception:
                return None

        # Vent : cDDD/SSS ou cDDD/SSS
        m = _re.search(r'c(\d{3}|\.{3})/s(\d{3}|\.{3})', raw, _re.IGNORECASE)
        if m:
            wd = _int(m.group(1)); ws = _int(m.group(2))
            if wd is not None: wx['wind_dir']     = wd
            if ws is not None:
                wx['wind_speed_kt']  = ws
                wx['wind_speed_kmh'] = round(ws * 1.852, 1)
                wx['wind_speed_ms']  = round(ws * 0.514444, 1)

        # Rafale
        m = _re.search(r'g(\d{3}|\.{3})', raw, _re.IGNORECASE)
        if m:
            v = _int(m.group(1))
            if v is not None:
                wx['gust_kt']  = v
                wx['gust_kmh'] = round(v * 1.852, 1)
                wx['gust_ms']  = round(v * 0.514444, 1)

        # Température °F → °C
        m = _re.search(r't(-?\d{2,3}|\.{3})', raw, _re.IGNORECASE)
        if m:
            v = _int(m.group(1))
            if v is not None:
                wx['temp_f'] = v
                wx['temp_c'] = round((v - 32) * 5 / 9, 1)

        # Pluie dernière heure
        m = _re.search(r'r(\d{3}|\.{3})', raw)
        if m:
            v = _int(m.group(1))
            if v is not None:
                wx['rain_1h_in']  = v / 100.0
                wx['rain_1h_mm']  = round(v * 0.254, 2)

        # Pluie 24h
        m = _re.search(r'p(\d{3}|\.{3})', raw)
        if m:
            v = _int(m.group(1))
            if v is not None:
                wx['rain_24h_in'] = v / 100.0
                wx['rain_24h_mm'] = round(v * 0.254, 2)

        # Pluie depuis minuit
        m = _re.search(r'P(\d{3}|\.{3})', raw)
        if m:
            v = _int(m.group(1))
            if v is not None:
                wx['rain_midnight_mm'] = round(v * 0.254, 2)

        # Humidité
        m = _re.search(r'h(\d{2})', raw, _re.IGNORECASE)
        if m:
            v = _int(m.group(1))
            if v is not None:
                wx['humidity_pct'] = 100 if v == 0 else v

        # Pression (1/10 mbar → hPa)
        m = _re.search(r'b(\d{5})', raw, _re.IGNORECASE)
        if m:
            v = _int(m.group(1))
            if v is not None:
                wx['pressure_hpa'] = round(v / 10.0, 1)

        # Luminosité
        m = _re.search(r'[Ll](\d{3})', raw)
        if m:
            v = _int(m.group(1))
            if v is not None:
                wx['luminosity_wm2'] = v + (1000 if raw[m.start()] == 'l' else 0)

        # Neige
        m = _re.search(r's(\d{3})', raw)
        if m and 'wind_speed_kt' not in wx:  # éviter confusion avec vitesse vent
            v = _int(m.group(1))
            if v is not None:
                wx['snow_24h_in'] = v / 10.0
                wx['snow_24h_cm'] = round(v * 0.254, 1)

        # Commentaire texte (après les champs numériques)
        comment = _re.sub(
            r'c[\d.]{3}/s[\d.]{3}|g[\d.]{3}|t-?[\d.]{2,3}|r[\d.]{3}|'
            r'p[\d.]{3}|P[\d.]{3}|h[\d.]{2}|b[\d.]{5}|[Ll][\d.]{3}',
            '', raw
        ).strip(' _/')
        if comment:
            wx['comment'] = comment

        # Description lisible
        parts = []
        if 'temp_c'       in wx: parts.append("%.1f°C" % wx['temp_c'])
        if 'humidity_pct' in wx: parts.append("%d%% HR" % wx['humidity_pct'])
        if 'wind_speed_kmh' in wx:
            _wd = wx.get('wind_dir', 0) or 0
            _cardinals = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
                          "S","SSO","SO","OSO","O","ONO","NO","NNO"]
            _card = _cardinals[int((_wd + 11.25) / 22.5) % 16]
            parts.append("Vent %s %.1f km/h" % (_card, wx['wind_speed_kmh']))
        elif 'wind_speed_ms' in wx:
            _wd = wx.get('wind_dir', 0) or 0
            _cardinals = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
                          "S","SSO","SO","OSO","O","ONO","NO","NNO"]
            _card = _cardinals[int((_wd + 11.25) / 22.5) % 16]
            parts.append("Vent %s %.1f km/h" % (_card, wx['wind_speed_ms'] * 3.6))
        if 'gust_kmh'  in wx: parts.append("Rafales %.1f km/h" % wx['gust_kmh'])
        elif 'gust_ms' in wx: parts.append("Rafales %.1f km/h" % (wx['gust_ms'] * 3.6))
        if 'rain_1h_mm'   in wx and wx['rain_1h_mm'] > 0:
            parts.append("Pluie 1h: %.1f mm" % wx['rain_1h_mm'])
        if 'pressure_hpa' in wx: parts.append("%.1f hPa" % wx['pressure_hpa'])
        if parts:
            wx['wx_summary'] = ' | '.join(parts)

        return wx

    def _decode_ax25_frame(self, frame_bytes):
        if len(frame_bytes) < 16:
            return None
        try:
            data_for_crc = frame_bytes[:-2]
            recv_crc = frame_bytes[-2] | (frame_bytes[-1] << 8)
            calc_crc = self.crc_func(bytes(data_for_crc))
            if recv_crc != calc_crc:
                return None
            pos = 0
            dest, _, _ = self._parse_callsign(frame_bytes[pos:pos+7]); pos += 7
            src, last, _ = self._parse_callsign(frame_bytes[pos:pos+7]); pos += 7
            digis = []
            while not last and pos + 7 <= len(frame_bytes) - 4:
                digi, last, repeated = self._parse_callsign(frame_bytes[pos:pos+7])
                digis.append(digi + ("*" if repeated else ""))
                pos += 7
            ctrl = frame_bytes[pos]; pos += 1
            pid  = frame_bytes[pos]; pos += 1
            if ctrl != 0x03 or pid != 0xF0:
                return None
            info_bytes = frame_bytes[pos:-2]
            # APRS utilise ISO-8859-1 (Latin-1) — décoder proprement
            # puis remplacer les caractères de contrôle non imprimables (sauf \t)
            info = info_bytes.decode('latin-1', errors='replace')
            info = "".join(c if (c == '\t' or ord(c) >= 32) else '·' for c in info)
            aprs_type, extra = self._decode_aprs_payload(info, dest)
            return {
                "src": src, "dest": dest, "path": ",".join(digis),
                "aprs_type": aprs_type, "payload": info, "extra": extra
            }
        except Exception:
            return None

    # ── Dire Wolf ────────────────────────────────────────────────────────────
    # Port KISS TCP exposé par direwolf (configurable dans direwolf.conf)
    DIREWOLF_HOST = "127.0.0.1"
    DIREWOLF_KISS_PORT = 8001   # port KISS TCP par défaut de Dire Wolf
    DIREWOLF_AGW_PORT  = 8000   # port AGW  (non utilisé ici)

    # Journal des dernières lignes stderr de Dire Wolf (diagnostic)
    dw_log_lines    = collections.deque(maxlen=80)
    dw_frames_decoded = 0   # trames signalées par Dire Wolf dans stderr

    def _find_alsa_device_name(self):
        """
        Retourne le nom ALSA à passer à Dire Wolf pour le device RX.
        La config stocke désormais directement "plughw:X,Y" (depuis _enum_audio_devices).
        On gère aussi l'ancien format (index entier PortAudio) pour compatibilité.
        """
        import subprocess as _sp
        dev_cfg = self.cfg.get("audio_device_rx")

        # Cas 1 : non configuré
        if dev_cfg is None or dev_cfg == "":
            print("[RX] Device RX non configure -> default")
            return "default"

        dev_str = str(dev_cfg)

        # Cas 2 : déjà un nom ALSA direct (plughw:X,Y ou hw:X,Y)
        if dev_str.startswith("plughw:") or dev_str.startswith("hw:"):
            print("[RX] Device ALSA direct : %s" % dev_str)
            return dev_str

        # Cas 3 : index entier PortAudio (ancien format) — résoudre vers ALSA
        try:
            idx = int(dev_str)
            devs = sd.query_devices()
            if idx < len(devs):
                pa_name = devs[idx]["name"]
                print("[RX] Résolution index PA %d (%s) -> ALSA" % (idx, pa_name))

                # Tentative via arecord -l
                try:
                    out = _sp.run(["arecord", "-l"], capture_output=True, text=True).stdout
                    keywords = [w for w in _re.split(r"[\s:,\-\(\)]+", pa_name) if len(w) >= 3]
                    for line in out.splitlines():
                        if any(kw.lower() in line.lower() for kw in keywords):
                            m = _re.search(r"card (\d+)[^,]*,\s*device (\d+)", line)
                            if m:
                                dev = "plughw:%s,%s" % (m.group(1), m.group(2))
                                print("[RX] Résolu : %s" % dev)
                                return dev
                except Exception:
                    pass

                # Tentative : hw:X,Y dans le nom PA
                m = _re.search(r"hw:(\d+),(\d+)", pa_name)
                if m:
                    dev = "plughw:%s,%s" % (m.group(1), m.group(2))
                    print("[RX] Extrait du nom PA : %s" % dev)
                    return dev

                print("[RX] Passage nom brut PA : %s" % pa_name)
                return pa_name
        except (ValueError, Exception) as ex:
            print("[RX] _find_alsa_device_name : %s" % ex)

        # Cas 4 : string inconnue, on la passe directement
        print("[RX] Device brut : %s" % dev_str)
        return dev_str

    def _write_direwolf_conf(self, alsa_dev, conf_path):
        """
        Genere direwolf.conf pour RX ET TX.
        Dire Wolf gere lui-meme la PTT et l'audio TX via KISS.
        """
        port     = self.cfg.get("serial_port", "").strip()
        ptt_mode = self.cfg.get("ptt_mode", "RTS").upper()
        tx_delay = max(100, int(self.cfg.get("tx_delay_ms", 300)))

        # Ligne PTT : "PTT RTS /dev/ttyUSB0" ou "PTT DTR /dev/ttyUSB0"
        if port and ptt_mode in ("RTS", "DTR"):
            ptt_line = "PTT %s %s" % (ptt_mode, port)
        elif port and ptt_mode == "RTS+DTR":
            ptt_line = "PTT RTS %s" % port   # RTS en priorite, DTR non supporte direct
        else:
            ptt_line = "PTT NONE"

        lines = [
            "# Genere automatiquement par aprs_direwolf.py",
            "ADEVICE %s" % alsa_dev,
            "ACHANNELS 1",
            "CHANNEL 0",
            "MYCALL %s"    % self.cfg.get("callsign", "N0CALL"),
            "MODEM 1200",
            ptt_line,
            "TXDELAY %d"   % tx_delay,
            "KISSPORT %d"  % APRSModem.DIREWOLF_KISS_PORT,
            "AGWPORT %d"   % APRSModem.DIREWOLF_AGW_PORT,
        ]
        with open(conf_path, "w") as f:
            f.write("\n".join(lines) + "\n")
        print("[DW] direwolf.conf:")
        for l in lines:
            print("[DW]   " + l)

    # ── Décodeur KISS ─────────────────────────────────────────────────────────
    @staticmethod
    def _kiss_unwrap(buf):
        """
        Extrait les trames KISS complètes d'un buffer d'octets.
        Retourne (liste_de_payloads_AX25, reste_non_consommé).
        Protocole KISS : FEND(0xC0) [CMD(1)] DATA FEND
        """
        FEND = 0xC0
        FESC = 0xDB
        TFEND = 0xDC
        TFESC = 0xDD
        frames = []
        while True:
            start = buf.find(FEND)
            if start == -1:
                break
            end = buf.find(FEND, start + 1)
            if end == -1:
                break
            raw = buf[start + 1 : end]
            buf = buf[end:]          # on laisse le FEND final comme début suivant
            if not raw:
                continue
            # Dé-escaper
            unescaped = bytearray()
            i = 0
            while i < len(raw):
                if raw[i] == FESC:
                    i += 1
                    if i < len(raw):
                        if raw[i] == TFEND:
                            unescaped.append(FEND)
                        elif raw[i] == TFESC:
                            unescaped.append(FESC)
                        else:
                            unescaped.append(raw[i])
                else:
                    unescaped.append(raw[i])
                i += 1
            if not unescaped:
                continue
            cmd = unescaped[0]
            if (cmd & 0x0F) == 0:        # canal 0, commande DATA (0x00)
                frames.append(bytes(unescaped[1:]))
        return frames, buf

    def _parse_ax25_kiss(self, ax25_bytes):
        """
        Parse une trame AX.25 reçue via KISS (sans CRC : Dire Wolf l'a déjà retiré).
        Format AX.25 : DEST(7) SRC(7) [DIGI(7)...] CTRL(1) PID(1) INFO(...)
        """
        try:
            data = list(ax25_bytes)
            if len(data) < 16:
                return None
            pos = 0
            dest, _, _ = self._parse_callsign(data[pos:pos+7]); pos += 7
            src, last, _ = self._parse_callsign(data[pos:pos+7]); pos += 7
            digis = []
            while not last and pos + 7 <= len(data) - 2:
                digi, last, repeated = self._parse_callsign(data[pos:pos+7])
                digis.append(digi + ("*" if repeated else ""))
                pos += 7
            if pos + 2 > len(data):
                return None
            ctrl = data[pos]; pos += 1
            pid  = data[pos]; pos += 1
            if ctrl != 0x03 or pid != 0xF0:
                return None
            info_bytes = data[pos:]
            info = bytes(info_bytes).decode('latin-1', errors='replace')
            info = "".join(c if (c == '\t' or ord(c) >= 32) else '·' for c in info)
            aprs_type, extra = APRSModem._decode_aprs_payload(info, dest)
            return {
                "src": src, "dest": dest, "path": ",".join(digis),
                "aprs_type": aprs_type, "payload": info, "extra": extra
            }
        except Exception as ex:
            print("[KISS] Erreur parseur AX.25 : " + str(ex))
            return None

    def _rx_loop(self):
        import subprocess, shutil, socket, os as _os, tempfile

        if not shutil.which('direwolf'):
            print("[RX] direwolf introuvable -- sudo apt install direwolf -y")
            APRSModem.rx_thread_alive = False
            return

        alsa_dev  = self._find_alsa_device_name()
        conf_path = _os.path.join(tempfile.gettempdir(), 'aprs_direwolf.conf')
        self._write_direwolf_conf(alsa_dev, conf_path)
        print("[RX] Démarrage Dire Wolf sur %s (KISS TCP :%d)" % (
            alsa_dev, APRSModem.DIREWOLF_KISS_PORT))

        ALPHA = 0.15

        while self.is_rx_running:
            dw_proc = None
            sock    = None

            try:
                # ── Lance Dire Wolf ───────────────────────────────────────────
                # stdout capturé pour les logs de trames ; stderr pour les erreurs audio
                dw_proc = subprocess.Popen(
                    ['direwolf', '-c', conf_path, '-t', '0'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                # Thread de capture stderr (erreurs audio, init device…)
                def _dw_stderr(proc):
                    for line in proc.stderr:
                        line = line.rstrip()
                        APRSModem.dw_log_lines.append("[ERR] " + line)
                        if line.strip():
                            print("[DW] " + line)
                threading.Thread(target=_dw_stderr, args=(dw_proc,), daemon=True, name="dw-stderr").start()
                # Thread de capture stdout (trames décodées, stats…)
                def _dw_stdout(proc):
                    for line in proc.stdout:
                        line = line.rstrip()
                        APRSModem.dw_log_lines.append(line)
                        if line.strip():
                            print("[DW] " + line)
                        if "[0]" in line or "audio" in line.lower():
                            # Comptage des trames signalées par Dire Wolf
                            APRSModem.dw_frames_decoded += 1
                threading.Thread(target=_dw_stdout, args=(dw_proc,), daemon=True, name="dw-stdout").start()
                # Attendre que le port KISS TCP soit prêt
                for _ in range(20):
                    time.sleep(0.5)
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.setsockopt(socket.SOL_SOCKET,  socket.SO_REUSEADDR, 1)
                        s.setsockopt(socket.SOL_SOCKET,  socket.SO_KEEPALIVE, 1)
                        try:
                            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE,  30)
                            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL,  5)
                            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT,    3)
                        except AttributeError:
                            pass
                        s.settimeout(2)
                        s.connect((APRSModem.DIREWOLF_HOST, APRSModem.DIREWOLF_KISS_PORT))
                        sock = s
                        break
                    except OSError:
                        try: s.close()
                        except: pass
                        sock = None

                if sock is None:
                    print("[RX] Impossible de se connecter au port KISS de Dire Wolf")
                    raise RuntimeError("KISS port unreachable")

                sock.settimeout(1.0)
                APRSModem.rx_thread_alive = True
                print("[RX] Connecté au port KISS -- en écoute...")

                buf = bytearray()
                while self.is_rx_running:
                    # ── Niveau audio : estimation via sounddevice ─────────────
                    # (Dire Wolf gère l'audio ; on mesure à part si possible)
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        # Pas de donnée, on continue la boucle
                        continue
                    except OSError:
                        break

                    if not data:
                        break

                    # Comptage brut des octets reçus comme proxy de "bits"
                    APRSModem.rx_bit_count += len(data) * 8

                    buf.extend(data)
                    frames, buf = APRSModem._kiss_unwrap(buf)

                    for ax25_bytes in frames:
                        # Les trames KISS de Dire Wolf sont déjà validées (CRC retiré)
                        # On utilise un parseur AX.25 sans vérification CRC
                        frame = self._parse_ax25_kiss(ax25_bytes)
                        if frame:
                            APRSModem.rx_bit_count += len(ax25_bytes) * 8
                            APRSModem.rx_queue.put(frame)
                            print("[KISS] Trame RX : %s>%s %s" % (
                                frame.get("src","?"), frame.get("dest","?"),
                                frame.get("aprs_type","?")))
                        else:
                            print("[KISS] Trame AX.25 non parsée (%d octets)" % len(ax25_bytes))

            except Exception as e:
                print("[RX] Erreur pipeline Dire Wolf : " + str(e))
            finally:
                if sock:
                    try: sock.shutdown(socket.SHUT_RDWR)
                    except: pass
                    try: sock.close()
                    except: pass
                    sock = None
                # Invalider aussi la socket TX partagée si Direwolf redémarre
                with APRSModem._tx_sock_lock:
                    if APRSModem._tx_sock:
                        try: APRSModem._tx_sock.close()
                        except: pass
                        APRSModem._tx_sock = None
                if dw_proc:
                    try: dw_proc.terminate()
                    except: pass
                    try: dw_proc.wait(timeout=3)
                    except:
                        try: dw_proc.kill()
                        except: pass

            if self.is_rx_running:
                print("[RX] Pipeline interrompu -- redémarrage dans 3 s...")
                time.sleep(3.0)

        APRSModem.rx_thread_alive = False


modem = APRSModem(config_manager.data)
modem.start_rx()


def _enum_audio_devices():
    """
    Enumère les devices audio depuis ALSA (arecord/aplay) ET PortAudio.
    Retourne une liste unifiée avec un identifiant stable basé sur plughw:X,Y.
    Cela évite que Dire Wolf (qui tient l'entrée ALSA ouverte) cache les devices
    dans l'énumération PortAudio.
    """
    import subprocess as _sp
    devices = []
    seen_hw = set()

    # ── Source 1 : ALSA arecord/aplay (indépendant de PortAudio) ─────────────
    for cmd, direction in [(['arecord', '-l'], 'in'), (['aplay', '-l'], 'out')]:
        try:
            out = _sp.run(cmd, capture_output=True, text=True).stdout
            for line in out.splitlines():
                m = _re.search(r'card (\d+): ([^[]+) \[([^\]]+)\].*device (\d+): ([^\[]+)', line)
                if not m:
                    continue
                card_n, card_id, card_name, dev_n, dev_name = (
                    m.group(1), m.group(2).strip(), m.group(3).strip(),
                    m.group(4), m.group(5).strip()
                )
                hw_key = "plughw:%s,%s" % (card_n, dev_n)
                label  = "%s — %s" % (card_name, dev_name)
                if hw_key not in seen_hw:
                    seen_hw.add(hw_key)
                    devices.append({
                        "id":    hw_key,          # identifiant stable ALSA
                        "name":  label,
                        "hw":    hw_key,
                        "in":    False,
                        "out":   False,
                    })
                # Marquer in/out
                for d in devices:
                    if d["hw"] == hw_key:
                        d[direction] = True
        except Exception:
            pass

    # ── Source 2 : PortAudio (fallback ou devices supplémentaires) ────────────
    pa_devices = []
    try:
        dev_list = sd.query_devices()
        for i, d in enumerate(dev_list):
            if d['max_output_channels'] > 0 or d['max_input_channels'] > 0:
                pa_name = d['name']
                # Vérifier si déjà dans la liste ALSA (correspondance par nom partiel)
                already = any(
                    any(w.lower() in pa_name.lower() for w in dev['name'].split() if len(w) >= 4)
                    for dev in devices
                )
                if not already:
                    pa_devices.append({
                        "id":   i,
                        "name": pa_name + " (PA:%d)" % i,
                        "hw":   None,
                        "in":   d['max_input_channels'] > 0,
                        "out":  d['max_output_channels'] > 0,
                    })
    except Exception:
        pass

    all_devices = devices + pa_devices

    # S'assurer que le device actuellement configuré est toujours présent
    # même si ALSA ne le voit plus (Dire Wolf le tient) — on l'ajoute depuis config
    cfg_rx = config_manager.data.get('audio_device_rx')
    cfg_tx = config_manager.data.get('audio_device_tx')
    for cfg_val in set([cfg_rx, cfg_tx]):
        if cfg_val is None:
            continue
        if not any(str(d['id']) == str(cfg_val) for d in all_devices):
            all_devices.insert(0, {
                "id":   cfg_val,
                "name": "⚠️ %s (config sauvegardee)" % cfg_val,
                "hw":   None, "in": True, "out": True,
            })

    return all_devices


@app.route('/audio_devices')
def audio_devices_route():
    """Retourne la liste des devices audio (appelable par AJAX pour refresh)."""
    return jsonify(_enum_audio_devices())


@app.route('/')
def index():
    devices = _enum_audio_devices()

    symbol_table_options = (
        '<option value="/"'  + (' selected' if config_manager.data.get('symbol_table','/') == '/' else '') + '>/  Primaire</option>' +
        '<option value="\\"' + (' selected' if config_manager.data.get('symbol_table','/') == '\\' else '') + '>&#92; Secondaire</option>'
    )
    symbol_code_options = "".join([
        '<option value="%s"%s>%s  %s</option>' % (
            code,
            ' selected' if config_manager.data.get('symbol_code','[') == code else '',
            emoji, label
        )
        for code, emoji, label in [
            ('[', '🚶', 'Pieton'), ('>', '🚗', 'Voiture'),
            ('-', '🏠', 'Maison fixe'), ('O', '🎈', 'Ballon'),
            ('/', '🚙', 'Vehicule'), ('Y', '⛵', 'Voilier'),
            ('B', '⛵', 'Bateau'), ('X', '🚁', 'Helicoptere'),
            ('_', '🌦️', 'Meteo'), ('k', '🚙', '4x4/Portable'),
            ('b', '🚲', 'Velo'), ('r', '📡', 'Repeteur'),
            ('s', '🚢', 'Navire'), ('j', '🚙', 'Jeep'),
            ('u', '🚌', 'Bus'),
        ]
    ])
    beacon_interval_options = "".join([
        '<option value="%s"%s>%s</option>' % (
            v,
            ' selected' if str(config_manager.data.get('beacon_interval',0)) == str(v) else '',
            label
        )
        for v, label in [(0,'🔕 Desactive'),(5,'⏱️ 5 min'),(10,'⏱️ 10 min'),(15,'⏱️ 15 min'),(30,'⏱️ 30 min'),(60,'⏱️ 60 min')]
    ])

    def _interval_select(btype, schedules):
        """Génère un <select> d'intervalle pour un type de balise donné."""
        cur = schedules.get(btype, 0)
        opts = "".join([
            '<option value="%s"%s>%s</option>' % (
                v, ' selected' if str(cur) == str(v) else '', lbl
            )
            for v, lbl in [(0,'🔕 Off'),(5,'5min'),(10,'10min'),(15,'15min'),(30,'30min'),(60,'60min')]
        ])
        return '<select name="sched_%s" class="bg-slate-900 border border-slate-700 rounded-lg px-2 py-1 text-[10px] text-white outline-none focus:border-blue-500 appearance-none">%s</select>' % (btype, opts)

    _scheds = config_manager.data.get('beacon_schedules', {})

    def _dev_label(d):
        flags = []
        if d.get("in"):  flags.append("IN")
        if d.get("out"): flags.append("OUT")
        tag = (" [%s]" % "/".join(flags)) if flags else ""
        return d["name"] + tag

    dev_options_tx = '<option value="">-- Defaut systeme --</option>' + "".join([
        '<option value="%s"%s>%s</option>' % (
            str(d["id"]),
            ' selected' if str(d["id"]) == str(config_manager.data.get("audio_device_tx","")) else '',
            _dev_label(d)
        ) for d in devices if d.get("out")
    ])
    dev_options_rx = '<option value="">-- Defaut systeme --</option>' + "".join([
        '<option value="%s"%s>%s</option>' % (
            str(d["id"]),
            ' selected' if str(d["id"]) == str(config_manager.data.get("audio_device_rx","")) else '',
            _dev_label(d)
        ) for d in devices if d.get("in")
    ])

    return """<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <link rel="icon" type="image/jpeg" href="data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/4gHYSUNDX1BST0ZJTEUAAQEAAAHIAAAAAAQwAABtbnRyUkdCIFhZWiAH4AABAAEAAAAAAABhY3NwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAA9tYAAQAAAADTLQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAlkZXNjAAAA8AAAACRyWFlaAAABFAAAABRnWFlaAAABKAAAABRiWFlaAAABPAAAABR3dHB0AAABUAAAABRyVFJDAAABZAAAAChnVFJDAAABZAAAAChiVFJDAAABZAAAAChjcHJ0AAABjAAAADxtbHVjAAAAAAAAAAEAAAAMZW5VUwAAAAgAAAAcAHMAUgBHAEJYWVogAAAAAAAAb6IAADj1AAADkFhZWiAAAAAAAABimQAAt4UAABjaWFlaIAAAAAAAACSgAAAPhAAAts9YWVogAAAAAAAA9tYAAQAAAADTLXBhcmEAAAAAAAQAAAACZmYAAPKnAAANWQAAE9AAAApbAAAAAAAAAABtbHVjAAAAAAAAAAEAAAAMZW5VUwAAACAAAAAcAEcAbwBvAGcAbABlACAASQBuAGMALgAgADIAMAAxADb/2wBDAAUDBAQEAwUEBAQFBQUGBwwIBwcHBw8LCwkMEQ8SEhEPERETFhwXExQaFRERGCEYGh0dHx8fExciJCIeJBweHx7/2wBDAQUFBQcGBw4ICA4eFBEUHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh7/wAARCABPAEkDASIAAhEBAxEB/8QAHAABAAEFAQEAAAAAAAAAAAAABwABAgUGCAQD/8QAQxAAAQMCAgQEEgkFAAAAAAAAAQACAwQFBhEHEiExE0FhgQgUFRYiRVFUVXF0g5KUssLR0hcjMjZikaGxwSQmNThS/8QAGwEAAgMBAQEAAAAAAAAAAAAAAwUCBAYBBwD/xAA1EQABAwIDBQUECwAAAAAAAAABAAIDBBEFITEGEkGBkRZRYXGxIkJTshMUFSMlMzQ1ocHR/9oADAMBAAIRAxEAPwDj1Va0ucGtBJOwADevVaaCoudxhoaVutLK7IdwDjJ5ANqZ8NYcttip2tp4myVBH1k7x2bjydwcgWnwnBZcRJIO60an+gltXWsphY5k8EOssl6e0OZaLg5p3EUzyP2VeoV88DXH1V/wTwSqErSdkIfiHoEvGLPPuhBHUK9+Brj6s/4KdQr34HuPqz/gnUlWkr7shD8Q9AiDE3n3UFmyXoDM2i4ADjNM/wCC8Mkb4nlkjHMeN7XDIhdAErG3u0W+70xhrYGvOXYvGx7PEUCfZEBt4pM/EIzMQJPtNQeosliK0z2a6SUc3ZN+1G/LY9p3FY1Y2WJ8Tyx4sQmTSHC4W/aG6Zj7hX1jgC+GNjG58WsST7KTCUdaGO23mffSGSvTdm2gYdGRxv6lZnEbmpdy9FCVaSlzodsEWPE8l5umI6bpmht8TWtjL3NGs4Elx1SDsDf1Wk1uDbn1nT40gbDHZenDTwtkl+tcM8gQMsiOLfnsOxXBiUBnfATYt3R4XdoB4rjKZ5jDxxv/ABqtXJVpK3m0aKMcXXDJxDSWkGkMZlja6UCWVg25tbvPJ3eLNYix4MvV5wpd8SUQp3UlpIFTG6QiXI7SQ3LcBvzI51P69TG9njIgHPQnIDqiMgfllrotbJVpKY9LeB8OUGivDWLsOUrqczxxMqs5HO4QvZnrHM7CHAjZlvQ0So0dbHVxl7LixIz1uEcwllr8RdaVpZp2OttHV5DXZMY8+RzSfdRwk3Sqf7eg8rb7D0ZLAbStAr3W4geia0v5YSJoZ7beZ99IRKPNDfbXzPvpBJWy2d/bY+fzFJa4XqHcvQJ+6FKohrLNinD5kDZ6iNr290tLXMJ5iR+a0e/YttcuhmlwPMypbd7bcnOBaBwTmBz8yXZ/iI5ke0dZVUU3D0dVNTS5FuvFIWOyOwjMcS85Kn9ksNU+dxyJa4DuLQR0N1Nk5EQYBpfodV1zgjHko0BHE7qBhmtdKYOCD8myOjAYHbtgOw5ILwXjm1WPRxi62TRVD7ze3BkeTRwQYQQTnnsy1nfot/wX/qZfPHP7bVzuSlOFYfBI+pYRl9J8uYHVXXSObHE4cL/4uh9Mro7H0O+FbDNIx1TUcA4AHPYGF7iOQFwHOudiV96qsq6pkLKmqnnbCzUiEkhcI2/8tzOwcgXmJTvDqI0cbmk3JcXHmUNzt8NHcAFqWlT7vweVt9h6NElaU/u/B5U32Ho1WH2m/XnyCv04sxIWhztr5n30gEo70PyNE1ziz7JzY3DxAuz/AHCQiVr9nT+HR8/mKU1jfv3cvRQlWkqEq0lOkNrU/YNuFCzoUr7TvrIGzCSVnBmQB2s57dUZb8yufyVCVaSl9JRCmfK4G++7e8vBW97eY1vcoSrSVUlWEq6ptatU0pf4CDypvsPRskXSjI0WWmiz7J1QHAcga7P9wjpecbSm9efIK/ELNWVwrd32W8xVgBdH9iVo42Hf/B5kyUdVT1lMypppWyxPGbXNOxAy99ovFytLy6hqnxBxzcze13jB2LuC44aAGOQXYeoQaimEvtDVNxKsJRk3H96a0AwULuUxuz/Ryn0gXnvag9B/zLTdp6DvPRVhSPCSyVQlGnX/AHnvag9B/wAyp1/Xjvag9B/zLnaeg7z0RRTuCSiV85ZGRxukke1jGjNznHIAI4OPLwRl0vQjlDHfMsRd77c7oNWrqSY88xG0arfyG/nQZ9qaVrbxguPRGbCeK9eNLyLvcxwJ/poAWxfi7ruf+AsCoosNU1D6iV0smpRwLCy//9k=">
    <title>Py-APRS | Station de Controle</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        .glass { background: rgba(15,23,42,0.8); backdrop-filter: blur(12px); border: 1px solid rgba(51,65,85,0.5); }
        .custom-scrollbar::-webkit-scrollbar { width: 4px; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: #334155; border-radius: 10px; }
        .rx-entry { border-left: 3px solid #3b82f6; animation: slideIn 0.3s ease-out; }
        @keyframes slideIn { from { opacity:0; transform:translateX(-10px); } to { opacity:1; transform:translateX(0); } }
        #aprs-map { height: 520px; border-radius: 1.5rem; z-index: 0; }
        .leaflet-popup-content-wrapper { background: #0f172a; color: #cbd5e1; border: 1px solid #334155; border-radius: 12px; }
        .leaflet-popup-tip { background: #0f172a; }
        /* ── Clignotement onglet QSO (messages non lus) ── */
        @keyframes blink-qso {
            0%, 100% { color: #94a3b8; }
            50%       { color: #ef4444; text-shadow: 0 0 8px #ef444488; }
        }
        .qso-blink     { animation: blink-qso 1s ease-in-out infinite !important; }
        .qso-blink-mob { animation: blink-qso 1s ease-in-out infinite !important; color: #ef4444 !important; }

        .aprs-marker-label { background: rgba(15,23,42,0.85); color: #60a5fa; border: 1px solid #334155; border-radius: 6px; padding: 1px 5px; font-size: 10px; font-weight: 700; font-family: monospace; white-space: nowrap; }
        /* ── Toggle switch Alerte Proximité ── */
        .prox-switch { position:relative; display:inline-block; width:40px; height:22px; flex-shrink:0; }
        .prox-switch input { opacity:0; width:0; height:0; position:absolute; }
        .prox-slider { position:absolute; inset:0; background:#1e293b; border:1px solid #334155; border-radius:22px; cursor:pointer; transition:.25s; }
        .prox-slider:before { content:""; position:absolute; width:16px; height:16px; left:2px; top:2px; background:#64748b; border-radius:50%; transition:.25s; }
        .prox-switch input:checked + .prox-slider { background:#92400e; border-color:#f59e0b; }
        .prox-switch input:checked + .prox-slider:before { transform:translateX(18px); background:#fbbf24; }
        @keyframes issDotPulse {
            0%,100% { opacity:.55; } 50% { opacity:1; }
        }
        /* ── Mobile optimisations ── */
        * { -webkit-tap-highlight-color: transparent; box-sizing: border-box; }
        html { scroll-behavior: smooth; }
        body { padding-bottom: env(safe-area-inset-bottom, 0px); }
        /* Bottom nav fixe mobile */
        #mobile-nav {
            display: none;
            position: fixed; bottom: 0; left: 0; right: 0; z-index: 1000;
            background: rgba(2,6,23,0.95); backdrop-filter: blur(16px);
            border-top: 1px solid #1e293b;
            padding: 6px 4px calc(6px + env(safe-area-inset-bottom, 0px)) 4px;
        }
        #mobile-nav button {
            flex: 1; display: flex; flex-direction: column; align-items: center;
            gap: 2px; padding: 6px 2px; border: none; background: transparent;
            color: #475569; font-size: 9px; font-weight: 800; text-transform: uppercase;
            letter-spacing: .04em; cursor: pointer; border-radius: 10px;
            transition: color .15s, background .15s; min-width: 0;
            -webkit-tap-highlight-color: transparent;
        }
        #mobile-nav button .nav-icon { font-size: 18px; line-height: 1; }
        #mobile-nav button.active { color: #60a5fa; background: rgba(59,130,246,.12); }
        #mobile-nav button .nav-badge {
            position: absolute; top: 2px; right: 4px;
            background: #ef4444; color: #fff; font-size: 8px; font-weight: 900;
            border-radius: 999px; padding: 0 4px; min-width: 14px; text-align: center;
            line-height: 14px; height: 14px;
        }
        /* Cacher nav desktop sur mobile */
        @media (max-width: 767px) {
            #mobile-nav { display: flex; }
            .desktop-nav { display: none !important; }
            .desktop-header-full { flex-direction: row; gap: 3px; margin-bottom: 8px; }
            body > div { padding: 8px 8px 0 8px !important; }
            /* Marges inférieures pour la bottom nav (72px) */
            main { padding-bottom: 72px; }
            /* Carte plein écran mobile */
            #aprs-map { height: calc(100vh - 220px) !important; min-height: 300px; }
            /* Colonnes → stack sur mobile */
            .lg\:col-span-3, .lg\:col-span-4, .lg\:col-span-8, .lg\:col-span-9 { width: 100% !important; }
            /* Console trafic compacte */
            .h-\[780px\] { height: calc(100vh - 260px) !important; min-height: 300px; }
            /* Chat QSO mobile */
            .h-\[600px\] { height: calc(100vh - 320px) !important; min-height: 260px; }
            /* ISS iframe */
            .h-\[500px\] { height: 320px !important; }
            /* Padding réduit sur mobile */
            .p-10 { padding: 1.25rem !important; }
            .p-6 { padding: 1rem !important; }
            .px-6 { padding-left: 1rem !important; padding-right: 1rem !important; }
            /* Rounds moins extrêmes */
            .rounded-\[3rem\] { border-radius: 1.25rem !important; }
            .rounded-\[2\.5rem\] { border-radius: 1rem !important; }
            .rounded-\[2rem\] { border-radius: 1rem !important; }
            /* Inputs touch-friendly */
            input[type=text], input[type=number], input[type=password],
            input[type=email], textarea, select {
                font-size: 16px !important; /* Évite zoom auto iOS */
                min-height: 44px;
            }
            /* Boutons touch targets */
            button { min-height: 40px; }
            /* Header compact */
            header h1 { font-size: 1.4rem !important; }
            header .text-3xl { font-size: 1.6rem !important; }
            /* Masquer éléments secondaires sur mobile */
            .mobile-hide { display: none !important; }
            /* Grilles réglages */
            .grid-cols-3 { grid-template-columns: 1fr !important; }
            .col-span-2 { grid-column: span 1 !important; }
        }
        @media (min-width: 768px) {
            #mobile-nav { display: none !important; }
        }
    </style>
<script>
    function switchTab(tabId) {
        ['terminal', 'config', 'qso', 'map', 'iss', 'stats', 'logbook'].forEach(function(t) {
            var el  = document.getElementById('tab-' + t);
            var btn = document.getElementById('btn-' + t);
            var mbn = document.getElementById('mnav-' + t);
            if (el)  el.classList.add('hidden');
            if (btn) btn.className = "px-6 py-2.5 rounded-xl font-bold transition-all text-slate-500 hover:text-slate-200";
            if (mbn) mbn.classList.remove('active');
        });
        var tab = document.getElementById('tab-' + tabId);
        var btn = document.getElementById('btn-' + tabId);
        var mbn = document.getElementById('mnav-' + tabId);
        if (tab) tab.classList.remove('hidden');
        if (btn) btn.className = "px-6 py-2.5 rounded-xl font-bold transition-all bg-blue-600 text-white shadow-lg";
        if (mbn) mbn.classList.add('active');
        if (tabId === 'map') setTimeout(function(){ if(typeof _initMap==='function') _initMap(); }, 80);
        if (tabId === 'iss') {
            var iframe = document.getElementById('iss-iframe');
            if (iframe && iframe.src === 'about:blank') iframe.src = iframe.dataset.src;
        }
        document.dispatchEvent(new CustomEvent('aprs-switchtab', {detail: tabId}));
        /* Scroll top sur mobile */
        if (window.innerWidth < 768) window.scrollTo({top: 0, behavior: 'smooth'});
    }
</script>
</head>
<body class="bg-[#020617] text-slate-300 min-h-screen font-sans">
<div class="max-w-6xl mx-auto p-4 lg:p-8">

    <header class="flex flex-col lg:flex-row items-center justify-between mb-4 lg:mb-10 gap-2 lg:gap-6">
        <div class="flex items-center gap-5">
            <a href="/" onclick="location.reload(); return false;" title="Rafraîchir la page"
               class="bg-gradient-to-br from-blue-600 to-indigo-700 p-4 rounded-2xl shadow-xl hover:from-blue-500 hover:to-indigo-600 transition-all cursor-pointer">
                <i class="fas fa-broadcast-tower text-3xl text-white"></i>
            </a>
            <div>
                <h1 class="text-3xl font-black tracking-tight text-white italic">Py-APRS <span id="app-version-badge" class="text-blue-500 text-sm not-italic font-medium ml-2 cursor-pointer hover:text-blue-300 transition-colors" onclick="document.getElementById('modal-aide').classList.remove('hidden');aideShowTab('changelog')" title="Voir le changelog">v2.2</span></h1>
                <div class="flex items-center gap-2 mt-1">
                    <span class="relative flex h-2 w-2">
                        <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                        <span class="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
                    </span>
                    <p class="text-[10px] text-slate-500 font-mono uppercase tracking-[0.2em]">Station Active</p>
                    <span id="iss-active-dot" title="Alerte passage ISS activée"
                          style="display:none;align-items:center;gap:3px;font-size:9px;font-weight:700;color:#a78bfa;letter-spacing:.05em;text-transform:uppercase;animation:issDotPulse 2.5s ease-in-out infinite">
                        <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#7c3aed;box-shadow:0 0 6px #7c3aed"></span>ISS
                    </span>
                    <p class="text-[10px] text-blue-400/70 font-mono tracking-widest mt-0.5">📡 144.800 MHz — APRS France</p>
                </div>
                <!-- ── Indicateur PTT ON AIR ── -->
                <div id="ptt-indicator" class="flex items-center gap-2 px-3 py-1.5 rounded-xl transition-all duration-200"
                     style="background:transparent">
                    <span id="ptt-dot" style="
                        display:inline-block;width:10px;height:10px;border-radius:50%;
                        background:#1e293b;border:2px solid #334155;
                        transition:background .1s,box-shadow .1s;flex-shrink:0">
                    </span>
                    <span id="ptt-label" class="font-black text-[10px] tracking-widest uppercase transition-colors duration-200"
                          style="color:#334155;letter-spacing:.15em">PTT</span>
                </div>
            </div>
        </div>
        <nav class="desktop-nav flex bg-slate-900/80 p-1.5 rounded-2xl border border-slate-800 shadow-inner">
            <button id="btn-terminal" onclick="switchTab('terminal')" class="px-6 py-2.5 rounded-xl font-bold transition-all bg-blue-600 text-white shadow-lg">📻 TRAFIC</button>
            <button id="btn-config"   onclick="switchTab('config')"   class="px-6 py-2.5 rounded-xl font-bold transition-all text-slate-500 hover:text-slate-200">⚙️ REGLAGES</button>
            <button id="btn-qso"      onclick="switchTab('qso')"      class="px-6 py-2.5 rounded-xl font-bold transition-all text-slate-500 hover:text-slate-200">
                💬 QSO <span id="qso-badge" class="hidden ml-1 bg-red-500 text-white text-[9px] font-black rounded-full px-1.5 py-0.5">0</span>
            </button>
            <button id="btn-map"      onclick="switchTab('map')"      class="px-6 py-2.5 rounded-xl font-bold transition-all text-slate-500 hover:text-slate-200">
                🗺️ MAP
                <span id="map-badge" class="hidden ml-1 bg-emerald-500 text-white text-[9px] font-black rounded-full px-1.5 py-0.5">0</span>
            </button>
            <button id="btn-iss"      onclick="switchTab('iss')"      class="px-6 py-2.5 rounded-xl font-bold transition-all text-slate-500 hover:text-slate-200">🛰️ ISS</button>
            <button id="btn-stats"    onclick="switchTab('stats')"    class="px-6 py-2.5 rounded-xl font-bold transition-all text-slate-500 hover:text-slate-200">📊 STATS</button>
            <button id="btn-logbook"  onclick="switchTab('logbook')"  class="px-6 py-2.5 rounded-xl font-bold transition-all text-slate-500 hover:text-slate-200">📓 CARNET</button>
            <button onclick="document.getElementById('modal-aide').classList.remove('hidden')" class="px-6 py-2.5 rounded-xl font-bold transition-all text-slate-500 hover:text-amber-300">❓ AIDE</button>
        </nav>
    </header>

    <!-- ── Navigation bas de page (mobile) ── -->
    <nav id="mobile-nav" role="navigation">
        <button id="mnav-terminal" onclick="switchTab('terminal')" class="active">
            <span class="nav-icon">📻</span>
            <span>TRAFIC</span>
        </button>
        <button id="mnav-config" onclick="switchTab('config')">
            <span class="nav-icon">⚙️</span>
            <span>RÉGL.</span>
        </button>
        <button id="mnav-qso" onclick="switchTab('qso')" style="position:relative">
            <span class="nav-icon">💬</span>
            <span>QSO</span>
            <span id="mnav-qso-badge" style="display:none" class="nav-badge">0</span>
        </button>
        <button id="mnav-map" onclick="switchTab('map')" style="position:relative">
            <span class="nav-icon">🗺️</span>
            <span>MAP</span>
            <span id="mnav-map-badge" style="display:none" class="nav-badge">0</span>
        </button>
        <button id="mnav-iss" onclick="switchTab('iss')">
            <span class="nav-icon">🛰️</span>
            <span>ISS</span>
        </button>
        <button id="mnav-stats" onclick="switchTab('stats')">
            <span class="nav-icon">📊</span>
            <span>STATS</span>
        </button>
        <button id="mnav-logbook" onclick="switchTab('logbook')">
            <span class="nav-icon">📓</span>
            <span>CARNET</span>
        </button>
        <button onclick="document.getElementById('modal-aide').classList.remove('hidden')">
            <span class="nav-icon">❓</span>
            <span>AIDE</span>
        </button>
    </nav>

    <main>
        <!-- ═══════════════════════════════ TRAFIC ═══════════════════════════════ -->
        <div id="tab-terminal" class="grid grid-cols-1 lg:grid-cols-12 gap-8">
            <div class="lg:col-span-3 space-y-6">
                <section class="glass p-6 rounded-[2rem] shadow-2xl">
                    <h2 class="text-[10px] font-black text-blue-400 uppercase tracking-widest mb-6 flex items-center gap-2">
                        📨 Envoyer un Message
                    </h2>
                    <div class="space-y-5">
                        <div>
                            <label class="text-[10px] text-slate-500 uppercase font-bold ml-1">📍 Destinataire</label>
                            <input type="text" id="dest_call" class="w-full bg-slate-950/50 border border-slate-800 rounded-xl p-3 text-white focus:ring-2 focus:ring-blue-600 outline-none transition-all mt-1" placeholder="Ex: F1ZGK-10">
                        </div>
                        <div>
                            <label class="text-[10px] text-slate-500 uppercase font-bold ml-1">✏️ Message</label>
                            <textarea id="msg" rows="3" class="w-full bg-slate-950/50 border border-slate-800 rounded-xl p-3 text-white focus:ring-2 focus:ring-blue-600 outline-none transition-all mt-1 resize-none" placeholder="Tapez votre message..."></textarea>
                        </div>
                        <button onclick="send()" class="w-full bg-blue-600 hover:bg-blue-500 p-4 rounded-xl font-black text-white shadow-lg active:scale-95 transition-all flex items-center justify-center gap-3">
                            📡 ENVOYER QSO
                        </button>
                        <div class="grid grid-cols-1 gap-3 pt-2">
                            <button onclick="sendISS()" class="bg-slate-800/50 hover:bg-indigo-600/30 border border-slate-700 p-3 rounded-xl text-xs font-bold transition-all flex items-center justify-center gap-2">
                                🛸 BEACON ISS
                            </button>
                            <button onclick="sendBeacon()" class="bg-slate-800/50 hover:bg-blue-600/30 border border-slate-700 p-3 rounded-xl text-xs font-bold transition-all flex items-center justify-center gap-2">
                                📡 BEACON STATION
                            </button>
                            <button onclick="sendWeather(this)" class="bg-slate-800/50 hover:bg-cyan-600/30 border border-slate-700 p-3 rounded-xl text-xs font-bold transition-all flex items-center justify-center gap-2">
                                🌦️ BEACON MÉTÉO
                            </button>
                            <button onclick="sendPropagation(this)" class="bg-slate-800/50 hover:bg-violet-600/30 border border-slate-700 p-3 rounded-xl text-xs font-bold transition-all flex items-center justify-center gap-2">
                                📶 BEACON PROPAGATION
                            </button>
                            <button onclick="sendStatus()" class="bg-slate-800/50 hover:bg-purple-600/30 border border-slate-700 p-3 rounded-xl text-xs font-bold transition-all flex items-center justify-center gap-2">
                                🛰️ ENVOYER STATUT
                            </button>
                        </div>
                    </div>
                </section>

                <div class="glass p-6 rounded-[2rem] bg-blue-900/5 border-blue-500/10">
                    <h3 class="text-[10px] font-black text-slate-500 uppercase tracking-widest mb-4 italic">📊 Statut Station</h3>
                    <div class="space-y-3 text-xs font-mono">

                        <!-- Dernier TX -->
                        <div class="flex justify-between items-center">
                            <span class="text-slate-500">📤 Dernier TX:</span>
                            <span id="last-tx" class="text-blue-400">--</span>
                        </div>

                        <!-- Balises actives -->
                        <div class="border-t border-slate-800/60 pt-3">
                            <div class="text-[10px] font-black text-slate-500 uppercase tracking-widest mb-2">Balises automatiques</div>
                            <div id="beacon-badges" class="space-y-1.5 mb-3">
                                <!-- Station -->
                                <div id="badge-station" class="flex items-center justify-between px-2.5 py-1.5 rounded-xl border transition-all bg-slate-800/40 border-slate-700/50 text-slate-600">
                                    <div class="flex items-center gap-1.5">
                                        <span id="led-station" class="w-2 h-2 rounded-full bg-slate-700 flex-shrink-0"></span>
                                        <span class="text-[11px]">📡 Station</span>
                                    </div>
                                    <span id="countdown-station" class="text-[10px] font-mono tabular-nums"></span>
                                </div>
                                <!-- ISS -->
                                <div id="badge-iss" class="flex items-center justify-between px-2.5 py-1.5 rounded-xl border transition-all bg-slate-800/40 border-slate-700/50 text-slate-600">
                                    <div class="flex items-center gap-1.5">
                                        <span id="led-iss" class="w-2 h-2 rounded-full bg-slate-700 flex-shrink-0"></span>
                                        <span class="text-[11px]">🛸 ISS</span>
                                    </div>
                                    <span id="countdown-iss" class="text-[10px] font-mono tabular-nums"></span>
                                </div>
                                <!-- Météo -->
                                <div id="badge-meteo" class="flex items-center justify-between px-2.5 py-1.5 rounded-xl border transition-all bg-slate-800/40 border-slate-700/50 text-slate-600">
                                    <div class="flex items-center gap-1.5">
                                        <span id="led-meteo" class="w-2 h-2 rounded-full bg-slate-700 flex-shrink-0"></span>
                                        <span class="text-[11px]">🌦️ Météo</span>
                                    </div>
                                    <span id="countdown-meteo" class="text-[10px] font-mono tabular-nums"></span>
                                </div>
                                <!-- Propagation -->
                                <div id="badge-propagation" class="flex items-center justify-between px-2.5 py-1.5 rounded-xl border transition-all bg-slate-800/40 border-slate-700/50 text-slate-600">
                                    <div class="flex items-center gap-1.5">
                                        <span id="led-propagation" class="w-2 h-2 rounded-full bg-slate-700 flex-shrink-0"></span>
                                        <span class="text-[11px]">📶 Propagation</span>
                                    </div>
                                    <span id="countdown-propagation" class="text-[10px] font-mono tabular-nums"></span>
                                </div>
                            </div>
                        </div>

                        <button onclick="testRX(this)" class="w-full mt-1 bg-slate-800 hover:bg-emerald-900/30 border border-slate-700 py-2 rounded-lg text-[10px] font-bold uppercase tracking-widest transition-all">
                            🎧 Tester la reception
                        </button>
                        <!-- ── Notifications navigateur ── -->
                        <div class="border-t border-slate-800/60 pt-3 mt-1">
                            <div class="text-[10px] font-black text-slate-500 uppercase tracking-widest mb-2">🔔 Notifications</div>
                            <button id="notif-btn" onclick="togglePushNotif()" class="w-full py-2 rounded-lg text-[10px] font-bold uppercase tracking-widest transition-all bg-slate-800 border border-slate-700 text-slate-500 hover:border-blue-600 hover:text-blue-300">
                                ─ Chargement…
                            </button>
                            <div id="notif-status" class="text-[9px] text-slate-600 text-center mt-1 italic"></div>
                        </div>
                    </div>
                </div>

                <!-- ── Passages ISS (compact) ── -->
                <div style="border-radius:1.5rem;overflow:hidden;box-shadow:0 10px 30px #0006;background:rgba(15,23,42,0.6);border:1px solid #1e293b">
                    <div style="padding:10px 16px;background:rgba(15,23,42,0.5);border-bottom:1px solid #1e293b;display:flex;align-items:center;justify-content:space-between">
                        <span style="font-size:10px;font-weight:900;color:#a78bfa;text-transform:uppercase;letter-spacing:.1em">🛸 Passages ISS</span>
                        <button type="button" onclick="issPassRefresh()" id="iss-refresh-btn"
                            style="font-size:9px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:.08em;background:none;border:none;cursor:pointer;padding:0">
                            ↺ MAJ
                        </button>
                    </div>
                    <div style="padding:12px 14px;display:flex;flex-direction:column;gap:6px">
                        <div id="iss-pass-list" style="font-size:10px;color:#475569;font-style:italic;text-align:center;min-height:32px">
                            ⏳ Chargement...
                        </div>
                        <div id="iss-pass-status" style="font-size:9px;color:#475569;font-style:italic;text-align:center"></div>
                    </div>
                </div>

            </div>

            <div class="lg:col-span-9">
                <div class="glass rounded-[2.5rem] overflow-hidden flex flex-col h-[780px] shadow-2xl">
                    <div class="px-6 py-4 bg-slate-900/50 border-b border-slate-800">
                        <div class="flex justify-between items-center mb-3">
                            <div class="flex items-center gap-3">
                                <span id="rx-led" class="w-2.5 h-2.5 rounded-full bg-slate-700 inline-block transition-colors duration-300"></span>
                                <span class="text-xs font-black text-slate-400 uppercase tracking-widest">📻 Moniteur de Trafic APRS</span>
                            </div>
                            <div class="flex items-center gap-4">
                                <span class="text-[10px] font-mono text-slate-600">
                                    📤 TX: <span id="tx-count" class="text-blue-400">0</span>
                                    &nbsp;📥 RX: <span id="rx-count" class="text-emerald-400">0</span>
                                </span>
                                <button onclick="clearConsole()" class="text-[10px] text-slate-600 hover:text-red-400 font-bold uppercase tracking-widest transition-colors">
                                    🗑️ Effacer
                                </button>
                            </div>
                        </div>
                        <div class="flex items-center gap-3">
                            <span class="text-[10px] text-slate-600 font-mono uppercase shrink-0">📶 Niveau RX</span>
                            <div class="flex-grow bg-slate-950 rounded-full h-1.5 overflow-hidden">
                                <div id="rx-bar" class="h-1.5 rounded-full transition-all duration-500" style="width:0%;background:linear-gradient(90deg,#059669,#34d399)"></div>
                            </div>
                            <span id="rx-level-pct" class="text-[10px] font-mono text-slate-500 w-8 text-right shrink-0">0%</span>
                            <span id="rx-status-text" class="text-[10px] text-slate-600 shrink-0 w-24">⏳ En attente...</span>
                        </div>
                    </div>
                    <div id="console" class="flex-grow p-6 font-mono text-sm overflow-y-auto custom-scrollbar space-y-3 bg-slate-950/30">
                        <div class="text-slate-500 italic opacity-50 border-b border-slate-800 pb-2">📻 -- Session Py-APRS demarree --</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- ═══════════════════════════════ REGLAGES ═══════════════════════════════ -->
        <div id="tab-config" class="hidden max-w-2xl mx-auto">
            <div class="glass p-10 rounded-[3rem] shadow-2xl">
                <h2 class="text-2xl font-black text-white mb-8 flex items-center gap-4">
                    ⚙️ Configuration Station
                </h2>
                <form id="configForm" class="space-y-8">
                    <div class="grid grid-cols-3 gap-8">
                        <div>
                            <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">📻 Mon Indicatif (SSID)</label>
                            <input type="text" name="callsign" value='""" + config_manager.data['callsign'] + """' class="w-full bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all">
                        </div>
                        <div>
                            <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">🔌 Port PTT</label>
                            <input type="text" name="serial_port" value='""" + config_manager.data['serial_port'] + """' class="w-full bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all">
                        </div>
                        <div>
                            <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">📡 Mode PTT</label>
                            <select name="ptt_mode" class="w-full bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all appearance-none">
                                """ + "".join([
                                    '<option value="%s"%s>%s</option>' % (
                                        v,
                                        ' selected' if config_manager.data.get('ptt_mode','RTS') == v else '',
                                        l
                                    ) for v, l in [
                                        ('RTS',     'RTS  (standard)'),
                                        ('DTR',     'DTR  (Microham µH-Router)'),
                                        ('RTS+DTR', 'RTS + DTR'),
                                    ]
                                ]) + """
                            </select>
                        </div>
                    </div>
                    <div class="grid grid-cols-2 gap-8">
                        <div>
                            <div class="flex items-center justify-between mb-3 ml-1">
                                <label class="block text-[10px] font-black text-slate-500 uppercase">🔊 Audio TX</label>
                                <button type="button" onclick="refreshDevices()" class="text-[9px] text-blue-400 hover:text-blue-300 font-bold uppercase tracking-widest">↺ Rafraichir</button>
                            </div>
                            <select id="sel_audio_tx" name="audio_device_tx" class="w-full bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all appearance-none">""" + dev_options_tx + """</select>
                        </div>
                        <div>
                            <div class="flex items-center justify-between mb-3 ml-1">
                                <label class="block text-[10px] font-black text-slate-500 uppercase">🎙️ Audio RX</label>
                            </div>
                            <select id="sel_audio_rx" name="audio_device_rx" class="w-full bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all appearance-none">""" + dev_options_rx + """</select>
                        </div>
                    </div>
                    <div class="grid grid-cols-3 gap-8">
                        <div>
                            <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">⏱️ TX Delay (ms)</label>
                            <input type="number" name="tx_delay_ms" value='""" + str(config_manager.data['tx_delay_ms']) + """' class="w-full bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all">
                        </div>
                        <div>
                            <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">📡 PTT Delay (ms)</label>
                            <input type="number" name="ptt_delay_ms" value='""" + str(config_manager.data.get('ptt_delay_ms', 250)) + """' class="w-full bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all">
                        </div>
                        <div>
                            <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">🔉 Volume Modulation</label>
                            <input type="range" name="volume" min="0" max="1" step="0.1" value='""" + str(config_manager.data['volume']) + """' class="w-full mt-4">
                        </div>
                    </div>
                    <div class="border-t border-slate-800 pt-8">
                        <h3 class="text-[10px] font-black text-blue-400 uppercase tracking-widest mb-6 flex items-center gap-2">
                            🪪 Informations Station
                        </h3>
                        <div class="space-y-6">
                            <div class="grid grid-cols-2 gap-8">
                                <div>
                                    <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">🗺️ Localisation</label>
                                    <!-- Toggle locator / coordonnées -->
                                    <div class="flex gap-2 mb-3">
                                        <label class="flex items-center gap-2 cursor-pointer bg-slate-900 border border-slate-800 rounded-xl px-3 py-2 flex-1 has-[:checked]:border-blue-500 transition-all">
                                            <input type="radio" name="geo_mode" value="locator" ' + ('checked' if config_manager.data.get('geo_mode','locator')=='locator' else '') + ' onchange="aprsGeoToggle(this.value)" class="accent-blue-500">
                                            <span class="text-xs text-slate-300 font-mono">Locator</span>
                                        </label>
                                        <label class="flex items-center gap-2 cursor-pointer bg-slate-900 border border-slate-800 rounded-xl px-3 py-2 flex-1 has-[:checked]:border-blue-500 transition-all">
                                            <input type="radio" name="geo_mode" value="coords" ' + ('checked' if config_manager.data.get('geo_mode','locator')=='coords' else '') + ' onchange="aprsGeoToggle(this.value)" class="accent-blue-500">
                                            <span class="text-xs text-slate-300 font-mono">Lat / Lon</span>
                                        </label>
                                    </div>
                                    <!-- Champ Locator -->
                                    <div id="geoLocatorBlock" ' + ('style="display:none"' if config_manager.data.get('geo_mode','locator')=='coords' else '') + '>
                                        <input type="text" name="maidenhead" value='""" + config_manager.data.get('maidenhead','') + """' maxlength="6" class="w-full bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all font-mono uppercase" placeholder="ex: JN07II">
                                    </div>
                                    <!-- Champs coordonnées -->
                                    <div id="geoCoordsBlock" ' + ('' if config_manager.data.get('geo_mode','locator')=='coords' else 'style="display:none"') + ' class="flex gap-2">
                                        <input type="text" name="lat_manual" value='""" + str(config_manager.data.get('lat_manual','')) + """' class="w-1/2 bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all font-mono" placeholder="Lat  47.3941">
                                        <input type="text" name="lon_manual" value='""" + str(config_manager.data.get('lon_manual','')) + """' class="w-1/2 bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all font-mono" placeholder="Lon  0.6848">
                                    </div>
                                </div>
                                <div>
                                    <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">🛤️ Digi Path</label>
                                    <select id="pathPreset" onchange="applyPathPreset(this)" class="w-full bg-slate-900 border border-slate-800 rounded-xl p-3 text-white outline-none focus:border-blue-500 transition-all appearance-none mb-2 text-[11px]">
                                        <option value="">— Choisir un preset —</option>
                                        <option value="WIDE1-1,WIDE2-1">WIDE1-1,WIDE2-1 &nbsp;(standard national)</option>
                                        <option value="WIDE2-1">WIDE2-1 &nbsp;(1 saut wide)</option>
                                        <option value="WIDE2-2">WIDE2-2 &nbsp;(2 sauts wide)</option>
                                        <option value="WIDE1-1">WIDE1-1 &nbsp;(local seulement)</option>
                                        <option value="WIDE1-1,WIDE2-2">WIDE1-1,WIDE2-2 &nbsp;(portée max)</option>
                                        <option value="RELAY,WIDE">RELAY,WIDE &nbsp;(ancien standard)</option>
                                        <option value="TRACE2-2">TRACE2-2 &nbsp;(tracé complet)</option>
                                        <option value="">— Sans digipeat (direct RF) —</option>
                                        <option value="NOGATE">NOGATE &nbsp;(pas de passerelle IS)</option>
                                        <option value="custom">✏️ Personnalise...</option>
                                    </select>
                                    <input type="text" id="pathCustom" name="path" value='""" + config_manager.data.get('path','WIDE1-1,WIDE2-1') + """' class="w-full bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all font-mono text-sm" placeholder="WIDE1-1,WIDE2-1">
                                </div>
                            </div>
                            <div class="grid grid-cols-2 gap-8">
                                <div>
                                    <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">🔣 Symbole APRS</label>
                                    <div class="flex gap-2">
                                        <select name="symbol_table" class="flex-1 bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all appearance-none">""" + symbol_table_options + """</select>
                                        <select name="symbol_code" class="flex-1 bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all appearance-none">""" + symbol_code_options + """</select>
                                    </div>
                                </div>
                                <div class="grid grid-cols-1 gap-4">
                                    <div>
                                        <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">🔔 Balises automatiques</label>
                                        <div class="space-y-2">
                                            """ + "".join([
                                                '<div class="flex items-center justify-between bg-slate-900/60 rounded-xl px-3 py-2 gap-3">'
                                                + '<span class="text-[11px] font-bold text-slate-300 w-32">%s</span>' % lbl
                                                + _interval_select(btype, _scheds)
                                                + '</div>'
                                                for btype, lbl in [
                                                    ('station',     '📡 Station'),
                                                    ('iss',         '🛸 ISS'),
                                                    ('meteo',       '🌦️ Météo'),
                                                    ('propagation', '📶 Propagation'),
                                                ]
                                            ]) + """
                                        </div>
                                    </div>
                                </div>
                            </div>
                            <div>
                                <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">💬 Commentaire Station</label>
                                <input type="text" name="station_comment" value='""" + config_manager.data.get('station_comment','') + """' maxlength="43" class="w-full bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all" placeholder="ex: QRP portable - Touraine 73">
                            </div>
                            <div>
                                <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">📢 Texte de Statut</label>
                                <input type="text" name="station_status" value='""" + config_manager.data.get('station_status','') + """' maxlength="62" class="w-full bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all" placeholder="ex: En ecoute 144.800 MHz">
                            </div>
                        </div>
                    </div>

                    <!-- ── iGate APRS-IS ─────────────────────────────────── -->
                    <div class="glass rounded-[2rem] p-6 space-y-5 mt-6">
                        <div class="flex items-center justify-between">
                            <h2 class="text-sm font-black text-white uppercase tracking-widest">📡 iGate APRS-IS</h2>
                            <div id="igate-status-badge" class="flex items-center gap-2 text-[10px] font-mono text-slate-500">
                                <span id="igate-dot" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#334155"></span>
                                <span id="igate-status-txt">–</span>
                            </div>
                        </div>

                        <!-- Toggle activer/désactiver -->
                        <div class="flex items-center justify-between bg-slate-900/60 rounded-2xl px-4 py-3">
                            <div>
                                <div class="text-white font-bold text-xs">Activer l'iGate</div>
                                <div class="text-slate-500 text-[10px] mt-0.5">Relaie les trames RF reçues vers APRS-IS</div>
                            </div>
                            <label class="relative inline-flex items-center cursor-pointer">
                                <input type="checkbox" name="igate_enabled" id="igate_enabled"
                                    """ + ('checked' if config_manager.data.get('igate_enabled') else '') + """
                                    class="sr-only peer">
                                <div class="w-11 h-6 bg-slate-700 peer-focus:ring-2 peer-focus:ring-blue-500 rounded-full peer
                                    peer-checked:after:translate-x-full peer-checked:bg-blue-600
                                    after:content-[''] after:absolute after:top-[2px] after:left-[2px]
                                    after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all"></div>
                            </label>
                        </div>

                        <!-- Mode RX-only / Full iGate -->
                        <div>
                            <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">Mode</label>
                            <div class="grid grid-cols-2 gap-2">
                                <label class="flex items-start gap-3 bg-slate-900/60 rounded-2xl px-4 py-3 cursor-pointer border border-transparent has-[:checked]:border-blue-500/50">
                                    <input type="radio" name="igate_rx_only" value="true"
                                        """ + ('checked' if config_manager.data.get('igate_rx_only', True) else '') + """
                                        class="mt-1 accent-blue-500">
                                    <div>
                                        <div class="text-white text-xs font-bold">/R RX-iGate</div>
                                        <div class="text-slate-500 text-[9px] mt-0.5">RF → IS uniquement<br>Recommandé pour débuter</div>
                                    </div>
                                </label>
                                <label class="flex items-start gap-3 bg-slate-900/60 rounded-2xl px-4 py-3 cursor-pointer border border-transparent has-[:checked]:border-amber-500/50">
                                    <input type="radio" name="igate_rx_only" value="false"
                                        """ + ('' if config_manager.data.get('igate_rx_only', True) else 'checked') + """
                                        class="mt-1 accent-amber-500">
                                    <div>
                                        <div class="text-amber-300 text-xs font-bold">/& Full iGate</div>
                                        <div class="text-slate-500 text-[9px] mt-0.5">RF ↔ IS bidirectionnel<br>Nécessite passcode valide</div>
                                    </div>
                                </label>
                            </div>
                        </div>

                        <!-- Serveur + Port -->
                        <div class="grid grid-cols-3 gap-3">
                            <div class="col-span-2">
                                <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">Serveur APRS-IS</label>
                                <input type="text" name="igate_server"
                                    value='""" + config_manager.data.get('igate_server','rotate.aprs2.net') + """'
                                    class="w-full bg-slate-900 border border-slate-800 rounded-xl p-3 text-white text-sm outline-none focus:border-blue-500 transition-all font-mono"
                                    placeholder="rotate.aprs2.net">
                            </div>
                            <div>
                                <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">Port</label>
                                <input type="number" name="igate_port"
                                    value='""" + str(config_manager.data.get('igate_port',14580)) + """'
                                    class="w-full bg-slate-900 border border-slate-800 rounded-xl p-3 text-white text-sm outline-none focus:border-blue-500 transition-all font-mono">
                            </div>
                        </div>

                        <!-- Passcode -->
                        <div>
                            <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">
                                Passcode <span class="text-slate-700 normal-case">(auto-calculé si vide)</span>
                            </label>
                            <div class="flex gap-3 items-center">
                                <input type="text" name="igate_passcode" id="igate_passcode"
                                    value='""" + str(config_manager.data.get('igate_passcode','-1')) + """'
                                    class="flex-1 bg-slate-900 border border-slate-800 rounded-xl p-3 text-white text-sm font-mono outline-none focus:border-blue-500 transition-all"
                                    placeholder="Auto">
                                <button type="button" onclick="igateCalcPasscode()"
                                    class="shrink-0 px-3 py-3 bg-slate-800 hover:bg-slate-700 rounded-xl text-[10px] font-bold text-slate-300 transition-colors">
                                    Calculer
                                </button>
                            </div>
                        </div>

                        <!-- Filtre IS -->
                        <div>
                            <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">
                                Filtre <span class="text-slate-700 normal-case">(optionnel)</span>
                            </label>
                            <input type="text" name="igate_filter"
                                value='""" + config_manager.data.get('igate_filter','') + """'
                                class="w-full bg-slate-900 border border-slate-800 rounded-xl p-3 text-white text-sm font-mono outline-none focus:border-blue-500 transition-all"
                                placeholder="r/46.5/1.5/200  (lat/lon/km)">
                            <p class="text-slate-600 text-[9px] mt-2 ml-1">
                                Ex : <span class="text-slate-500">r/46.5/1.5/200</span> — rayon 200 km autour de JN07II · <span class="text-slate-500">b/F4XXX*</span> — callsigns spécifiques
                            </p>
                        </div>

                        <!-- Compteurs temps réel -->
                        <div class="grid grid-cols-2 gap-3">
                            <div class="bg-slate-900/60 rounded-xl px-3 py-2 text-center">
                                <div class="text-slate-500 text-[9px] uppercase">RF → IS</div>
                                <div id="igate-gated" class="text-emerald-400 font-black text-lg tabular-nums">0</div>
                            </div>
                            <div class="bg-slate-900/60 rounded-xl px-3 py-2 text-center">
                                <div class="text-slate-500 text-[9px] uppercase">IS reçues</div>
                                <div id="igate-is-rx" class="text-blue-400 font-black text-lg tabular-nums">0</div>
                            </div>
                        </div>
                    </div>


                    <!-- ── Synchronisation Wavelog ───────────────────────── -->
                    <div class="glass rounded-[2rem] p-6 space-y-5 mt-6">
                        <div class="flex items-center justify-between">
                            <h2 class="text-sm font-black text-white uppercase tracking-widest flex items-center gap-2">
                                <img src="https://wavelog.org/assets/img/wavelog-sm.png" style="height:18px;vertical-align:middle;filter:brightness(1.2)" onerror="this.style.display='none'">
                                🌊 Synchronisation Wavelog
                            </h2>
                            <div id="wavelog-status-badge" class="flex items-center gap-2 text-[10px] font-mono text-slate-500">
                                <span id="wavelog-dot" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#334155"></span>
                                <span id="wavelog-status-txt">–</span>
                            </div>
                        </div>

                        <!-- Toggle activer -->
                        <div class="flex items-center justify-between bg-slate-900/60 rounded-2xl px-4 py-3">
                            <div>
                                <div class="text-white font-bold text-xs">Activer la synchronisation</div>
                                <div class="text-slate-500 text-[10px] mt-0.5">Exporte automatiquement les QSO vers votre instance Wavelog</div>
                            </div>
                            <label class="relative inline-flex items-center cursor-pointer">
                                <input type="checkbox" name="wavelog_enabled" id="wavelog_enabled"
                                    """ + ('checked' if config_manager.data.get('wavelog',{}).get('enabled') else '') + """
                                    class="sr-only peer">
                                <div class="w-11 h-6 bg-slate-700 peer-focus:ring-2 peer-focus:ring-cyan-500 rounded-full peer
                                    peer-checked:after:translate-x-full peer-checked:bg-cyan-600
                                    after:content-[''] after:absolute after:top-[2px] after:left-[2px]
                                    after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all"></div>
                            </label>
                        </div>

                        <!-- URL + API key -->
                        <div class="grid grid-cols-1 gap-4">
                            <div>
                                <label class="block text-[10px] font-black text-slate-500 uppercase mb-2 ml-1">URL Wavelog</label>
                                <input type="text" name="wavelog_url" id="wavelog_url"
                                    value='""" + (config_manager.data.get('wavelog',{}).get('url','')) + """'
                                    class="w-full bg-slate-900 border border-slate-800 rounded-xl p-3 text-white text-sm font-mono outline-none focus:border-cyan-500 transition-all"
                                    placeholder="https://monwavelog.example.com">
                                <p class="text-slate-600 text-[9px] mt-1 ml-1">Sans slash final · ex : <span class="text-slate-500 font-mono">https://log.f1riq.fr</span></p>
                            </div>
                            <div>
                                <div class="flex items-center justify-between mb-2 ml-1">
                                    <label class="block text-[10px] font-black text-slate-500 uppercase">Clé API</label>
                                    <a href="#" onclick="document.getElementById('wavelog_url').value && window.open(document.getElementById('wavelog_url').value+'/index.php/admin/api_keys','_blank'); return false;"
                                       class="text-[9px] text-cyan-500 hover:text-cyan-300 font-bold uppercase tracking-widest transition-colors">↗ Gérer les clés</a>
                                </div>
                                <div class="flex gap-2">
                                    <input type="password" name="wavelog_api_key" id="wavelog_api_key"
                                        value='""" + (config_manager.data.get('wavelog',{}).get('api_key','')) + """'
                                        class="flex-1 bg-slate-900 border border-slate-800 rounded-xl p-3 text-white text-sm font-mono outline-none focus:border-cyan-500 transition-all"
                                        placeholder="Votre clé API Wavelog"
                                        autocomplete="off">
                                    <button type="button" onclick="wavelogTestConn()"
                                        id="wavelog-test-btn"
                                        class="shrink-0 px-3 py-2 bg-slate-800 hover:bg-cyan-900/40 border border-slate-700 hover:border-cyan-600 rounded-xl text-[10px] font-bold text-cyan-300 transition-colors">
                                        🔌 Tester
                                    </button>
                                </div>
                            </div>
                        </div>

                        <!-- Station ID + Intervalle -->
                        <div class="grid grid-cols-2 gap-4">
                            <div>
                                <label class="block text-[10px] font-black text-slate-500 uppercase mb-2 ml-1">ID Station Wavelog</label>
                                <input type="number" name="wavelog_station_id" min="1"
                                    value='""" + str(config_manager.data.get('wavelog',{}).get('station_id',1)) + """'
                                    class="w-full bg-slate-900 border border-slate-800 rounded-xl p-3 text-white text-sm font-mono outline-none focus:border-cyan-500 transition-all">
                                <p class="text-slate-600 text-[9px] mt-1 ml-1">Paramètres → Stations dans Wavelog</p>
                            </div>
                            <div>
                                <label class="block text-[10px] font-black text-slate-500 uppercase mb-2 ml-1">Intervalle synchro (min)</label>
                                <select name="wavelog_sync_interval"
                                    class="w-full bg-slate-900 border border-slate-800 rounded-xl p-3 text-white text-sm outline-none focus:border-cyan-500 appearance-none transition-all">
                                    """ + "".join([
                                        '<option value="%d"%s>%d min</option>' % (v,
                                        ' selected' if str(config_manager.data.get('wavelog',{}).get('sync_interval',5)) == str(v) else '', v)
                                        for v in [1,5,10,15,30,60]
                                    ]) + """
                                </select>
                            </div>
                        </div>

                        <!-- Options de filtre -->
                        <div class="space-y-2">
                            <div class="text-[10px] font-black text-slate-500 uppercase mb-2">Trames à synchroniser</div>
                            <label class="flex items-center gap-3 bg-slate-900/60 rounded-2xl px-4 py-3 cursor-pointer border border-transparent has-[:checked]:border-cyan-500/40">
                                <input type="checkbox" name="wavelog_sync_rx" id="wavelog_sync_rx"
                                    """ + ('checked' if config_manager.data.get('wavelog',{}).get('sync_rx',True) else '') + """
                                    class="accent-cyan-500">
                                <div>
                                    <div class="text-white text-xs font-bold">📥 Trames RX (reçues)</div>
                                    <div class="text-slate-500 text-[9px]">Stations entendues en RF ou via iGate IS</div>
                                </div>
                            </label>
                            <label class="flex items-center gap-3 bg-slate-900/60 rounded-2xl px-4 py-3 cursor-pointer border border-transparent has-[:checked]:border-cyan-500/40">
                                <input type="checkbox" name="wavelog_sync_tx" id="wavelog_sync_tx"
                                    """ + ('checked' if config_manager.data.get('wavelog',{}).get('sync_tx',True) else '') + """
                                    class="accent-cyan-500">
                                <div>
                                    <div class="text-white text-xs font-bold">📤 Trames TX (émises)</div>
                                    <div class="text-slate-500 text-[9px]">Beacons, messages et QSO envoyés</div>
                                </div>
                            </label>
                            <label class="flex items-center gap-3 bg-slate-900/60 rounded-2xl px-4 py-3 cursor-pointer border border-transparent has-[:checked]:border-cyan-500/40">
                                <input type="checkbox" name="wavelog_only_qso" id="wavelog_only_qso"
                                    """ + ('checked' if config_manager.data.get('wavelog',{}).get('only_qso',True) else '') + """
                                    class="accent-cyan-500">
                                <div>
                                    <div class="text-white text-xs font-bold">💬 QSO / contacts uniquement</div>
                                    <div class="text-slate-500 text-[9px]">Exclut les beacons météo, propagation, telemetrie, objets purs</div>
                                </div>
                            </label>
                        </div>

                        <!-- Compteurs + actions -->
                        <div class="grid grid-cols-2 gap-3">
                            <div class="bg-slate-900/60 rounded-xl px-3 py-2 text-center">
                                <div class="text-slate-500 text-[9px] uppercase">QSO synchronisés</div>
                                <div id="wavelog-synced" class="text-cyan-400 font-black text-lg tabular-nums">0</div>
                            </div>
                            <div class="bg-slate-900/60 rounded-xl px-3 py-2 text-center">
                                <div class="text-slate-500 text-[9px] uppercase">Dernière synchro</div>
                                <div id="wavelog-last-sync" class="text-slate-400 font-mono text-xs mt-1">–</div>
                            </div>
                        </div>
                        <div class="flex gap-2">
                            <button type="button" onclick="wavelogSyncNow()" id="wavelog-sync-btn"
                                class="flex-1 py-2 rounded-xl text-[10px] font-bold uppercase tracking-widest transition-all bg-cyan-900/30 border border-cyan-700/50 text-cyan-300 hover:bg-cyan-800/40">
                                ⚡ Synchroniser maintenant
                            </button>
                            <button type="button" onclick="wavelogResetSync()"
                                class="px-3 py-2 rounded-xl text-[10px] font-bold transition-all bg-slate-800 border border-slate-700 text-slate-400 hover:border-orange-600 hover:text-orange-300"
                                title="Réinitialise le curseur — tout sera re-synchronisé">
                                ↺ Réinitialiser
                            </button>
                        </div>
                        <p class="text-slate-700 text-[9px] text-center">
                            Wavelog open-source · <a href="https://github.com/wavelog/wavelog" target="_blank" class="text-slate-500 hover:text-cyan-400 transition-colors">github.com/wavelog/wavelog</a>
                        </p>
                    </div>

                    <!-- ── Alertes passage ISS ── -->
                    <div class="border-t border-slate-800/60 pt-8">
                        <h3 class="text-[10px] font-black text-violet-400 uppercase tracking-widest mb-6 flex items-center gap-2">
                            🛸 Alertes Passage ISS
                        </h3>
                        <div class="space-y-5">
                            <!-- Toggle + avance -->
                            <div class="flex items-center justify-between">
                                <div>
                                    <div class="text-sm font-bold text-white">Alerte avant passage</div>
                                    <div class="text-[10px] text-slate-500 mt-0.5">Bannière + bip sonore X minutes avant le passage de l'ISS</div>
                                </div>
                                <label class="prox-switch" title="Activer / désactiver l'alerte ISS">
                                    <input type="checkbox" id="iss-alert-toggle" onchange="issAlertToggle()">
                                    <span class="prox-slider" style="--sw-on:#5b21b6"></span>
                                </label>
                            </div>
                            <div class="flex items-center gap-4">
                                <label class="text-[10px] text-slate-500 uppercase font-bold shrink-0">⏱ Avance (minutes)</label>
                                <input id="iss-advance" type="number" min="1" max="30" value="10"
                                    oninput="issAlertSave()"
                                    class="w-24 bg-slate-900 border border-slate-800 rounded-xl p-3 text-white text-sm font-mono outline-none focus:border-violet-500 transition-all text-center">
                            </div>
                            <!-- Prochains passages en aperçu -->
                            <div>
                                <div class="text-[10px] text-slate-500 uppercase font-bold mb-2">📅 Prochains passages</div>
                                <div id="iss-pass-list-cfg" class="bg-slate-900/60 rounded-xl p-3 text-[10px] text-slate-500 italic">
                                    ⏳ Chargement...
                                </div>
                            </div>
                        </div>
                    </div>

                    <button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-500 p-5 rounded-2xl font-black text-white shadow-xl transition-all active:scale-95 mt-6">
                        ✅ APPLIQUER LES MODIFICATIONS
                    </button>
                </form>
            </div>
        </div>

        <!-- ═══════════════════════════════ QSO ═══════════════════════════════ -->
        <div id="tab-qso" class="hidden">
            <div class="grid grid-cols-1 lg:grid-cols-12 gap-8">
                <div class="lg:col-span-4">
                    <div class="glass rounded-[2rem] overflow-hidden shadow-2xl">
                        <div class="px-6 py-4 bg-slate-900/50 border-b border-slate-800 flex justify-between items-center">
                            <span class="text-[10px] font-black text-blue-400 uppercase tracking-widest">
                                📒 Contacts
                            </span>
                            <div class="flex items-center gap-3">
                                <button onclick="openCQGeneral()"
                                        class="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-[10px] font-black transition-all active:scale-95 bg-amber-500/15 border border-amber-500/40 text-amber-300 hover:bg-amber-500/30 hover:text-white"
                                        title="Appel général — destination APRS (broadcast)">
                                    📢 CQ
                                </button>
                                <button onclick="refreshContacts()" class="text-[10px] text-slate-600 hover:text-blue-400 transition-colors">
                                    🔄
                                </button>
                            </div>
                        </div>
                        <div id="contact-list" class="divide-y divide-slate-800/50 max-h-[500px] overflow-y-auto custom-scrollbar">
                            <div class="px-6 py-8 text-center text-slate-600 text-xs italic">📭 Aucun contact</div>
                        </div>
                        <div class="px-6 py-4 border-t border-slate-800 bg-slate-900/30">
                            <div class="flex gap-2">
                                <input id="new-contact-input" type="text" placeholder="Indicatif ex: F4XXX-7"
                                       class="flex-1 bg-slate-950 border border-slate-800 rounded-xl px-3 py-2 text-white text-xs outline-none focus:border-blue-500 font-mono uppercase"
                                       onkeydown="if(event.key==='Enter') openContact(this.value)">
                                <button onclick="openContact(document.getElementById('new-contact-input').value)"
                                        class="bg-blue-600 hover:bg-blue-500 px-4 py-2 rounded-xl text-xs font-black transition-all">
                                    ➕
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="lg:col-span-8">
                    <div class="glass rounded-[2rem] overflow-hidden flex flex-col h-[600px] shadow-2xl">
                        <div class="px-6 py-4 bg-slate-900/50 border-b border-slate-800 flex items-center gap-3">
                            <div id="chat-avatar" class="w-9 h-9 rounded-xl bg-slate-800 flex items-center justify-center text-sm font-black text-blue-400">?</div>
                            <div>
                                <div id="chat-title" class="font-black text-white text-sm">Selectionner un contact</div>
                                <div id="chat-subtitle" class="text-[10px] text-slate-500">APRS Point-a-Point</div>
                            </div>
                        </div>
                        <div id="chat-messages" class="flex-grow overflow-y-auto custom-scrollbar p-6 space-y-3 bg-slate-950/30">
                            <div class="text-center text-slate-600 text-xs italic mt-20">👆 Choisissez un contact pour demarrer</div>
                        </div>
                        <div class="px-6 py-4 border-t border-slate-800 bg-slate-900/30">
                            <!-- ── Macros QSO ── -->
                            <div id="macro-bar" class="flex flex-wrap gap-1.5 mb-3"></div>
                            <div class="flex gap-3">
                                <input id="chat-input" type="text" maxlength="67"
                                       placeholder="Message APRS 67 car. max..."
                                       class="flex-1 bg-slate-950 border border-slate-800 rounded-xl px-4 py-3 text-white text-sm outline-none focus:border-blue-500 transition-all"
                                       onkeydown="if(event.key==='Enter') chatSend()"
                                       disabled>
                                <button id="chat-send-btn" onclick="chatSend()"
                                        class="bg-blue-600 hover:bg-blue-500 px-5 py-3 rounded-xl font-black text-white shadow-lg transition-all active:scale-95"
                                        disabled>
                                    📨
                                </button>
                            </div>
                            <div class="flex justify-between mt-2 px-1">
                                <span id="chat-charcount" class="text-[10px] text-slate-600">0/67</span>
                                <span class="text-[10px] text-slate-600 italic">✅ ACK automatique actif</span>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        <!-- ═══════════════════════════════ MAP ═══════════════════════════════ -->
        <div id="tab-map" class="hidden">
            <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">
                <!-- Carte -->
                <div class="lg:col-span-9">
                    <div class="glass rounded-[2rem] overflow-hidden shadow-2xl">
                        <div class="px-6 py-3 bg-slate-900/50 border-b border-slate-800 flex justify-between items-center">
                            <span class="text-[10px] font-black text-emerald-400 uppercase tracking-widest flex items-center gap-2">
                                🗺️ Carte APRS - Stations recues
                            </span>
                            <div class="flex items-center gap-4">
                                <span class="text-[10px] font-mono text-slate-500">
                                    <span id="map-station-count" class="text-emerald-400 font-bold">0</span> station(s)
                                </span>
                                <button onclick="mapClearAll()" class="text-[10px] text-slate-600 hover:text-red-400 font-bold uppercase tracking-widest transition-colors">
                                    🗑️ Effacer
                                </button>
                                <button onclick="mapFitAll()" class="text-[10px] text-slate-400 hover:text-blue-400 font-bold uppercase tracking-widest transition-colors">
                                    🎯 Centrer
                                </button>
                            </div>
                        </div>
                        <div id="aprs-map"></div>
                    </div>
                </div>
                <!-- Panneau légende + liste stations -->
                <div class="lg:col-span-3 space-y-4">
                    <!-- Légende -->
                    <div class="glass p-5 rounded-[2rem] shadow-xl">
                        <h3 class="text-[10px] font-black text-slate-400 uppercase tracking-widest mb-4 flex items-center gap-2">
                            ℹ️ Légende
                        </h3>
                        <div class="space-y-2.5 text-[11px] font-mono">
                            <div class="flex items-center gap-2">
                                <span class="w-4 h-4 rounded-full bg-blue-500 border-2 border-blue-300 shrink-0 inline-block"></span>
                                <span class="text-slate-300">📤 Ma station (TX)</span>
                            </div>
                            <div class="flex items-center gap-2">
                                <span class="w-4 h-4 rounded-full bg-emerald-500 border-2 border-emerald-300 shrink-0 inline-block"></span>
                                <span class="text-slate-300">📥 Station RX</span>
                            </div>
                            <div class="flex items-center gap-2">
                                <span class="w-4 h-4 rounded-full bg-orange-400 border-2 border-orange-200 shrink-0 inline-block"></span>
                                <span class="text-slate-300">🚗 Mobile / véhicule</span>
                            </div>
                            <div class="flex items-center gap-2">
                                <span class="w-4 h-4 rounded-full bg-purple-500 border-2 border-purple-300 shrink-0 inline-block"></span>
                                <span class="text-slate-300">🌦️ Météo / objet</span>
                            </div>
                            <div class="flex items-center gap-2">
                                <span class="w-4 h-4 rounded-full bg-red-500 border-2 border-red-300 shrink-0 inline-block"></span>
                                <span class="text-slate-300">🚨 Urgence / alertes</span>
                            </div>
                            <div class="border-t border-slate-700 pt-2 mt-2 text-[10px] text-slate-600 italic">
                                💡 Cliquez sur un marqueur pour voir les détails. Les lignes indiquent le chemin digipeater.
                            </div>
                        </div>
                    </div>
                    <!-- Indice de propagation VHF -->
                    <div class="glass rounded-[2rem] overflow-hidden shadow-xl">
                        <div class="px-4 py-3 bg-slate-900/50 border-b border-slate-800 flex items-center justify-between">
                            <span class="text-[10px] font-black text-violet-400 uppercase tracking-widest">📶 Propagation VHF</span>
                            <button onclick="vhfPropRefresh()" id="vhf-refresh-btn" class="text-[9px] text-slate-500 hover:text-violet-300 font-bold uppercase tracking-widest transition-colors">↺ MAJ</button>
                        </div>
                        <div id="vhf-prop-content" class="p-4 space-y-3">
                            <div class="text-center text-slate-600 text-xs italic py-2">⏳ Chargement...</div>
                        </div>
                    </div>
                    <!-- Liste des stations -->
                    <div class="glass rounded-[2rem] overflow-hidden shadow-xl">
                        <div class="px-4 py-3 bg-slate-900/50 border-b border-slate-800">
                            <span class="text-[10px] font-black text-slate-400 uppercase tracking-widest">📡 Stations</span>
                        </div>
                        <div id="map-station-list" class="max-h-[320px] overflow-y-auto custom-scrollbar divide-y divide-slate-800/50">
                            <div class="px-4 py-6 text-center text-slate-600 text-xs italic">⏳ En attente de trames...</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- ── Onglet ISS ───────────────────────────────────────────────── -->
        <div id="tab-iss" class="hidden">
            <div class="grid grid-cols-1 gap-6">

                <!-- Carte OrbTrack -->
                <div class="glass rounded-[2rem] overflow-hidden shadow-2xl">
                    <div class="px-5 py-3 bg-slate-900/50 border-b border-slate-800 flex items-center justify-between">
                        <span class="text-[10px] font-black text-blue-400 uppercase tracking-widest">🛰️ Position ISS temps réel</span>
                        <a href="https://www.orbtrack.org/#" target="_blank"
                           class="text-[10px] text-blue-400 hover:text-blue-300 font-bold uppercase tracking-widest transition-colors">↗ orbtrack.org</a>
                    </div>
                    <iframe id="iss-iframe"
                        src="about:blank"
                        data-src="https://www.orbtrack.org/#"
                        style="width:100%;height:80vh;border:none;background:#020617;"
                        loading="lazy" title="ISS Live Tracking - OrbTrack">
                    </iframe>
                    <div class="px-5 py-2 bg-slate-900/30 border-t border-slate-800 text-center">
                        <span class="text-[10px] text-slate-600 font-mono">145.825 MHz APRS · 437.800 MHz FM</span>
                    </div>
                </div>

            </div>

            <!-- Liens rapides -->
            <div class="mt-4 glass rounded-2xl px-6 py-3 grid grid-cols-2 lg:grid-cols-4 gap-2 text-center">
                <a href="https://aprs.fi/#!call=a%2FRS0ISS&timerange=3600&tail=3600" target="_blank"
                   class="text-[10px] text-slate-400 hover:text-emerald-300 font-bold uppercase tracking-widest transition-colors py-2">
                    🗺️ Trace APRS — aprs.fi
                </a>
                <a href="https://www.heavens-above.com/PassSummary.aspx?satid=25544" target="_blank"
                   class="text-[10px] text-slate-400 hover:text-yellow-300 font-bold uppercase tracking-widest transition-colors py-2">
                    🌟 Heavens-Above
                </a>
                <a href="https://www.n2yo.com/satellite/?s=25544" target="_blank"
                   class="text-[10px] text-slate-400 hover:text-blue-300 font-bold uppercase tracking-widest transition-colors py-2">
                    📡 N2YO Passages
                </a>
                <a href="https://issfanclub.eu/" target="_blank"
                   class="text-[10px] text-slate-400 hover:text-purple-300 font-bold uppercase tracking-widest transition-colors py-2">
                    🚀 ISS Fan Club
                </a>
            </div>
        </div>

        <!-- ══════════════════════════════ STATS ═══════════════════════════════ -->
        <div id="tab-stats" class="hidden">
            <div class="grid grid-cols-1 xl:grid-cols-3 gap-6">

                <!-- Courbe trafic 24h -->
                <div class="xl:col-span-2 glass rounded-[2rem] overflow-hidden shadow-2xl">
                    <div class="px-5 py-3 bg-slate-900/50 border-b border-slate-800 flex items-center justify-between">
                        <span class="text-[10px] font-black text-blue-400 uppercase tracking-widest">📈 Trafic RX / TX / IS — 24h</span>
                        <span id="stats-last-update" class="text-[9px] font-mono text-slate-600">–</span>
                    </div>
                    <div class="p-4">
                        <svg id="stats-chart" viewBox="0 0 800 220" style="width:100%;display:block;" xmlns="http://www.w3.org/2000/svg">
                            <text x="400" y="115" text-anchor="middle" font-size="11" fill="#475569">En attente de données…</text>
                        </svg>
                        <div class="flex gap-4 justify-center mt-2">
                            <span class="text-[10px] font-mono flex items-center gap-1"><span style="display:inline-block;width:14px;height:3px;background:#60a5fa;border-radius:2px"></span>RX</span>
                            <span class="text-[10px] font-mono flex items-center gap-1"><span style="display:inline-block;width:14px;height:3px;background:#f59e0b;border-radius:2px"></span>TX</span>
                            <span class="text-[10px] font-mono flex items-center gap-1"><span style="display:inline-block;width:14px;height:3px;background:#a78bfa;border-radius:2px"></span>IS</span>
                        </div>
                    </div>
                </div>

                <!-- Top 10 stations -->
                <div class="glass rounded-[2rem] overflow-hidden shadow-2xl">
                    <div class="px-5 py-3 bg-slate-900/50 border-b border-slate-800">
                        <span class="text-[10px] font-black text-emerald-400 uppercase tracking-widest">🏆 Top 10 stations entendues</span>
                    </div>
                    <div id="stats-top10" class="p-4 space-y-2">
                        <p class="text-slate-600 text-[10px] italic text-center py-4">En attente de données…</p>
                    </div>
                </div>

                <!-- Compteurs résumé -->
                <div class="xl:col-span-3 grid grid-cols-2 sm:grid-cols-4 gap-4">
                    <div class="glass rounded-2xl p-5 text-center">
                        <div id="stats-total-rx" class="text-3xl font-black text-blue-400">0</div>
                        <div class="text-[10px] font-black text-slate-500 uppercase tracking-widest mt-1">Trames RX</div>
                    </div>
                    <div class="glass rounded-2xl p-5 text-center">
                        <div id="stats-total-tx" class="text-3xl font-black text-amber-400">0</div>
                        <div class="text-[10px] font-black text-slate-500 uppercase tracking-widest mt-1">Trames TX</div>
                    </div>
                    <div class="glass rounded-2xl p-5 text-center">
                        <div id="stats-total-is" class="text-3xl font-black text-violet-400">0</div>
                        <div class="text-[10px] font-black text-slate-500 uppercase tracking-widest mt-1">Trames IS</div>
                    </div>
                    <div class="glass rounded-2xl p-5 text-center">
                        <div id="stats-total-stations" class="text-3xl font-black text-emerald-400">0</div>
                        <div class="text-[10px] font-black text-slate-500 uppercase tracking-widest mt-1">Stations uniques</div>
                    </div>
                </div>

            </div>
        </div>

        <!-- ════════════════════════════ CARNET DE TRAFIC ══════════════════════════ -->
        <div id="tab-logbook" class="hidden space-y-6">

            <!-- ── Barre de stats + actions ── -->
            <div class="glass rounded-[2rem] p-5 flex flex-col lg:flex-row items-start lg:items-center gap-4 justify-between">
                <div class="flex items-center gap-4 flex-wrap">
                    <span class="text-[10px] font-black text-amber-400 uppercase tracking-widest">📓 Carnet de Trafic APRS</span>
                    <div id="lb-stats-badges" class="flex gap-2 flex-wrap text-[10px] font-mono">
                        <span class="bg-slate-800 px-2 py-1 rounded-lg text-slate-400">Total : <span id="lb-stat-total" class="text-white font-bold">0</span></span>
                        <span class="bg-slate-800 px-2 py-1 rounded-lg text-slate-400">RX : <span id="lb-stat-rx" class="text-emerald-400 font-bold">0</span></span>
                        <span class="bg-slate-800 px-2 py-1 rounded-lg text-slate-400">TX : <span id="lb-stat-tx" class="text-blue-400 font-bold">0</span></span>
                        <span class="bg-slate-800 px-2 py-1 rounded-lg text-slate-400">Indicatifs uniques : <span id="lb-stat-calls" class="text-violet-400 font-bold">0</span></span>
                    </div>
                </div>
                <div class="flex gap-2 flex-wrap">
                    <!-- Import -->
                    <label class="cursor-pointer flex items-center gap-1.5 px-3 py-2 rounded-xl text-[10px] font-bold bg-slate-800 border border-slate-700 text-slate-300 hover:bg-slate-700 transition-all">
                        📥 Importer CSV/ADIF
                        <input type="file" accept=".csv,.adi,.adif" onchange="lbImport(this)" class="hidden">
                    </label>
                    <!-- Export CSV -->
                    <button onclick="lbExport('csv')" class="px-3 py-2 rounded-xl text-[10px] font-bold bg-slate-800 border border-slate-700 text-emerald-300 hover:bg-emerald-900/30 transition-all">
                        📤 Export CSV
                    </button>
                    <!-- Export ADIF -->
                    <button onclick="lbExport('adif')" class="px-3 py-2 rounded-xl text-[10px] font-bold bg-slate-800 border border-slate-700 text-amber-300 hover:bg-amber-900/30 transition-all">
                        📤 Export ADIF
                    </button>
                    <!-- Ajouter manuellement -->
                    <button onclick="lbShowAddModal()" class="px-3 py-2 rounded-xl text-[10px] font-bold bg-slate-800 border border-emerald-700/60 text-emerald-300 hover:bg-emerald-900/30 transition-all">
                        ➕ Ajouter contact
                    </button>
                    <!-- Vider -->
                    <button onclick="lbClearAll()" class="px-3 py-2 rounded-xl text-[10px] font-bold bg-slate-800 border border-red-900/40 text-red-400 hover:bg-red-900/30 transition-all">
                        🗑️ Vider
                    </button>
                </div>
            </div>

            <!-- ── Filtres ── -->
            <div class="glass rounded-[2rem] p-4 flex flex-col lg:flex-row gap-3 items-start lg:items-center">
                <div class="flex items-center gap-2 flex-1">
                    <span class="text-[10px] text-slate-500 font-bold uppercase shrink-0">🔍 Recherche</span>
                    <input id="lb-search" type="text" placeholder="Indicatif, commentaire, payload..."
                           class="flex-1 bg-slate-900 border border-slate-800 rounded-xl px-3 py-2 text-white text-xs font-mono outline-none focus:border-blue-500 transition-all"
                           oninput="lbRefresh()">
                </div>
                <div class="flex gap-2 flex-wrap">
                    <select id="lb-dir" onchange="lbRefresh()"
                            class="bg-slate-900 border border-slate-800 rounded-xl px-3 py-2 text-xs text-white outline-none focus:border-blue-500 appearance-none">
                        <option value="">Tous (TX+RX)</option>
                        <option value="RX">📥 RX uniquement</option>
                        <option value="TX">📤 TX uniquement</option>
                    </select>
                    <select id="lb-type" onchange="lbRefresh()"
                            class="bg-slate-900 border border-slate-800 rounded-xl px-3 py-2 text-xs text-white outline-none focus:border-blue-500 appearance-none">
                        <option value="">Tous les types</option>
                        <option value="Position">📍 Position</option>
                        <option value="Message">💬 Message</option>
                        <option value="Mic-E">📱 Mic-E</option>
                        <option value="Meteo">🌦️ Météo</option>
                        <option value="Statut">📢 Statut</option>
                        <option value="Objet">🎯 Objet</option>
                        <option value="Telemetrie">📊 Télémétrie</option>
                        <option value="Beacon">📡 Beacon</option>
                    </select>
                    <span class="text-[10px] text-slate-600 font-mono self-center" id="lb-count-label">0 entrée(s)</span>
                </div>
            </div>

            <!-- ── Tableau du carnet ── -->
            <div class="glass rounded-[2rem] overflow-hidden shadow-2xl">
                <div class="overflow-x-auto">
                    <table class="w-full text-[11px] font-mono">
                        <thead>
                            <tr class="border-b border-slate-800 bg-slate-900/70 text-[9px] font-black text-slate-500 uppercase tracking-widest">
                                <th class="px-3 py-3 text-left">Date/Heure</th>
                                <th class="px-3 py-3 text-left">Dir</th>
                                <th class="px-3 py-3 text-left">Indicatif</th>
                                <th class="px-3 py-3 text-left">Dest</th>
                                <th class="px-3 py-3 text-left">Type</th>
                                <th class="px-3 py-3 text-left">Commentaire</th>
                                <th class="px-3 py-3 text-left">Pos</th>
                                <th class="px-3 py-3 text-left">Src</th>
                                <th class="px-3 py-3 text-left">Note</th>
                                <th class="px-3 py-3 text-center">⚙️</th>
                            </tr>
                        </thead>
                        <tbody id="lb-tbody">
                            <tr><td colspan="10" class="px-6 py-12 text-center text-slate-600 italic">⏳ Chargement...</td></tr>
                        </tbody>
                    </table>
                </div>
                <!-- Pagination -->
                <div class="px-5 py-3 bg-slate-900/50 border-t border-slate-800 flex items-center justify-between gap-3">
                    <button onclick="lbPrevPage()" id="lb-prev" class="px-3 py-1.5 rounded-lg text-[10px] font-bold bg-slate-800 border border-slate-700 text-slate-300 hover:bg-slate-700 disabled:opacity-30 transition-all">◀ Préc.</button>
                    <span id="lb-page-info" class="text-[10px] text-slate-500 font-mono">Page 1</span>
                    <button onclick="lbNextPage()" id="lb-next" class="px-3 py-1.5 rounded-lg text-[10px] font-bold bg-slate-800 border border-slate-700 text-slate-300 hover:bg-slate-700 disabled:opacity-30 transition-all">Suiv. ▶</button>
                </div>
            </div>
        </div>

    </main>
</div>

<!-- ══════════════════════════════════════════════════════════════════════
     Modal — Ajout manuel d'un contact dans le carnet
═══════════════════════════════════════════════════════════════════════ -->
<div id="lb-add-modal" style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(2,6,23,.82);backdrop-filter:blur(4px);"
     onclick="if(event.target===this)lbCloseAddModal()">
    <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:min(96vw,560px);
                background:#0f172a;border:1px solid #334155;border-radius:24px;box-shadow:0 24px 60px #00000080;padding:28px 24px;">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px">
            <span style="font-size:13px;font-weight:900;color:#f59e0b;text-transform:uppercase;letter-spacing:.06em">
                ➕ Ajouter un contact
            </span>
            <button onclick="lbCloseAddModal()" style="color:#64748b;font-size:18px;line-height:1;background:none;border:none;cursor:pointer;padding:2px 6px">✕</button>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:11px;font-family:monospace;">
            <div style="grid-column:1/3">
                <label style="display:block;color:#94a3b8;font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Indicatif *</label>
                <input id="lbAdd-callsign" type="text" placeholder="F4XXX-9" maxlength="20"
                       style="width:100%;background:#020617;border:1px solid #334155;border-radius:10px;padding:9px 12px;color:#fff;font-size:13px;font-family:monospace;outline:none;box-sizing:border-box"
                       oninput="this.value=this.value.toUpperCase()">
            </div>
            <div>
                <label style="display:block;color:#94a3b8;font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Direction</label>
                <select id="lbAdd-direction"
                        style="width:100%;background:#020617;border:1px solid #334155;border-radius:10px;padding:9px 12px;color:#fff;font-size:11px;font-family:monospace;outline:none;appearance:none">
                    <option value="RX">📥 RX — Reçu</option>
                    <option value="TX">📤 TX — Émis</option>
                </select>
            </div>
            <div>
                <label style="display:block;color:#94a3b8;font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Type APRS</label>
                <select id="lbAdd-aprs_type"
                        style="width:100%;background:#020617;border:1px solid #334155;border-radius:10px;padding:9px 12px;color:#fff;font-size:11px;font-family:monospace;outline:none;appearance:none">
                    <option value="Manuel">Manuel</option>
                    <option value="Message">💬 Message</option>
                    <option value="Position">📍 Position</option>
                    <option value="Mic-E">📱 Mic-E</option>
                    <option value="Beacon">📡 Beacon</option>
                    <option value="Statut">📢 Statut</option>
                    <option value="Objet">🎯 Objet</option>
                    <option value="Meteo">🌦️ Météo</option>
                </select>
            </div>
            <div>
                <label style="display:block;color:#94a3b8;font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Date</label>
                <input id="lbAdd-date" type="date"
                       style="width:100%;background:#020617;border:1px solid #334155;border-radius:10px;padding:9px 12px;color:#fff;font-size:11px;font-family:monospace;outline:none;box-sizing:border-box">
            </div>
            <div>
                <label style="display:block;color:#94a3b8;font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Heure UTC</label>
                <input id="lbAdd-time" type="time"
                       style="width:100%;background:#020617;border:1px solid #334155;border-radius:10px;padding:9px 12px;color:#fff;font-size:11px;font-family:monospace;outline:none;box-sizing:border-box">
            </div>
            <div>
                <label style="display:block;color:#94a3b8;font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Fréquence (MHz)</label>
                <input id="lbAdd-freq" type="text" value="144.800" maxlength="20"
                       style="width:100%;background:#020617;border:1px solid #334155;border-radius:10px;padding:9px 12px;color:#fff;font-size:11px;font-family:monospace;outline:none;box-sizing:border-box">
            </div>
            <div>
                <label style="display:block;color:#94a3b8;font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Bande</label>
                <select id="lbAdd-band"
                        style="width:100%;background:#020617;border:1px solid #334155;border-radius:10px;padding:9px 12px;color:#fff;font-size:11px;font-family:monospace;outline:none;appearance:none">
                    <option value="2m">2m (144 MHz)</option>
                    <option value="70cm">70cm (430 MHz)</option>
                    <option value="10m">10m (28 MHz)</option>
                    <option value="6m">6m (50 MHz)</option>
                    <option value="23cm">23cm (1.2 GHz)</option>
                </select>
            </div>
            <div>
                <label style="display:block;color:#94a3b8;font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Mode</label>
                <select id="lbAdd-mode"
                        style="width:100%;background:#020617;border:1px solid #334155;border-radius:10px;padding:9px 12px;color:#fff;font-size:11px;font-family:monospace;outline:none;appearance:none">
                    <option value="APRS">APRS</option>
                    <option value="FM">FM</option>
                    <option value="SSB">SSB</option>
                    <option value="CW">CW</option>
                    <option value="FT8">FT8</option>
                    <option value="JS8">JS8</option>
                </select>
            </div>
            <div style="grid-column:1/3">
                <label style="display:block;color:#94a3b8;font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Commentaire</label>
                <input id="lbAdd-comment" type="text" placeholder="Commentaire ou message échangé..." maxlength="100"
                       style="width:100%;background:#020617;border:1px solid #334155;border-radius:10px;padding:9px 12px;color:#fff;font-size:11px;font-family:monospace;outline:none;box-sizing:border-box">
            </div>
            <div style="grid-column:1/3">
                <label style="display:block;color:#94a3b8;font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Note personnelle</label>
                <input id="lbAdd-note" type="text" placeholder="Mémo, conditions de propagation..." maxlength="200"
                       style="width:100%;background:#020617;border:1px solid #334155;border-radius:10px;padding:9px 12px;color:#fff;font-size:11px;font-family:monospace;outline:none;box-sizing:border-box">
            </div>
        </div>
        <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:20px">
            <button onclick="lbCloseAddModal()"
                    style="padding:9px 18px;border-radius:12px;font-size:10px;font-weight:800;background:#1e293b;border:1px solid #334155;color:#94a3b8;cursor:pointer;text-transform:uppercase;letter-spacing:.05em">
                Annuler
            </button>
            <button id="lbAdd-submit-btn" onclick="lbSubmitAdd()"
                    style="padding:9px 18px;border-radius:12px;font-size:10px;font-weight:800;background:#065f46;border:1px solid #10b981;color:#34d399;cursor:pointer;text-transform:uppercase;letter-spacing:.05em">
                ✅ Enregistrer
            </button>
        </div>
        <div id="lbAdd-error" style="margin-top:10px;font-size:10px;color:#f87171;font-family:monospace;display:none"></div>
    </div>
</div>

<script>
    var txCount = 0;

    // ── Modal ajout manuel logbook ──────────────────────────────────────────
    function lbShowAddModal() {
        var now = new Date();
        var pad = function(n){ return String(n).padStart(2,'0'); };
        document.getElementById('lbAdd-date').value =
            now.getFullYear() + '-' + pad(now.getMonth()+1) + '-' + pad(now.getDate());
        document.getElementById('lbAdd-time').value =
            pad(now.getHours()) + ':' + pad(now.getMinutes());
        document.getElementById('lbAdd-callsign').value = '';
        document.getElementById('lbAdd-comment').value  = '';
        document.getElementById('lbAdd-note').value     = '';
        document.getElementById('lbAdd-freq').value     = '144.800';
        document.getElementById('lbAdd-direction').value  = 'RX';
        document.getElementById('lbAdd-aprs_type').value  = 'Manuel';
        document.getElementById('lbAdd-band').value     = '2m';
        document.getElementById('lbAdd-mode').value     = 'APRS';
        document.getElementById('lbAdd-error').style.display = 'none';
        document.getElementById('lb-add-modal').style.display = 'block';
        setTimeout(function(){ document.getElementById('lbAdd-callsign').focus(); }, 80);
    }

    function lbCloseAddModal() {
        document.getElementById('lb-add-modal').style.display = 'none';
    }

    async function lbSubmitAdd() {
        var cs = (document.getElementById('lbAdd-callsign').value || '').trim().toUpperCase();
        if (!cs) {
            document.getElementById('lbAdd-error').textContent = '⚠️ Indicatif requis.';
            document.getElementById('lbAdd-error').style.display = 'block';
            document.getElementById('lbAdd-callsign').focus();
            return;
        }
        var btn = document.getElementById('lbAdd-submit-btn');
        btn.textContent = '⏳ Enregistrement...';
        btn.disabled = true;
        document.getElementById('lbAdd-error').style.display = 'none';
        var payload = {
            callsign:   cs,
            direction:  document.getElementById('lbAdd-direction').value,
            aprs_type:  document.getElementById('lbAdd-aprs_type').value,
            date:       document.getElementById('lbAdd-date').value,
            time:       document.getElementById('lbAdd-time').value,
            freq:       (document.getElementById('lbAdd-freq').value || '').trim() || '144.800',
            band:       document.getElementById('lbAdd-band').value,
            mode:       document.getElementById('lbAdd-mode').value,
            comment:    document.getElementById('lbAdd-comment').value.trim(),
            note:       document.getElementById('lbAdd-note').value.trim(),
        };
        try {
            var r   = await fetch('/logbook/add', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload),
            });
            var res = await r.json();
            if (res.ok) {
                lbCloseAddModal();
                lbRefresh();
                lbLoadStats();
            } else {
                throw new Error(res.error || 'Erreur inconnue');
            }
        } catch(e) {
            document.getElementById('lbAdd-error').textContent = '❌ ' + e.message;
            document.getElementById('lbAdd-error').style.display = 'block';
        } finally {
            btn.textContent = '✅ Enregistrer';
            btn.disabled = false;
        }
    }

    // ── Navigation ─────────────────────────────────────────────────────────
    // switchTab définie dans <head>

    // ── Helpers ────────────────────────────────────────────────────────────
    function esc(s) {
        return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    function coordLink(lat, lon) {
        var la = lat.toFixed(5), lo = lon.toFixed(5);
        return '<a href="https://www.openstreetmap.org/?mlat=' + la + '&mlon=' + lo + '&zoom=14" target="_blank" class="text-blue-400 hover:text-blue-300 underline font-mono text-[11px]">' + la + ', ' + lo + '</a>';
    }

    function badge(label, value, cls) {
        cls = cls || 'text-slate-300';
        return '<span class="inline-flex items-center gap-1 bg-slate-800 rounded-md px-2 py-0.5 text-[10px] font-mono"><span class="text-slate-500">' + esc(label) + '</span><span class="' + cls + '">' + esc(String(value)) + '</span></span>';
    }

    // ── Coordonnées station locale (depuis la config serveur) ──────────────
    var _stationLat = null, _stationLon = null;
    (function() {
        try {
            var cfg = """ + json.dumps({'maidenhead': config_manager.data.get('maidenhead',''), 'geo_mode': config_manager.data.get('geo_mode','locator'), 'lat_manual': config_manager.data.get('lat_manual',''), 'lon_manual': config_manager.data.get('lon_manual','')}) + """;
            if (cfg.geo_mode === 'coords' && cfg.lat_manual !== '' && cfg.lon_manual !== '') {
                _stationLat = parseFloat(cfg.lat_manual);
                _stationLon = parseFloat(cfg.lon_manual);
            } else if (cfg.maidenhead && cfg.maidenhead.length >= 4) {
                // Maidenhead → lat/lon (centre de la maille)
                var g = cfg.maidenhead.toUpperCase();
                var lon = (g.charCodeAt(0) - 65) * 20 - 180;
                var lat = (g.charCodeAt(1) - 65) * 10 - 90;
                lon += parseInt(g[2]) * 2;
                lat += parseInt(g[3]) * 1;
                if (g.length >= 6) {
                    lon += (g.charCodeAt(4) - 65) * (2/24) + (1/24);
                    lat += (g.charCodeAt(5) - 65) * (1/24) + (0.5/24);
                } else {
                    lon += 1; lat += 0.5;
                }
                _stationLat = lat; _stationLon = lon;
            }
        } catch(ex) {}
    })();

    // callsign → distKm calculée depuis une trame de position propre (pas Objet)
    var _stationPosDistKm = {};

    function haversineKm(lat1, lon1, lat2, lon2) {
        var R = 6371;
        var dLat = (lat2 - lat1) * Math.PI / 180;
        var dLon = (lon2 - lon1) * Math.PI / 180;
        var a = Math.sin(dLat/2) * Math.sin(dLat/2)
              + Math.cos(lat1 * Math.PI/180) * Math.cos(lat2 * Math.PI/180)
              * Math.sin(dLon/2) * Math.sin(dLon/2);
        return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
    }

    function buildFields(frame) {
        var e = frame.extra || {};
        var parts = [];
        if (e.lat !== undefined && e.lon !== undefined) {
            parts.push('<span class="inline-flex items-center gap-1 bg-slate-800 rounded-md px-2 py-0.5 text-[10px] font-mono"><span class="text-slate-500">pos</span>' + coordLink(e.lat, e.lon) + '</span>');
            if (_stationLat !== null && _stationLon !== null) {
                var _isObj = (frame.aprs_type === 'Objet');
                var _src   = (frame.src || '').toUpperCase();
                var _dist;
                if (_isObj && _stationPosDistKm[_src] !== undefined) {
                    // Objet iGaté : afficher la distance de la station émettrice
                    _dist = _stationPosDistKm[_src];
                } else {
                    _dist = haversineKm(_stationLat, _stationLon, e.lat, e.lon);
                    // Mémoriser si c'est une trame de position propre
                    if (!_isObj) _stationPosDistKm[_src] = _dist;
                }
                var _distStr = _dist < 10 ? _dist.toFixed(1) + ' km' : Math.round(_dist) + ' km';
                var _distCls = _dist < 50 ? 'text-emerald-300' : _dist < 150 ? 'text-yellow-300' : 'text-orange-300';
                var _distBadge = badge('📏', _distStr, _distCls);
                if (_isObj && _stationPosDistKm[_src] !== undefined) {
                    _distBadge = badge('📏', _distStr + ' ·sta', _distCls);
                }
                parts.push(_distBadge);
            }
        }
        if (e.symbol) parts.push(badge('sym', e.symbol));
        if (e.mice_status) parts.push(badge('📱', e.mice_status, 'text-fuchsia-300'));
        if (e.speed_kt !== undefined) {
            var _card16b = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSO","SO","OSO","O","ONO","NO","NNO"];
            var capCard = e.course !== undefined ? _card16b[Math.round((e.course % 360) / 22.5) % 16] : '?';
            parts.push(badge('🧭', capCard + ' ' + e.course + '°', 'text-yellow-300'));
            parts.push(badge('🏎️', e.speed_kmh + ' km/h', 'text-yellow-300'));
        }
        if (e.alt_m !== undefined) parts.push(badge('alt', e.alt_m + ' m', 'text-cyan-300'));
        if (e.phg_power_w !== undefined)
            parts.push(badge('📡 PHG', e.phg_power_w+'W · '+e.phg_height_m+'m · '+e.phg_gain_db+'dB · '+e.phg_dir, 'text-orange-300'));
        if (e.rng_km !== undefined)
            parts.push(badge('📶 RNG', e.rng_km+' km', 'text-orange-300'));
        if (e.msg_dest) parts.push(badge('vers', e.msg_dest, 'text-indigo-300'));
        if (e.msg_text) parts.push('<span class="text-slate-200 text-xs italic">' + esc(e.msg_text) + '</span>');

        // ── Champs météo ──────────────────────────────────────────────────
        var isWx = (frame.aprs_type === 'Meteo' || frame.aprs_type === 'Meteo Peet');
        if (e.temp_c        !== undefined) parts.push(badge('🌡️', e.temp_c.toFixed(1) + ' °C', 'text-orange-300'));
        if (e.humidity_pct  !== undefined) parts.push(badge('💧', e.humidity_pct + ' %', 'text-cyan-300'));
        var _card16 = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSO","SO","OSO","O","ONO","NO","NNO"];
        function degToCard(d) { return _card16[Math.round((d % 360) / 22.5) % 16]; }
        if (e.wind_dir !== undefined && e.wind_speed_kmh !== undefined)
            parts.push(badge('💨', degToCard(e.wind_dir) + ' ' + e.wind_speed_kmh + ' km/h', 'text-sky-300'));
        else if (e.wind_dir !== undefined && e.wind_speed_ms !== undefined)
            parts.push(badge('💨', degToCard(e.wind_dir) + ' ' + (e.wind_speed_ms * 3.6).toFixed(1) + ' km/h', 'text-sky-300'));
        else if (e.wind_dir !== undefined && e.wind_speed_kt !== undefined)
            parts.push(badge('💨', degToCard(e.wind_dir) + ' ' + (e.wind_speed_kt * 1.852).toFixed(1) + ' km/h', 'text-sky-300'));
        if (e.gust_kmh !== undefined) parts.push(badge('🌬️', 'Raf. ' + e.gust_kmh + ' km/h', 'text-sky-200'));
        else if (e.gust_ms !== undefined) parts.push(badge('🌬️', 'Raf. ' + (e.gust_ms * 3.6).toFixed(1) + ' km/h', 'text-sky-200'));
        else if (e.gust_kt !== undefined) parts.push(badge('🌬️', 'Raf. ' + (e.gust_kt * 1.852).toFixed(1) + ' km/h', 'text-sky-200'));
        if (e.rain_1h_mm    !== undefined) parts.push(badge('🌧️', e.rain_1h_mm + ' mm/h', 'text-blue-300'));
        if (e.rain_24h_mm   !== undefined && e.rain_24h_mm !== e.rain_1h_mm)
            parts.push(badge('🌧️', e.rain_24h_mm + ' mm/24h', 'text-blue-400'));
        if (e.pressure_hpa  !== undefined) parts.push(badge('🔵', e.pressure_hpa + ' hPa', 'text-violet-300'));
        if (e.luminosity_wm2 !== undefined) parts.push(badge('☀️', e.luminosity_wm2 + ' W/m²', 'text-yellow-300'));
        if (e.snow_24h_cm   !== undefined && e.snow_24h_cm > 0)
            parts.push(badge('❄️', e.snow_24h_cm + ' cm', 'text-slate-200'));
        // ── Champs télémétrie ─────────────────────────────────────────────
        if (e.telem_seq !== undefined) {
            parts.push(badge('📊', '#' + e.telem_seq, 'text-violet-400'));

            // Canaux analogiques : badge compact "label: valeur unité ▌░░░"
            var names = e.telem_names || [];
            var units = e.telem_units || [];
            if (e.telem_analog && e.telem_analog.length) {
                e.telem_analog.forEach(function(v, i) {
                    if (v === null || v === undefined) return;
                    var label = names[i] || ('A' + (i + 1));
                    var unit  = units[i] && units[i] !== 'None' ? ' ' + units[i] : '';
                    var disp  = (v === Math.floor(v)) ? String(v) : v.toFixed(2);
                    var raw   = (e.telem_analog_raw && e.telem_analog_raw[i] != null) ? e.telem_analog_raw[i] : v;
                    var pct   = Math.min(100, Math.round((raw / 255) * 100));
                    var bar   = '<span style="display:inline-block;width:24px;height:3px;background:#1e293b;border-radius:2px;vertical-align:middle;margin-left:3px">'
                              + '<span style="display:block;width:' + pct + '%;height:100%;background:#22d3ee;border-radius:2px"></span></span>';
                    parts.push('<span class="inline-flex items-center gap-1 bg-slate-800/70 rounded px-1.5 py-0.5 text-[10px] font-mono text-cyan-300">'
                             + '<span class="text-slate-400" style="font-size:9px">' + esc(label) + '</span>'
                             + '<span class="font-bold">' + esc(disp) + esc(unit) + '</span>'
                             + bar + '</span>');
                });
            }

            // Bits : regroupés en un seul badge compact
            if (e.telem_named_bits && e.telem_named_bits.length) {
                var bitsHtml = e.telem_named_bits.map(function(b) {
                    var col = b.val ? 'color:#34d399' : 'color:#475569';
                    return '<span style="' + col + '">' + (b.val ? '●' : '○') + '\u202f' + esc(b.name) + '</span>';
                }).join('<span style="color:#334155"> · </span>');
                parts.push('<span class="inline-flex items-center bg-slate-800/70 rounded px-1.5 py-0.5 text-[10px] font-mono" style="gap:2px">'
                         + bitsHtml + '</span>');
            } else if (e.telem_bits) {
                parts.push(badge('bits', e.telem_bits, 'text-amber-300'));
            }
        }
        // ── Objet APRS ───────────────────────────────────────────────────
        if (e.obj_name) parts.push(badge('objet', e.obj_name, 'text-amber-300'));

        // ─────────────────────────────────────────────────────────────────

        var hasWxFields = (e.temp_c !== undefined || e.humidity_pct !== undefined || e.wind_dir !== undefined || e.pressure_hpa !== undefined);

        // ── Détecte si le commentaire ressemble à un relais radio ──────────
        function _parseRelayComment(com, parts) {
            var stripped = com;
            var hit = false;

            // Fréquence : 145.7125 MHz  /  144.937rx  /  145.7125FM
            var mFreq = stripped.match(/(\d{2,3}[.,]\d{3,4})\s*(rx|tx)?\s*MHz/i)
                     || stripped.match(/(\d{2,3}[.,]\d{3,4})\s*(FM|AM|SSB|C4FM|DMR|DSTAR|D-STAR|FUSION|YSF|APCO|P25|NXDN|TETRA|EchoLink|rx|tx)/i);
            if (mFreq) {
                parts.push(badge('📻', mFreq[1].replace(',','.') + ' MHz', 'text-emerald-300'));
                stripped = stripped.replace(mFreq[0], ' ');
                hit = true;
            }

            // Mode : FM, C4FM, DMR, D-STAR, FUSION, SSB, AM…
            var mMode = stripped.match(/\b(C4FM|DSTAR|D-STAR|DMR|FUSION|YSF|APCO|P25|NXDN|TETRA|EchoLink|SSB|AM|FM)\b/i);
            if (mMode) {
                parts.push(badge('mode', mMode[1].toUpperCase(), 'text-cyan-300'));
                stripped = stripped.replace(mMode[0], ' ');
                hit = true;
            }

            // CTCSS texte libre : "ctcss 94.8Hz" / "ctcss 94.8" / "tone 94.8"
            var mCtcssFull = stripped.match(/(?:ctcss|tone|pl)\s*([0-9]+(?:[.,][0-9]+)?)\s*Hz?/i);
            if (mCtcssFull) {
                parts.push(badge('CTCSS', parseFloat(mCtcssFull[1].replace(',','.')).toFixed(1) + ' Hz', 'text-violet-300'));
                stripped = stripped.replace(mCtcssFull[0], ' ');
                hit = true;
            } else {
                // Format compact APRS : T094 (dixièmes de Hz)
                var mTone = stripped.match(/\bT(\d{3,4})\b/);
                if (mTone) {
                    parts.push(badge('CTCSS', (parseInt(mTone[1])/10).toFixed(1) + ' Hz', 'text-violet-300'));
                    stripped = stripped.replace(mTone[0], ' ');
                    hit = true;
                }
            }

            // DCS
            var mDcs = stripped.match(/\bC(\d{3})\b/);
            if (mDcs) {
                parts.push(badge('DCS', mDcs[1], 'text-violet-300'));
                stripped = stripped.replace(mDcs[0], ' ');
                hit = true;
            }

            // Offset (+/- kHz ou MHz) : +600 / -600 / +0.6
            var mOff = stripped.match(/([+\-])(\d{1,4}(?:[.,]\d+)?)\s*(?:kHz|k)?\b(?!\s*MHz)/i);
            if (mOff) {
                var offVal = parseFloat(mOff[2].replace(',','.')) * (mOff[1] === '+' ? 1 : -1);
                var offStr = (offVal >= 0 ? '+' : '') + (Math.abs(offVal) >= 1 ? offVal.toFixed(0) : offVal.toFixed(1)) + ' kHz';
                parts.push(badge('offset', offStr, 'text-sky-300'));
                stripped = stripped.replace(mOff[0], ' ');
                hit = true;
            }

            // Portée : R25k / R25km / R10 / R4X (X = miles)
            var mRange = stripped.match(/\bR(\d+)\s*(k(?:m)?|m|X)?\b/i);
            if (mRange) {
                var rUnit = (mRange[2] || '').toLowerCase();
                var rStr  = mRange[1] + (rUnit === 'x' ? ' mi' : rUnit ? ' km' : ' km');
                parts.push(badge('📶', rStr, 'text-yellow-300'));
                stripped = stripped.replace(mRange[0], ' ');
                hit = true;
            }

            // Opérateur : "by F4KOA" / "via F4KOA"
            var mBy = stripped.match(/\b(?:by|via)\s+([A-Z0-9]{3,8}(?:-\d{1,2})?)\b/i);
            if (mBy) {
                parts.push(badge('by', mBy[1].toUpperCase(), 'text-slate-400'));
                stripped = stripped.replace(mBy[0], ' ');
                hit = true;
            }

            // Texte libre restant (QTH, nom du relais…)
            var txt = stripped
                .replace(/\b\d+\.\s*/g, '')   // numéros résiduels type "36."
                .replace(/\s{2,}/g, ' ')
                .trim();
            if (txt) parts.push('<span class="text-slate-300 text-xs">' + esc(txt) + '</span>');

            return hit;
        }

        // Détecte si le commentaire contient une fréquence → parsing relais
        function _looksLikeRelay(com) {
            return /\d{2,3}[.,]\d{3,4}\s*(MHz|rx|tx|FM|C4FM|DMR|DSTAR|D-STAR|FUSION)?/i.test(com)
                || /\b(?:ctcss|tone|pl)\s*\d/i.test(com)
                || /\bR\d+\s*k/i.test(com);
        }

        if (e.comment && e.comment.length > 0 && !isWx) {
            var _isRelayFrame = (frame.aprs_type === 'Objet') || _looksLikeRelay(e.comment);
            if (_isRelayFrame) {
                _parseRelayComment(e.comment, parts);
            } else {
                parts.push('<span class="text-slate-400 text-xs">' + esc(e.comment) + '</span>');
            }
        } else if (e.comment && isWx && !hasWxFields)
            parts.push('<span class="text-slate-400 text-xs italic">' + esc(e.comment) + '</span>');
        return parts.length ? '<div class="flex flex-wrap gap-1.5 mt-2">' + parts.join('') + '</div>' : '';
    }

    function aprsTypeEmoji(t) {
        var map = {
            'Position': '📍', 'Position+Msg': '📍💬', 'Position+TS': '📍🕐',
            'Position+TS+Msg': '📍🕐💬', 'Message': '💬', 'Statut': '📢',
            'Objet': '🎯', 'Mic-E': '📱', 'NMEA': '🛰️', 'Telemetrie': '📊',
            'Meteo': '🌦️', 'Meteo Peet': '🌧️', 'Beacon': '📡', 'Beacon ISS': '🛸',
            'TX': '📤', 'ACK': '✅', 'Raw': '📄'
        };
        for (var k in map) { if (t && t.indexOf(k) !== -1) return map[k]; }
        return '📻';
    }

    // ── Lien QRZ.com ─────────────────────────────────────────────────────────
    function qrzLink(cs, opts) {
        if (!cs || cs === '?' || cs === 'APRS' || cs === 'BEACON') return esc(cs);
        var o = opts || {};
        var cls   = o.cls   || 'text-[10px] bg-slate-800 rounded px-1.5 py-0.5 font-mono text-slate-300 hover:text-blue-300 hover:bg-slate-700 transition-colors';
        var style = o.style || '';
        var base  = cs.split('-')[0];   // sans SSID pour QRZ
        var href  = 'https://www.qrz.com/db/' + encodeURIComponent(base);
        return '<a href="' + href + '" target="_blank" rel="noopener" title="Fiche QRZ · ' + esc(cs) + '"'
             + ' class="' + cls + '"'
             + (style ? ' style="' + style + '"' : '')
             + ' onclick="event.stopPropagation()">'
             + esc(cs) + '</a>';
    }

    function addLog(type, frame) {
        var con = document.getElementById('console');
        var div = document.createElement('div');
        var timeStr = new Date().toLocaleTimeString();
        var isTX    = type === 'TX';
        var isIS    = frame._source === 'IS';
        var borderColor = isTX ? '#3b82f6' : isIS ? '#8b5cf6' : '#10b981';
        var color       = isTX ? 'text-blue-400' : isIS ? 'text-violet-400' : 'text-emerald-400';
        var dirLabel    = isTX ? '📤 TX' : isIS ? '🌐 IS' : '📥 RX';
        var fieldsHtml = buildFields(frame);
        var rawLine = (frame.payload && frame.aprs_type !== 'Telemetrie')
            ? '<div class="mt-1.5 font-mono text-[10px] text-slate-600 truncate">' + esc(frame.payload.substring(0,120)) + '</div>'
            : '';
        div.style.borderLeftColor = borderColor;
        div.className = "rx-entry bg-slate-900/40 p-4 rounded-2xl border border-slate-800/50";
        div.innerHTML = '<div class="flex justify-between items-start">'
            + '<div class="flex items-center gap-2 flex-wrap">'
            + '<span class="text-[10px] font-black ' + color + ' uppercase tracking-tighter">' + dirLabel + ' &bull; ' + timeStr + '</span>'
            + qrzLink(frame.src || '?')
            + '<span class="text-slate-600 text-[10px]">&#9658;</span>'
            + '<span class="text-[10px] bg-slate-800 rounded px-1.5 py-0.5 font-mono text-slate-400">' + esc(frame.dest || '?') + '</span>'
            + (frame.path ? '<span class="text-[9px] text-slate-600 font-mono">' + esc(frame.path) + '</span>' : '')
            + '</div>'
            + '<span class="text-[9px] font-bold text-slate-600 uppercase tracking-widest shrink-0 ml-2">' + aprsTypeEmoji(frame.aprs_type) + ' ' + esc(frame.aprs_type || '') + '</span>'
            + '</div>'
            + fieldsHtml + rawLine;
        con.prepend(div);
    }

    // ── Restauration de l'historique après F5 ─────────────────────────────
    var rxFrameCount = 0;
    var _seenFids = new Set();   // fids déjà affichés via rx_history

    // ══════════════════════════════════════════════════════════════════════
    // 📊 STATISTIQUES — défini avant fetch('/rx_history') et EventSource
    // ══════════════════════════════════════════════════════════════════════
    (function() {
        var BUCKET_MIN  = 5;                          // résolution : 5 min
        var N_BUCKETS   = (24 * 60) / BUCKET_MIN;     // 288 buckets
        var _buckets    = [];                          // [{rx,tx,is,t}, …]
        var _stations   = {};                          // { callsign: {rx,tx,is} }
        var _totalRx = 0, _totalTx = 0, _totalIs = 0;
        var _statsDirty = false;
    
        function _bucketIdx() {
            var now = new Date();
            return Math.floor((now.getHours() * 60 + now.getMinutes()) / BUCKET_MIN);
        }
    
        function _ensureBuckets() {
            var now   = Date.now();
            var idx   = _bucketIdx();
            if (!_buckets[idx]) _buckets[idx] = {rx:0, tx:0, is:0, t: now};
            // Réinitialiser les buckets si leur timestamp date de > 24h
            for (var i = 0; i < N_BUCKETS; i++) {
                if (_buckets[i] && (now - _buckets[i].t) > 24 * 3600 * 1000) {
                    _buckets[i] = null;
                }
            }
        }
    
        // Appelé depuis le handler SSE
        window._statsRecord = function(type, frame) {
            _ensureBuckets();
            var idx = _bucketIdx();
            if (!_buckets[idx]) _buckets[idx] = {rx:0, tx:0, is:0, t: Date.now()};
            var src = (frame && frame.src) ? frame.src.toUpperCase() : null;
            function _setTxt(id, val) { var el=document.getElementById(id); if(el) el.textContent=val; }
            if (type === 'TX') {
                _buckets[idx].tx++;
                _totalTx++;
                _setTxt('stats-total-tx', _totalTx);
            } else if (type === 'IS') {
                _buckets[idx].is++;
                _totalIs++;
                _setTxt('stats-total-is', _totalIs);
                if (src) { if (!_stations[src]) _stations[src]={rx:0,tx:0,is:0}; _stations[src].is++; }
            } else {
                _buckets[idx].rx++;
                _totalRx++;
                _setTxt('stats-total-rx', _totalRx);
                if (src) { if (!_stations[src]) _stations[src]={rx:0,tx:0,is:0}; _stations[src].rx++; }
            }
            var uniq = Object.keys(_stations).length;
            _setTxt('stats-total-stations', uniq);
            // Mise à jour différée (pas à chaque trame pour les perfs)
            _statsDirty = true;
            if (window._statsRenderTimer) clearTimeout(window._statsRenderTimer);
            window._statsRenderTimer = setTimeout(_statsRender, 800);
        };
    
        // ── Rendu ─────────────────────────────────────────────────────────
        function _statsRender() {
            _renderChart();
            _renderTop10();
            var el = document.getElementById('stats-last-update');
            if (el) el.textContent = 'màj ' + new Date().toLocaleTimeString();
        }

        // ── Persistance ──────────────────────────────────────────────────────
        function _statsSerialize() {
            var bSave = {};
            for (var i = 0; i < N_BUCKETS; i++) {
                if (_buckets[i]) bSave[i] = _buckets[i];
            }
            return {buckets: bSave, stations: _stations,
                    totalRx: _totalRx, totalTx: _totalTx, totalIs: _totalIs,
                    savedAt: Date.now()};
        }

        function _statsSave() {
            if (!_statsDirty) return;
            _statsDirty = false;
            fetch('/stats/save', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(_statsSerialize())
            }).catch(function(){});
        }

        function _statsLoadFromServer() {
            fetch('/stats/load').then(function(r){return r.json();}).then(function(d) {
                if (!d || !d.buckets) return;
                var cutoff = Date.now() - 24 * 3600 * 1000;
                for (var i in d.buckets) {
                    var b = d.buckets[i];
                    if (b && b.t && b.t > cutoff) _buckets[parseInt(i)] = b;
                }
                if (d.stations) _stations = d.stations;
                function _s(id,v){var e=document.getElementById(id);if(e)e.textContent=v;}
                if (d.totalRx) { _totalRx = d.totalRx; _s('stats-total-rx', _totalRx); }
                if (d.totalTx) { _totalTx = d.totalTx; _s('stats-total-tx', _totalTx); }
                if (d.totalIs) { _totalIs = d.totalIs; _s('stats-total-is', _totalIs); }
                _s('stats-total-stations', Object.keys(_stations).length);
                _statsRender();
            }).catch(function(){});
        }

        // Sauvegarde toutes les 2 min si données modifiées + à la fermeture de page
        setInterval(_statsSave, 2 * 60 * 1000);
        window.addEventListener('beforeunload', _statsSave);
        // Chargement initial
        _statsLoadFromServer();
    
        function _renderChart() {
            var svg = document.getElementById('stats-chart');
            if (!svg) return;
    
            // Agréger les 288 buckets de 5 min en 48 slots de 30 min
            // ordonnés chronologiquement (le plus ancien à gauche)
            var SLOTS    = 48;
            var PER_SLOT = N_BUCKETS / SLOTS;   // 6 buckets par slot
            var idx0     = _bucketIdx();         // bucket actuel = le plus récent
    
            var rxA = [], txA = [], isA = [];
            var maxVal = 1;
            for (var s = 0; s < SLOTS; s++) {
                var rx = 0, tx = 0, is = 0;
                for (var k = 0; k < PER_SLOT; k++) {
                    // slot 0 = le plus ancien, slot 47 = le plus récent
                    var bucketAge = (SLOTS - 1 - s) * PER_SLOT + (PER_SLOT - 1 - k);
                    var bi = (idx0 - bucketAge + N_BUCKETS * 10) % N_BUCKETS;
                    var b  = _buckets[bi];
                    if (b) { rx += b.rx; tx += b.tx; is += b.is; }
                }
                rxA.push(rx); txA.push(tx); isA.push(is);
                if (rx > maxVal) maxVal = rx;
                if (tx > maxVal) maxVal = tx;
                if (is > maxVal) maxVal = is;
            }
    
            // Dimensions SVG
            var W = 800, H = 220, PL = 36, PR = 6, PT = 12, PB = 28;
            var cW = W - PL - PR, cH = H - PT - PB;
            var slotW = cW / SLOTS;
            var barW  = Math.max(1, slotW - 1.5);
    
            var out = '<rect x="0" y="0" width="' + W + '" height="' + H + '" fill="none"/>';
    
            // Grille horizontale + axe Y
            for (var step = 0; step <= 4; step++) {
                var yv  = Math.round((step / 4) * maxVal);
                var ypy = PT + cH - (step / 4) * cH;
                out += '<line x1="' + PL + '" y1="' + ypy.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + ypy.toFixed(1) + '" stroke="#1e293b" stroke-width="1" stroke-dasharray="2,4"/>';
                out += '<text x="' + (PL - 4) + '" y="' + (ypy + 3).toFixed(1) + '" text-anchor="end" font-size="8" fill="#475569">' + yv + '</text>';
            }
    
            // Axe X : étiquettes toutes les 4h (= 8 slots)
            var now = new Date();
            for (var si = 0; si < SLOTS; si += 8) {
                var xTick  = PL + (si + 0.5) * slotW;
                var hAgo   = (SLOTS - 1 - si) / 2;   // en heures avant maintenant
                var hLabel = (now.getHours() - Math.round(hAgo) + 24) % 24;
                out += '<line x1="' + xTick.toFixed(1) + '" y1="' + PT + '" x2="' + xTick.toFixed(1) + '" y2="' + (PT + cH) + '" stroke="#1e293b" stroke-width="1"/>';
                out += '<text x="' + xTick.toFixed(1) + '" y="' + (PT + cH + 14) + '" text-anchor="middle" font-size="8" fill="#475569">' + String(hLabel).padStart(2,'0') + 'h</text>';
            }
    
            // Barres empilées RX / IS / TX par slot
            for (var si = 0; si < SLOTS; si++) {
                var x   = PL + si * slotW + (slotW - barW) / 2;
                var rx  = rxA[si], tx = txA[si], is = isA[si];
                var tot = rx + tx + is;
                if (tot === 0) continue;
    
                var yBase = PT + cH;
                function bar(val, color) {
                    if (!val) return '';
                    var bH = Math.max(1, (val / maxVal) * cH);
                    yBase -= bH;
                    return '<rect x="' + x.toFixed(1) + '" y="' + yBase.toFixed(1) + '" width="' + barW.toFixed(1) + '" height="' + bH.toFixed(1) + '" fill="' + color + '" rx="1"/>';
                }
                out += bar(tx, '#f59e0b');
                out += bar(is, '#a78bfa');
                out += bar(rx, '#3b82f6');
            }
    
            // Ligne de base
            out += '<line x1="' + PL + '" y1="' + (PT + cH) + '" x2="' + (W - PR) + '" y2="' + (PT + cH) + '" stroke="#334155" stroke-width="1"/>';
    
            svg.innerHTML = out;
        }
    
        function _renderTop10() {
            var el = document.getElementById('stats-top10');
            if (!el) return;
            var entries = Object.keys(_stations).map(function(cs) {
                var s = _stations[cs];
                return {cs: cs, total: s.rx + s.tx + s.is, rx: s.rx, is: s.is};
            });
            entries.sort(function(a,b){ return b.total - a.total; });
            var top = entries.slice(0, 10);
            if (!top.length) { el.innerHTML = '<p class="text-slate-600 text-[10px] italic text-center py-4">Aucune donnée</p>'; return; }
            var max = top[0].total || 1;
            el.innerHTML = top.map(function(e, i) {
                var pct = Math.round((e.total / max) * 100);
                var medal = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : (i + 1) + '.';
                var isTag = e.is ? '<span style="font-size:8px;color:#a78bfa;margin-left:4px">IS</span>' : '';
                return '<div class="space-y-0.5">'
                     + '<div class="flex items-center justify-between">'
                     + '<span class="text-[10px] font-mono font-bold text-slate-300">' + medal + ' ' + qrzLink(e.cs, {cls:'font-mono font-bold text-slate-300 hover:text-blue-300 transition-colors'}) + isTag + '</span>'
                     + '<span class="text-[9px] font-mono text-slate-500">' + e.total + '</span>'
                     + '</div>'
                     + '<div style="height:4px;background:#1e293b;border-radius:2px;overflow:hidden">'
                     + '<div style="height:100%;width:' + pct + '%;background:linear-gradient(90deg,#3b82f6,#60a5fa);border-radius:2px;transition:width .4s"></div>'
                     + '</div></div>';
            }).join('');
        }
    
        // Brancher sur l'ouverture de l'onglet via event
        document.addEventListener('aprs-switchtab', function(e) {
            if (e.detail === 'stats') setTimeout(_statsRender, 50);
        });
    })();


    fetch('/rx_history')
        .then(function(r) { return r.json(); })
        .then(function(frames) {
            frames.forEach(function(data) {
                if (data._fid) _seenFids.add(data._fid);
                if (data.type === 'tx_event') {
                    txCount++;
                    if (window._statsRecord) _statsRecord('TX', data);
                    addLog('TX', data);
                } else if (data.type !== 'rx_level' && data.type !== 'connected') {
                    rxFrameCount++;
                    if (window._statsRecord) _statsRecord(data._source === 'IS' ? 'IS' : 'RX', data);
                    addLog('RX', data);
                    document.dispatchEvent(new CustomEvent('aprs-frame', {detail: data}));
                }
            });
            document.getElementById('rx-count').textContent = rxFrameCount;
            document.getElementById('tx-count').innerText   = txCount;
            if (frames.length > 0) {
                var banner = document.createElement('div');
                banner.className = 'text-center text-slate-600 text-[10px] py-2 italic';
                banner.textContent = "— " + frames.length + " trame(s) restauree(s) depuis l'historique —";
                var con = document.getElementById('console');
                con.appendChild(banner);
            }
        })
        .catch(function() {});

    // ── SSE ────────────────────────────────────────────────────────────────
    var evtSource = new EventSource('/rx_stream');

    evtSource.onmessage = function(e) {
        try {
            var data = JSON.parse(e.data);
            // Ignorer les trames déjà affichées via rx_history (dédoublonnage)
            if (data._fid && _seenFids.has(data._fid)) return;
            if (data._fid) _seenFids.add(data._fid);
            if (data.type === 'rx_level') {
                var pct = Math.round(data.level * 100);
                document.getElementById('rx-bar').style.width = pct + '%';
                document.getElementById('rx-level-pct').textContent = pct + '%';
                var led = document.getElementById('rx-led');
                var txt = document.getElementById('rx-status-text');
                if (pct > 5) {
                    led.style.background = '#34d399';
                    txt.textContent = '🟢 Signal detecte';
                    txt.className = 'text-emerald-400';
                } else {
                    led.style.background = '#475569';
                    txt.textContent = '🔇 Silencieux';
                    txt.className = 'text-slate-500';
                }
            } else if (data.type === 'tx_event') {
                txCount++;
                document.getElementById('tx-count').innerText = txCount;
                document.getElementById('last-tx').innerText = new Date().toLocaleTimeString();
                document.getElementById('rx-led').style.background = '#f59e0b';
                setTimeout(function() { document.getElementById('rx-led').style.background = '#475569'; }, 600);

                // ── PTT ON AIR : allumage rouge + clignotement ─────────────
                var dot   = document.getElementById('ptt-dot');
                var lbl   = document.getElementById('ptt-label');
                var ind   = document.getElementById('ptt-indicator');
                if (dot && lbl && ind) {
                    // ON : rouge vif + halo + fond sombre
                    dot.style.background  = '#ef4444';
                    dot.style.border      = '2px solid #fca5a5';
                    dot.style.boxShadow   = '0 0 8px 3px #ef444488';
                    lbl.textContent       = '● ON AIR';
                    lbl.style.color       = '#ef4444';
                    ind.style.background  = '#450a0a80';
                    ind.style.borderRadius= '0.75rem';

                    // Clignotement 3× pendant la TX
                    var blinks = 0;
                    var blink  = setInterval(function() {
                        blinks++;
                        dot.style.opacity = (blinks % 2 === 0) ? '1' : '0.3';
                        if (blinks >= 6) clearInterval(blink);
                    }, 250);

                    // OFF après 3 s (durée typique d'une trame APRS)
                    if (window._pttTimer) clearTimeout(window._pttTimer);
                    window._pttTimer = setTimeout(function() {
                        dot.style.background  = '#1e293b';
                        dot.style.border      = '2px solid #334155';
                        dot.style.boxShadow   = 'none';
                        dot.style.opacity     = '1';
                        lbl.textContent       = 'PTT';
                        lbl.style.color       = '#334155';
                        ind.style.background  = 'transparent';
                    }, 3000);
                }
                if (window._statsRecord) _statsRecord('TX', data);
                addLog('TX', data);
            } else if (data.type === 'iss_pass_alert') {
                _issPassAlertShow(data);
            } else if (data.type !== 'connected') {
                rxFrameCount++;
                document.getElementById('rx-count').textContent = rxFrameCount;
                document.getElementById('rx-led').style.background = '#60a5fa';
                setTimeout(function() { document.getElementById('rx-led').style.background = '#34d399'; }, 400);
                if (window._statsRecord) _statsRecord(data._source === 'IS' ? 'IS' : 'RX', data);
                addLog('RX', data);
                // Dispatcher vers la carte Leaflet
                document.dispatchEvent(new CustomEvent('aprs-frame', {detail: data}));
                // Notification chat si message prive
                if (data._chat) {
                    refreshContacts();
                    if (activeContact && activeContact === data.src) loadHistory(activeContact);
                    document.getElementById('qso-badge').classList.remove('hidden');
                    var _mb=document.getElementById('mnav-qso-badge');if(_mb){_mb.textContent='●';_mb.style.display='block';}
                    var _msgTxt = (data.extra && data.extra.msg_text) ? data.extra.msg_text : '';
                    _pushNotif('💬 QSO — ' + (data.src || '?'), _msgTxt, 'qso-' + (data.src || 'rx'));
                }
            }
        } catch(err) {}
    };

    evtSource.onerror = function() {
        document.getElementById('rx-status-text').textContent = '❌ SSE deconnecte';
        document.getElementById('rx-status-text').className = 'text-red-400';
        document.getElementById('rx-led').style.background = '#ef4444';
    };

    // ── Digi Path preset ───────────────────────────────────────────────────
    function aprsGeoToggle(mode) {
        var locBlock    = document.getElementById('geoLocatorBlock');
        var coordsBlock = document.getElementById('geoCoordsBlock');
        if (!locBlock || !coordsBlock) return;
        if (mode === 'coords') {
            locBlock.style.display    = 'none';
            coordsBlock.style.display = '';
        } else {
            locBlock.style.display    = '';
            coordsBlock.style.display = 'none';
        }
    }

    function applyPathPreset(sel) {
        var input = document.getElementById('pathCustom');
        if (!input) return;
        if (sel.value === 'custom') {
            input.focus();
        } else if (sel.value !== '') {
            input.value = sel.value;
        }
        // Remettre le select sur l'option correspondante ou "personnalisé"
        syncPathSelect();
    }

    function syncPathSelect() {
        var input  = document.getElementById('pathCustom');
        var sel    = document.getElementById('pathPreset');
        if (!input || !sel) return;
        var val = input.value.trim();
        var found = false;
        for (var i = 0; i < sel.options.length; i++) {
            if (sel.options[i].value === val && val !== '' && val !== 'custom') {
                sel.selectedIndex = i;
                found = true;
                break;
            }
        }
        if (!found) sel.value = 'custom';
    }

    // Synchroniser le select au chargement
    syncPathSelect();
    document.getElementById('pathCustom') && document.getElementById('pathCustom').addEventListener('input', syncPathSelect);


    // ── Passages ISS ─────────────────────────────────────────────────────────

    function issPassRefresh() {
        var list = document.getElementById('iss-pass-list');
        var btn  = document.getElementById('iss-refresh-btn');
        if (list) list.innerHTML = '<span style="color:#475569;font-style:italic">⏳ Interrogation open-notify.org...</span>';
        if (btn)  btn.textContent = '…';
        fetch('/iss_passes').then(function(r){ return r.json(); }).then(function(d){
            if (btn) btn.textContent = '↺ MAJ';
            if (!list) return;
            if (d.error) {
                list.innerHTML = '<span style="color:#ef4444">' + d.error + '</span>';
                return;
            }
            if (!d.passes || !d.passes.length) {
                list.innerHTML = '<span style="color:#475569;font-style:italic">Aucun passage prévu</span>';
                return;
            }
            list.innerHTML = d.passes.map(function(p, i) {
                var inMin = p.in_min;
                var col   = inMin < 20 ? '#a78bfa' : (inMin < 60 ? '#67e8f9' : '#475569');
                var bold  = (i === 0) ? 'font-weight:700;color:#c4b5fd' : '';
                return '<div style="display:flex;justify-content:space-between;align-items:center;' +
                       'padding:4px 0;border-bottom:1px solid #1e293b;' + bold + '">' +
                       '<span style="font-family:monospace;font-size:11px">' +
                       (i === 0 ? '🛸 ' : '   ') + p.risetime_fmt + '</span>' +
                       '<span style="color:' + col + ';font-family:monospace;font-size:10px">' +
                       'dans ' + inMin + ' min · ' + p.duration_min + ' min</span>' +
                       '</div>';
            }).join('');
            var st = document.getElementById('iss-pass-status');
            if (st) {
                st.textContent = '📍 ' + d.lat.toFixed(2) + '° / ' + d.lon.toFixed(2) + '°';
            }
            // Miroir dans l'onglet Réglages
            var cfgList = document.getElementById('iss-pass-list-cfg');
            if (cfgList) cfgList.innerHTML = list ? list.innerHTML : '';
        }).catch(function(e){
            if (btn) btn.textContent = '↺ MAJ';
            if (list) list.innerHTML = '<span style="color:#ef4444">❌ open-notify.org inaccessible</span>';
            console.warn('[ISS]', e);
        });
    }

    function issAlertToggle() {
        issAlertSave();
    }

    function issAlertSave() {
        var payload = {
            enabled:    document.getElementById('iss-alert-toggle').checked,
            advance_min: parseFloat(document.getElementById('iss-advance').value) || 10,
        };
        fetch('/iss_alert_config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        }).then(function(r){ return r.json(); }).then(function(){
            var st = document.getElementById('iss-pass-status');
            if (st) { st.textContent = '✅ Sauvegardé'; setTimeout(function(){ issPassRefresh(); }, 1500); }
            _issUpdateDot(payload.enabled);
        }).catch(function(e){ console.warn('[ISS]', e); });
    }

    function _issPassAlertShow(data) {
        var banner = document.createElement('div');
        banner.style.cssText = [
            'position:fixed','top:60px','left:50%','transform:translateX(-50%)',
            'z-index:9999','background:#2e1065','border:2px solid #7c3aed',
            'border-radius:14px','padding:12px 26px','font-size:13px','font-weight:700',
            'color:#ede9fe','box-shadow:0 8px 32px #7c3aed40','max-width:92vw',
            'text-align:center','cursor:pointer'
        ].join(';');
        var adv = data.advance_min !== undefined ? data.advance_min.toFixed(0) : '?';
        banner.innerHTML = '🛸 <b>PASSAGE ISS dans ' + adv + ' min</b>'
            + '<br><span style="font-size:11px;font-weight:400;color:#c4b5fd">'
            + data.risetime_fmt + ' · durée ' + data.duration_min + ' min'
            + ' · 145.825 MHz</span>';
        banner.onclick = function() {
            document.getElementById('iss-pass-list') && issPassRefresh();
            banner.parentNode && banner.parentNode.removeChild(banner);
        };
        document.body.appendChild(banner);
        // Bip triple (style alerte proximité) — 440 / 660 / 880 Hz
        try {
            var ctx = new (window.AudioContext || window.webkitAudioContext)();
            [440, 660, 880].forEach(function(freq, i) {
                var osc = ctx.createOscillator();
                var g   = ctx.createGain();
                osc.frequency.value = freq;
                osc.type = 'sine';
                g.gain.value = 0.22;
                osc.connect(g); g.connect(ctx.destination);
                osc.start(ctx.currentTime + i * 0.18);
                osc.stop (ctx.currentTime + i * 0.18 + 0.14);
            });
            setTimeout(function(){ try { ctx.close(); } catch(_){} }, 1000);
        } catch(_){}
        _pushNotif('🛸 Passage ISS dans ' + adv + ' min',
                   data.risetime_fmt + ' · ' + data.duration_min + ' min · 145.825 MHz',
                   'iss-pass');
        setTimeout(function(){ if (banner.parentNode) banner.parentNode.removeChild(banner); }, 15000);
    }

    function _issUpdateDot(enabled) {
        var dot = document.getElementById('iss-active-dot');
        if (!dot) return;
        dot.style.display = enabled ? 'inline-flex' : 'none';
    }

    // Charger config + passages au démarrage
    (function issInit(){
        fetch('/iss_alert_config').then(function(r){ return r.json(); }).then(function(d){
            var tog = document.getElementById('iss-alert-toggle');
            var adv = document.getElementById('iss-advance');
            if (tog) tog.checked = !!d.enabled;
            if (adv && d.advance_min !== undefined) adv.value = d.advance_min;
            _issUpdateDot(!!d.enabled);
        }).catch(function(){});
        issPassRefresh();
    })();

    // Rafraîchissement auto toutes les 15 min (évite d'appeler open-notify trop souvent)
    setInterval(issPassRefresh, 15 * 60 * 1000);

    // ── Rafraichissement liste peripheriques audio ──────────────────────────
    async function refreshDevices() {
        try {
            var r = await fetch('/audio_devices');
            var devs = await r.json();
            var selTx = document.getElementById('sel_audio_tx');
            var selRx = document.getElementById('sel_audio_rx');
            if (!selTx || !selRx) return;
            var prevTx = selTx.value;
            var prevRx = selRx.value;
            function buildOptions(sel, filterKey, prevVal) {
                sel.innerHTML = '<option value="">-- Defaut systeme --</option>';
                devs.filter(function(d) { return d[filterKey]; }).forEach(function(d) {
                    var opt = document.createElement('option');
                    opt.value = d.id;
                    var flags = [];
                    if (d.in)  flags.push('IN');
                    if (d.out) flags.push('OUT');
                    opt.textContent = d.name + (flags.length ? ' [' + flags.join('/') + ']' : '');
                    if (String(d.id) === String(prevVal)) opt.selected = true;
                    sel.appendChild(opt);
                });
            }
            buildOptions(selTx, 'out', prevTx);
            buildOptions(selRx, 'in',  prevRx);
        } catch(e) { console.error('refreshDevices:', e); }
    }

    // ── Test RX ────────────────────────────────────────────────────────────
    async function testRX(btn) {
        var orig = btn.textContent;
        btn.textContent = 'Test en cours...';
        btn.disabled = true;
        try {
            var r = await fetch('/rx_test');
            var d = await r.json();
            var msg = '';
            if (!d.rx_thread) msg += 'Thread RX inactif. ';
            if (!d.audio_device_ok) msg += 'Audio: ' + (d.audio_error || 'erreur') + '. ';
            if (d.rx_thread && d.audio_device_ok) {
                msg = 'RX OK -- ' + d.bits_received + ' bits';
            }
            if (d.tx_last_error) msg += ' | TX ERR: ' + d.tx_last_error;
            else if (d.tx_last_ok) msg += ' | Derniere TX: ' + d.tx_last_ok;
            btn.textContent = msg || 'Resultat inconnu';
        } catch(e) {
            btn.textContent = 'Erreur reseau';
        }
        setTimeout(function() { btn.textContent = orig; btn.disabled = false; }, 4000);
    }

    // ── Envoi messages trafic ──────────────────────────────────────────────
    async function send() {
        var msg  = document.getElementById('msg').value;
        var dest = (document.getElementById('dest_call').value.toUpperCase() || "APRS");
        if (!msg) return;
        fetch('/send_raw', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ message: msg, dest_station: dest })
        });
        document.getElementById('msg').value = "";
    }

    async function sendISS() {
        fetch('/send_raw', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ message: "Contact via ARISS / ISS", is_iss: true })
        });
    }

    async function sendBeacon() { await fetch('/send_beacon', { method: 'POST' }); }

    async function sendWeather(btn) {
        var orig = btn.textContent;
        btn.textContent = '⏳ Récupération météo...';
        btn.disabled = true;
        try {
            var r = await fetch('/send_weather', { method: 'POST' });
            var d = await r.json();
            if (d.error) {
                btn.textContent = '❌ ' + d.error;
            } else if (d.wx) {
                var wx = d.wx;
                btn.textContent = '✅ ' + wx.temp_c.toFixed(1) + '°C ' + wx.humidity_pct + '% ' + wx.description;
            } else {
                btn.textContent = '✅ Envoyé';
            }
        } catch(e) {
            btn.textContent = '❌ Erreur réseau';
        }
        setTimeout(function(){ btn.textContent = orig; btn.disabled = false; }, 4000);
    }

    async function sendPropagation(btn) {
        var orig = btn.textContent;
        btn.textContent = '⏳ Indices NOAA...';
        btn.disabled = true;
        try {
            var r = await fetch('/send_propagation', { method: 'POST' });
            var d = await r.json();
            if (d.error) {
                btn.textContent = '❌ ' + d.error;
            } else if (d.data) {
                var p = d.data;
                var sfi = p.sfi  !== null ? 'SFI:' + Math.round(p.sfi)  : '';
                var kp  = p.k_index !== null ? ' K:'  + p.k_index.toFixed(1) : '';
                var hf  = p.hf_cond  ? ' HF:'  + p.hf_cond  : '';
                btn.textContent = '✅ ' + sfi + kp + hf;
            } else {
                btn.textContent = '✅ Envoyé';
            }
        } catch(e) {
            btn.textContent = '❌ Erreur réseau';
        }
        setTimeout(function(){ btn.textContent = orig; btn.disabled = false; }, 5000);
    }

    async function sendStatus() {
        await fetch('/send_status', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({}) });
    }

    function clearConsole() { document.getElementById('console').innerHTML = ''; }

    // ── Beacon auto ────────────────────────────────────────────────────────
    var _beaconRemaining = null;
    var _beaconInterval  = 0;

    async function pollBeaconStatus() {
        try {
            var r = await fetch('/beacon_status');
            var d = await r.json();
            // d.schedules = {station:{interval,next_in}, meteo:{...}, ...}
            var schedules = d.schedules || {};

            var TYPES = ['station', 'iss', 'meteo', 'propagation'];
            var anyActive = false;

            TYPES.forEach(function(btype) {
                var badge     = document.getElementById('badge-' + btype);
                var led       = document.getElementById('led-' + btype);
                var countdown = document.getElementById('countdown-' + btype);
                if (!badge || !led) return;

                var info     = schedules[btype] || {};
                var interval = info.interval || 0;
                var nextIn   = (info.next_in !== null && info.next_in !== undefined) ? info.next_in : null;
                var active   = interval > 0;
                if (active) anyActive = true;

                if (active) {
                    badge.className = 'flex items-center justify-between px-2.5 py-1.5 rounded-xl border transition-all '
                        + 'bg-amber-500/10 border-amber-500/40 text-amber-300 font-bold';
                    led.style.background = '#f59e0b';
                    led.style.boxShadow  = '0 0 6px #f59e0b';
                    if (countdown) {
                        if (nextIn !== null) {
                            var m = Math.floor(nextIn / 60), s = nextIn % 60;
                            countdown.textContent = m + 'min ' + (s < 10 ? '0' : '') + s + 's';
                            countdown.style.color = '#f59e0b';
                        } else {
                            countdown.textContent = interval + 'min';
                            countdown.style.color = '#64748b';
                        }
                    }
                } else {
                    badge.className = 'flex items-center justify-between px-2.5 py-1.5 rounded-xl border transition-all '
                        + 'bg-slate-800/40 border-slate-700/50 text-slate-600 font-normal';
                    led.style.background = '#334155';
                    led.style.boxShadow  = 'none';
                    if (countdown) { countdown.textContent = ''; }
                }
            });

            // Compatibilité : mettre à jour _beaconInterval pour d'éventuels usages
            _beaconInterval = anyActive ? 1 : 0;

        } catch(_) {}
    }

    function tickBeaconCountdown() {
        if (_beaconRemaining === null) return;
        if (_beaconRemaining > 0) _beaconRemaining--;
        var el = document.getElementById('beacon-countdown');
        if (!el) return;
        var m = Math.floor(_beaconRemaining / 60);
        var s = String(_beaconRemaining % 60).padStart(2, '0');
        el.textContent = m + ':' + s;
        el.style.color = _beaconRemaining < 10 ? '#ef4444' : '#f59e0b';
    }

    pollBeaconStatus();
    setInterval(pollBeaconStatus, 5000);
    setInterval(tickBeaconCountdown, 1000);

    // ── Config form ────────────────────────────────────────────────────────
    document.getElementById('configForm').onsubmit = async function(e) {
        e.preventDefault();
        var btn = e.target.querySelector('button[type=submit]');
        var origText = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin mr-2"></i> Chargement...';
        var data = Object.fromEntries(new FormData(e.target).entries());
        // Conversion des champs numériques — NE PAS convertir les devices audio
        // car ils peuvent être des strings ALSA ("plughw:2,0")
        var devTx = data.audio_device_tx;
        var devRx = data.audio_device_rx;
        data.audio_device_tx = (devTx === '' || devTx === null) ? null
            : isNaN(parseInt(devTx)) ? devTx : parseInt(devTx);
        data.audio_device_rx = (devRx === '' || devRx === null) ? null
            : isNaN(parseInt(devRx)) ? devRx : parseInt(devRx);
        data.volume           = parseFloat(data.volume);
        data.tx_delay_ms      = parseInt(data.tx_delay_ms);
        data.ptt_delay_ms     = parseInt(data.ptt_delay_ms) || 250;
        data.beacon_interval  = parseInt(data.beacon_interval) || 0;
        // Coordonnées géographiques manuelles (optionnelles)
        data.lat_manual = (data.lat_manual !== '' && !isNaN(parseFloat(data.lat_manual)))
            ? parseFloat(data.lat_manual) : '';
        data.lon_manual = (data.lon_manual !== '' && !isNaN(parseFloat(data.lon_manual)))
            ? parseFloat(data.lon_manual) : '';
        // Construire beacon_schedules depuis les selects sched_*
        var schedules = {};
        ['station','iss','meteo','propagation'].forEach(function(t) {
            var v = parseInt(data['sched_' + t]) || 0;
            if (v > 0) schedules[t] = v;
            delete data['sched_' + t];
        });
        data.beacon_schedules = schedules;
        console.log('[CONFIG] beacon_schedules=', JSON.stringify(schedules));
        // ── iGate : checkbox → bool, radio → bool ────────────────────────────
        data.igate_enabled  = document.getElementById('igate_enabled') ? document.getElementById('igate_enabled').checked : false;
        data.igate_rx_only  = data.igate_rx_only === 'true';
        data.igate_port     = parseInt(data.igate_port) || 14580;
        // ── Wavelog : reconstruction du sous-objet ────────────────────────────
        data.wavelog = {
            enabled:       document.getElementById('wavelog_enabled')  ? document.getElementById('wavelog_enabled').checked  : false,
            url:           (data.wavelog_url       || '').trim(),
            api_key:       (data.wavelog_api_key   || '').trim(),
            station_id:    parseInt(data.wavelog_station_id)   || 1,
            sync_interval: parseInt(data.wavelog_sync_interval) || 5,
            sync_rx:       document.getElementById('wavelog_sync_rx')   ? document.getElementById('wavelog_sync_rx').checked   : true,
            sync_tx:       document.getElementById('wavelog_sync_tx')   ? document.getElementById('wavelog_sync_tx').checked   : true,
            only_qso:      document.getElementById('wavelog_only_qso')  ? document.getElementById('wavelog_only_qso').checked  : true,
            last_sync_id:  0,   // conservé côté serveur — on le recharge après save
        };
        // Récupérer le last_sync_id actuel pour ne pas l'écraser
        try {
            var _wlst = await (await fetch('/wavelog/status')).json();
            if (_wlst.last_sync_id) data.wavelog.last_sync_id = _wlst.last_sync_id;
        } catch(_) {}
        // Nettoyer les clés wavelog_* à plat (on a reconstruit l'objet)
        ['wavelog_url','wavelog_api_key','wavelog_station_id','wavelog_sync_interval',
         'wavelog_enabled','wavelog_sync_rx','wavelog_sync_tx','wavelog_only_qso'
        ].forEach(function(k){ delete data[k]; });
        try {
            await fetch('/update_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
        } catch(err) {
            btn.innerHTML = 'Erreur reseau';
            btn.disabled = false;
            return;
        }
        var tries = 0;
        var poll = setInterval(async function() {
            tries++;
            try {
                var r = await fetch('/config_status');
                var s = await r.json();
                if (s.state === 'ok') {
                    clearInterval(poll);
                    btn.innerHTML = '<i class="fas fa-check-circle mr-2"></i> Applique !';
                    setTimeout(function() { location.reload(); }, 800);
                } else if (s.state === 'error') {
                    clearInterval(poll);
                    btn.innerHTML = 'Erreur : ' + (s.error || 'inconnue');
                    btn.disabled = false;
                } else if (tries > 30) {
                    clearInterval(poll);
                    btn.innerHTML = 'Timeout -- verifier les logs';
                    btn.disabled = false;
                }
            } catch(_) {}
        }, 300);
    };

    // ── QSO Chat ───────────────────────────────────────────────────────────
    var activeContact = null;

    function openCQGeneral() {
        activeContact = 'APRS';
        document.getElementById('chat-title').innerHTML =
            '<span style="color:#f59e0b;font-weight:900;letter-spacing:.04em">📢 Appel général</span>'
            + ' <span style="font-size:9px;color:#78716c;font-family:monospace">→ APRS</span>';
        document.getElementById('chat-subtitle').textContent = 'Broadcast — tous les postes APRS';
        document.getElementById('chat-avatar').innerHTML =
            '<span style="font-size:16px">📢</span>';
        document.getElementById('chat-avatar').style.background = 'linear-gradient(135deg,#451a03,#78350f)';
        document.getElementById('chat-avatar').style.border = '1px solid #f59e0b';
        document.getElementById('chat-input').disabled    = false;
        document.getElementById('chat-send-btn').disabled = false;
        document.getElementById('chat-input').placeholder = 'CQ CQ DE F1RIQ — 67 car. max...';
        document.getElementById('chat-input').focus();
        _macroSetEnabled(true);
        loadHistory('APRS');
        switchTab('qso');
    }

    function openContact(cs) {
        cs = cs.trim().toUpperCase();
        if (!cs) return;
        activeContact = cs;
        document.getElementById('new-contact-input').value = '';
        document.getElementById('chat-title').innerHTML = cs + ' ' + qrzLink(cs, {cls:'text-[10px] text-slate-500 hover:text-blue-400 font-mono transition-colors align-middle'});
        document.getElementById('chat-subtitle').textContent = 'APRS - 144.800 MHz';
        document.getElementById('chat-avatar').textContent = cs.substring(0, 2);
        document.getElementById('chat-input').disabled    = false;
        document.getElementById('chat-send-btn').disabled = false;
        document.getElementById('chat-input').placeholder = 'Message APRS 67 car. max...';
        document.getElementById('chat-avatar').style.background = '';
        document.getElementById('chat-avatar').style.border = '';
        document.getElementById('chat-input').focus();
        _macroSetEnabled(true);
        loadHistory(cs);
        switchTab('qso');
    }

    async function loadHistory(cs) {
        var r    = await fetch('/chat/history/' + cs);
        var msgs = await r.json();
        renderMessages(msgs);
        refreshContacts();
    }

    function renderMessages(msgs) {
        var box = document.getElementById('chat-messages');
        if (!msgs.length) {
            box.innerHTML = '<div class="text-center text-slate-600 text-xs italic mt-20">Aucun message — dites bonjour !</div>';
            return;
        }

        var html = '';
        var lastDay = null;

        msgs.forEach(function(m) {
            var isTx = m.dir === 'tx';

            // ── Séparateur de date ─────────────────────────────────────────
            var dayStr = m.ts ? m.ts.split(' ')[0] : null;   // ex. "25/05"
            if (dayStr && dayStr !== lastDay) {
                lastDay = dayStr;
                html += '<div style="display:flex;align-items:center;gap:8px;margin:12px 0 8px">'
                      + '<div style="flex:1;height:1px;background:#1e293b"></div>'
                      + '<span style="font-size:9px;color:#475569;font-family:monospace">' + dayStr + '</span>'
                      + '<div style="flex:1;height:1px;background:#1e293b"></div>'
                      + '</div>';
            }

            // ── Métadonnées ────────────────────────────────────────────────
            var acked   = m.acked ? '<span style="color:#34d399;font-size:9px;margin-left:4px">✓ ACK</span>' : '';
            var timeStr = m.ts ? m.ts.split(' ')[1] || m.ts : '';
            var msgno   = m.msgno ? '<span style="color:#475569;font-size:9px"> #' + m.msgno + '</span>' : '';
            var caller  = esc(m.from || (isTx ? 'Moi' : activeContact || '?'));

            if (isTx) {
                // ── Bulle TX (droite, bleu) ────────────────────────────────
                html += '<div style="display:flex;justify-content:flex-end;margin-bottom:10px">'
                      + '<div style="max-width:78%;min-width:80px">'
                      +   '<div style="'
                      +     'background:linear-gradient(135deg,#1d4ed8,#2563eb);'
                      +     'color:#fff;'
                      +     'padding:10px 14px;'
                      +     'border-radius:18px 18px 4px 18px;'
                      +     'font-size:13px;line-height:1.5;'
                      +     'box-shadow:0 2px 8px #1d4ed840;'
                      +     'word-break:break-word;'
                      +   '">'
                      +     esc(m.text) + acked
                      +   '</div>'
                      +   '<div style="text-align:right;font-size:9px;color:#475569;margin-top:3px;padding-right:4px;font-family:monospace">'
                      +     timeStr + msgno
                      +   '</div>'
                      + '</div>'
                      + '</div>';
            } else {
                // ── Bulle RX (gauche, vert émeraude) ──────────────────────
                var initials = caller.substring(0, 2).toUpperCase();
                html += '<div style="display:flex;justify-content:flex-start;align-items:flex-end;gap:8px;margin-bottom:10px">'
                      // Avatar indicatif
                      + '<div style="'
                      +   'width:32px;height:32px;border-radius:10px;'
                      +   'background:linear-gradient(135deg,#064e3b,#065f46);'
                      +   'border:1px solid #10b981;'
                      +   'display:flex;align-items:center;justify-content:center;'
                      +   'font-size:10px;font-weight:900;color:#34d399;'
                      +   'font-family:monospace;flex-shrink:0;'
                      + '">' + initials + '</div>'
                      // Bulle + méta
                      + '<div style="max-width:74%;min-width:80px">'
                      +   '<div style="font-size:9px;font-weight:700;color:#34d399;font-family:monospace;margin-bottom:3px;letter-spacing:.04em">'
                      +     caller
                      +   '</div>'
                      +   '<div style="'
                      +     'background:#0f2922;'
                      +     'border:1px solid #10b981;'
                      +     'color:#d1fae5;'
                      +     'padding:10px 14px;'
                      +     'border-radius:18px 18px 18px 4px;'
                      +     'font-size:13px;line-height:1.5;'
                      +     'box-shadow:0 2px 8px #10b98120;'
                      +     'word-break:break-word;'
                      +   '">'
                      +     esc(m.text)
                      +   '</div>'
                      +   '<div style="font-size:9px;color:#475569;margin-top:3px;padding-left:4px;font-family:monospace">'
                      +     timeStr + msgno
                      +   '</div>'
                      + '</div>'
                      + '</div>';
            }
        });

        box.innerHTML = html;
        box.scrollTop = box.scrollHeight;
    }

    // ── Macros QSO ─────────────────────────────────────────────────────────
    var _CALLSIGN_CFG = '""" + (config_manager.data.get('callsign','N0CALL')) + """';
    // Groupe 1 : macros QSO standard
    var _macrosQso = [
        { label:'CQ',      text:'CQ CQ DE {MY} APRS QRZ?' },
        { label:'73',      text:'73 DE {MY} GL ES 73' },
        { label:'QTH',     text:'QTH {MY} LOC {LOC}' },
        { label:'QRZ?',    text:'QRZ? DE {MY}' },
        { label:'RST 599', text:'UR RST 599 599 DE {MY}' },
        { label:'QSL?',    text:'QSL? DE {MY}' },
        { label:'QSL OK',  text:'QSL TU DE {MY} 73' },
        { label:'BCNU',    text:'BCNU 73 DE {MY}' },
        { label:'Test',    text:'TEST DE {MY} PSE QSL' },
        { label:'POTA',    text:'POTA ACTIVATION DE {MY} PSE QSL' },
    ];

    // Groupe 2 : interrogations directes APRS / Q-codes
    // Interrogations APRS directes — envoi immédiat (APRS spec §16)
    var _macrosAprs = [
        { label:'?APRST', text:'?APRST', title:'Demande la position APRS de la station' },
        { label:'?APRSV', text:'?APRSV', title:'Demande la version logiciel APRS' },
        { label:'?WX',    text:'?WX',    title:'Demande météo locale' },
        { label:'?PING?', text:'?PING?', title:'Ping — vérifie que la station est active' },
    ];

    function _macroExpand(tpl) {
        var my   = (document.querySelector('[name=callsign]') || {}).value || _CALLSIGN_CFG || 'N0CALL';
        var loc  = (document.querySelector('[name=maidenhead]') || {}).value || '';
        var dest = activeContact || 'DEST';
        return tpl.replace(/\{MY\}/g,   my.toUpperCase())
                  .replace(/\{LOC\}/g,  loc ? loc.toUpperCase() : '???')
                  .replace(/\{DEST\}/g, dest.toUpperCase());
    }

    // Envoi direct d'un texte sans passer par le champ de saisie
    async function _chatSendDirect(text) {
        if (!text || !activeContact) return;
        // Feedback visuel : flash du bouton envoi
        var sendBtn = document.getElementById('chat-send-btn');
        if (sendBtn) {
            sendBtn.textContent = '⚡';
            setTimeout(function() { sendBtn.textContent = '📨'; }, 600);
        }
        await fetch('/chat/send', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ dest: activeContact, text: text })
        });
        loadHistory(activeContact);
    }

    function _makeMacroBtn(m, idx, group) {
        var isAprs  = (group === 'aprs');
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.disabled = true;
        btn.setAttribute('data-group', group);
        btn.setAttribute('data-idx', idx);

        var baseClass = 'macro-btn px-2 py-1 rounded-lg text-[10px] font-bold transition-all active:scale-95 '
                      + 'disabled:opacity-30 disabled:cursor-not-allowed ';

        if (isAprs) {
            // Bouton APRS : étiquette + icône éclair pour signaler l'envoi direct
            btn.innerHTML = m.label + ' <span style="font-size:8px;opacity:.7">⚡</span>';
            btn.title = m.title + ' — Envoi immédiat: ' + m.text;
            btn.className = baseClass
                + 'bg-violet-950/60 border border-violet-700/60 text-violet-300 '
                + 'hover:bg-violet-700 hover:border-violet-400 hover:text-white';
            btn.onclick = function() {
                if (this.disabled) return;
                var txt = _macrosAprs[parseInt(this.getAttribute('data-idx'))].text;
                _chatSendDirect(txt);
            };
        } else {
            btn.textContent = m.label;
            btn.title = (m.title || _macroExpand(m.text)) + ' — ' + _macroExpand(m.text);
            btn.className = baseClass
                + 'bg-slate-800 border border-slate-700 text-slate-300 '
                + 'hover:bg-blue-700 hover:border-blue-500 hover:text-white';
            btn.onclick = function() {
                var inp2 = document.getElementById('chat-input');
                if (!inp2 || inp2.disabled) return;
                var txt = _macroExpand(_macrosQso[parseInt(this.getAttribute('data-idx'))].text).substring(0, 67);
                inp2.value = txt;
                inp2.focus();
                var cc = document.getElementById('chat-charcount');
                if (cc) cc.textContent = txt.length + '/67';
            };
        }
        return btn;
    }

    function _renderMacroBar() {
        var bar = document.getElementById('macro-bar');
        if (!bar) return;
        bar.innerHTML = '';

        // ── Groupe QSO ────────────────────────────────────────────────────
        var lblQso = document.createElement('span');
        lblQso.textContent = 'QSO';
        lblQso.style.cssText = 'font-size:8px;font-weight:900;color:#475569;'
                             + 'text-transform:uppercase;letter-spacing:.08em;'
                             + 'align-self:center;white-space:nowrap;margin-right:2px';
        bar.appendChild(lblQso);

        _macrosQso.forEach(function(m, idx) {
            bar.appendChild(_makeMacroBtn(m, idx, 'qso'));
        });

        // ── Séparateur ────────────────────────────────────────────────────
        var sep = document.createElement('span');
        sep.style.cssText = 'width:1px;height:18px;background:#334155;'
                          + 'align-self:center;flex-shrink:0;margin:0 4px';
        bar.appendChild(sep);

        // ── Groupe APRS (envoi direct) ────────────────────────────────────
        var lblAprs = document.createElement('span');
        lblAprs.textContent = 'APRS';
        lblAprs.style.cssText = 'font-size:8px;font-weight:900;color:#6d28d9;'
                              + 'text-transform:uppercase;letter-spacing:.08em;'
                              + 'align-self:center;white-space:nowrap;margin-right:2px';
        bar.appendChild(lblAprs);

        _macrosAprs.forEach(function(m, idx) {
            bar.appendChild(_makeMacroBtn(m, idx, 'aprs'));
        });
    }

    function _macroSetEnabled(enabled) {
        document.querySelectorAll('.macro-btn').forEach(function(b) { b.disabled = !enabled; });
    }

    _renderMacroBar();

    async function chatSend() {
        var input = document.getElementById('chat-input');
        var text  = input.value.trim();
        if (!text || !activeContact) return;
        input.value = '';
        document.getElementById('chat-charcount').textContent = '0/67';
        await fetch('/chat/send', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ dest: activeContact, text: text })
        });
        loadHistory(activeContact);
    }

    async function refreshContacts() {
        try {
            var r        = await fetch('/chat/contacts');
            var contacts = await r.json();
            var list     = document.getElementById('contact-list');
            var badge    = document.getElementById('qso-badge');
            var unreadTotal = contacts.reduce(function(s, c) { return s + c.unread; }, 0);
            if (unreadTotal > 0) {
                badge.textContent = unreadTotal;
                badge.classList.remove('hidden');
                var _btnQso = document.getElementById('btn-qso');
                if (_btnQso) _btnQso.classList.add('qso-blink');
                var _mnavQso = document.getElementById('mnav-qso');
                if (_mnavQso) _mnavQso.classList.add('qso-blink-mob');
            } else {
                badge.classList.add('hidden');
                var _btnQso = document.getElementById('btn-qso');
                if (_btnQso) _btnQso.classList.remove('qso-blink');
                var _mnavQso = document.getElementById('mnav-qso');
                if (_mnavQso) _mnavQso.classList.remove('qso-blink-mob');
            }
            var _mqb = document.getElementById('mnav-qso-badge');
            if (_mqb) { if (unreadTotal > 0) { _mqb.textContent = unreadTotal; _mqb.style.display = 'block'; } else { _mqb.style.display = 'none'; } }
            if (!contacts.length) {
                list.innerHTML = '<div class="px-6 py-8 text-center text-slate-600 text-xs italic">Aucun contact</div>';
                return;
            }
            list.innerHTML = contacts.map(function(c) {
                var active = (activeContact === c.callsign) ? 'bg-slate-800/60 border-l-2 border-blue-500' : '';
                var unreadBadge = c.unread > 0
                    ? '<span class="bg-blue-500 text-white text-[9px] font-black rounded-full w-5 h-5 flex items-center justify-center shrink-0">' + c.unread + '</span>'
                    : '';
                return '<div onclick="openContact(this.dataset.cs)" data-cs="' + esc(c.callsign) + '"'
                     + ' class="px-6 py-4 flex items-center gap-4 cursor-pointer hover:bg-slate-800/40 transition-colors ' + active + '">'
                     + '<div class="w-10 h-10 rounded-xl bg-slate-800 flex items-center justify-center text-sm font-black text-blue-400 shrink-0">'
                     + esc(c.callsign.substring(0, 2)) + '</div>'
                     + '<div class="flex-1 min-w-0">'
                     + '<div class="flex justify-between items-center">'
                     + '<span class="font-bold text-white text-sm font-mono">' + esc(c.callsign) + '</span>'
                     + qrzLink(c.callsign, {cls:'text-[9px] text-slate-600 hover:text-blue-400 font-mono transition-colors ml-1', style:'vertical-align:middle'})
                     + '<span class="text-[10px] text-slate-600">' + esc(c.ts) + '</span>'
                     + '</div>'
                     + '<div class="text-xs text-slate-500 truncate">' + esc(c.last) + '</div>'
                     + '</div>' + unreadBadge + '</div>';
            }).join('');
        } catch(e) {}
    }

    document.getElementById('chat-input').addEventListener('input', function() {
        document.getElementById('chat-charcount').textContent = this.value.length + '/67';
    });


    // ══════════════════════════════════════════════════════════════════════
    // CARTE APRS — Leaflet
    // ══════════════════════════════════════════════════════════════════════
    var _map        = null;
    var _mapMarkers = {};   // callsign → { marker, emoji }
    var _mapCount   = 0;

    // Emoji par type de trame
    var _TYPE_EMOJI = {
        'Position': '📍', 'Position+Msg': '📍', 'Position+TS': '📍', 'Position+TS+Msg': '📍',
        'Message': '💬', 'Statut': '📻', 'Meteo': '🌤️', 'Objet': '🎯',
        'Mic-E': '📟', 'Beacon': '📡', 'Beacon ISS': '🛸', 'DIGI': '🔁',
    };
    function _typeEmoji(t) {
        if (!t) return '📦';
        for (var k in _TYPE_EMOJI) { if (t.indexOf(k) !== -1) return _TYPE_EMOJI[k]; }
        return '📦';
    }

    function _initMap() {
        if (_map) { _map.invalidateSize(); return; }
        var el = document.getElementById('aprs-map');
        if (!el) return;
        _map = L.map('aprs-map', { zoomControl: true, attributionControl: false })
                .setView([46.5, 2.5], 6);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 18, attribution: '© OSM'
        }).addTo(_map);
        // Rejouer les stations déjà en mémoire
        Object.keys(_mapMarkers).forEach(function(cs) {
            var m = _mapMarkers[cs];
            if (m.lat && m.lon) _placeMarker(cs, m.lat, m.lon, m.emoji, m.popup);
        });
    }

    function _placeMarker(cs, lat, lon, emoji, popupHtml) {
        var icon = L.divIcon({
            className: '',
            html: '<div style="display:flex;flex-direction:column;align-items:center;gap:1px">'
                + '<div style="font-size:22px;line-height:1;filter:drop-shadow(0 1px 2px #000a)">' + emoji + '</div>'
                + '<div style="background:rgba(15,23,42,0.85);color:#60a5fa;border:1px solid #334155;'
                + 'border-radius:4px;padding:1px 4px;font-size:9px;font-weight:700;font-family:monospace;white-space:nowrap">' + cs + '</div>'
                + '</div>',
            iconAnchor: [11, 30]
        });
        if (_mapMarkers[cs] && _mapMarkers[cs].marker) {
            _mapMarkers[cs].marker.setLatLng([lat, lon]).setIcon(icon).setPopupContent(popupHtml);
        } else {
            var marker = L.marker([lat, lon], {icon: icon}).bindPopup(popupHtml).addTo(_map);
            _mapMarkers[cs].marker = marker;
        }
    }

    function _mapAddOrUpdate(frame) {
        var e = frame.extra || {};
        if (e.lat === undefined || e.lon === undefined) return;
        var cs    = (frame.src || '?').toUpperCase();
        var lat   = e.lat, lon = e.lon;
        var sym   = e.symbol || '';
        // Ignorer les symboles APRS bruts (/X \X) — utiliser l'emoji du type
        var _symRaw = /^[\/\\][\x21-\x7e]/.test(sym.trim());
        var emoji = (!sym || _symRaw) ? _typeEmoji(frame.aprs_type) : sym.split(' ')[0];
        var _isObjFrame = (frame.aprs_type === 'Objet');
        var _rawDistKm  = (_stationLat !== null && _stationLon !== null)
            ? haversineKm(_stationLat, _stationLon, lat, lon) : null;
        // Pour un objet, préférer la distance de position propre de la station
        var _distKm = (_isObjFrame && _stationPosDistKm[cs] !== undefined)
            ? _stationPosDistKm[cs] : _rawDistKm;
        // Mémoriser si c'est une trame de position propre
        if (!_isObjFrame && _rawDistKm !== null) _stationPosDistKm[cs] = _rawDistKm;
        var _distLabel = _distKm !== null
            ? (_distKm < 10 ? _distKm.toFixed(1) : Math.round(_distKm)) + ' km' : null;
        var _distColor = _distKm === null ? '' : _distKm < 50 ? '#34d399' : _distKm < 150 ? '#fbbf24' : '#fb923c';
        var popup =
            '<b style="font-size:13px">' + emoji + ' ' + cs + '</b>'
            + (_distLabel ? ' &nbsp;<span style="font-size:11px;color:' + _distColor + ';font-weight:700">📏 ' + _distLabel + '</span>' : '')
            + '<br><span style="font-size:11px;color:#94a3b8">' + (frame.aprs_type || '') + '</span>'
            + (e.symbol ? '<br><span style="font-size:11px">' + e.symbol + '</span>' : '')
            + (e.speed_kmh !== undefined ? '<br>🏎️ ' + e.speed_kmh + ' km/h &nbsp;🧭 ' + (e.course||'?') + '°' : '')
            + (e.alt_m     !== undefined ? '<br>⬆️ '  + e.alt_m + ' m' : '')
            + (e.comment && e.comment.length ? '<br>💬 ' + e.comment : '')
            + '<br><span style="font-size:10px;color:#64748b">' + new Date().toLocaleTimeString() + '</span>';

        if (!_mapMarkers[cs]) {
            _mapMarkers[cs] = { lat: lat, lon: lon, emoji: emoji, popup: popup, distKm: _distKm };
            _mapCount++;
            document.getElementById('map-station-count').textContent = _mapCount;
            var badge = document.getElementById('map-badge');
            badge.textContent = _mapCount;
            badge.classList.remove('hidden');
            var _mmb = document.getElementById('mnav-map-badge');
            if (_mmb) { _mmb.textContent = _mapCount; _mmb.style.display = 'block'; }
        } else {
            _mapMarkers[cs].lat    = lat;
            _mapMarkers[cs].lon    = lon;
            _mapMarkers[cs].emoji  = emoji;
            _mapMarkers[cs].popup  = popup;
            _mapMarkers[cs].distKm = _distKm;
        }

        if (_map) _placeMarker(cs, lat, lon, emoji, popup);
        _updateStationList();
    }

    function _updateStationList() {
        var list = document.getElementById('map-station-list');
        var keys = Object.keys(_mapMarkers);
        if (!keys.length) {
            list.innerHTML = '<div class="px-4 py-6 text-center text-slate-600 text-xs italic">En attente de trames...</div>';
            return;
        }
        list.innerHTML = keys.map(function(cs) {
            var m = _mapMarkers[cs];
            var distHtml = '';
            if (m.distKm !== null && m.distKm !== undefined) {
                var dStr = m.distKm < 10 ? m.distKm.toFixed(1) : Math.round(m.distKm);
                var dCol = m.distKm < 50 ? '#34d399' : m.distKm < 150 ? '#fbbf24' : '#fb923c';
                distHtml = '<span style="font-size:9px;font-family:monospace;color:' + dCol + ';margin-left:auto;white-space:nowrap">📏 ' + dStr + ' km</span>';
            }
            return '<div class="px-4 py-3 flex items-center gap-3 cursor-pointer hover:bg-slate-800/40 transition-colors"'
                 + ' data-cs="' + cs + '" onclick="(function(el){var c=el.dataset.cs;if(_map&&_mapMarkers[c]&&_mapMarkers[c].marker){_map.setView(_mapMarkers[c].marker.getLatLng(),13);_mapMarkers[c].marker.openPopup();switchTab(&quot;map&quot;)}})(this)">'
                 + '<span style="font-size:18px;line-height:1">' + (m.emoji||'📍') + '</span>'
                 + qrzLink(cs, {cls:'font-mono font-bold text-white text-xs hover:text-blue-300 transition-colors'})
                 + distHtml
                 + '</div>';
        }).join('');
    }

    function mapClearAll() {
        if (_map) Object.values(_mapMarkers).forEach(function(m) { if (m.marker) _map.removeLayer(m.marker); });
        _mapMarkers = {}; _mapCount = 0;
        document.getElementById('map-station-count').textContent = '0';
        document.getElementById('map-badge').classList.add('hidden');
        var _mmbc = document.getElementById('mnav-map-badge'); if (_mmbc) _mmbc.style.display = 'none';
        _updateStationList();
    }

    function mapFitAll() {
        if (!_map) return;
        var pts = Object.values(_mapMarkers).filter(function(m){return m.lat;}).map(function(m){return [m.lat,m.lon];});
        if (pts.length) _map.fitBounds(pts, {padding:[40,40]});
    }

    // Écouter les frames APRS reçues via SSE
    document.addEventListener('aprs-frame', function(ev) {
        var f = ev.detail;
        if (f.extra && f.extra.lat !== undefined) _mapAddOrUpdate(f);
    });

    // ── Propagation VHF ────────────────────────────────────────────────────
    function vhfPropRefresh() {
        var btn = document.getElementById('vhf-refresh-btn');
        var box = document.getElementById('vhf-prop-content');
        if (btn) btn.textContent = '...';

        function kpColor(v) { return v===null?'text-slate-500':v<=2?'text-emerald-400':v<=4?'text-yellow-400':'text-red-400'; }
        function kpLbl(v)   { return v===null?'–':v<=2?'🟢 Calme':v<=4?'🟡 Agité':'🔴 Perturbé'; }
        function aColor(v)  { return v===null?'text-slate-500':v<=7?'text-emerald-400':v<=20?'text-yellow-400':'text-red-400'; }
        function aLbl(v)    { return v===null?'–':v<=7?'🟢 Stable':v<=20?'🟡 Instable':'🔴 Perturbé'; }
        function sfiColor(v){ return v===null?'text-slate-500':v>=150?'text-emerald-400':v>=100?'text-yellow-400':'text-red-400'; }
        function sfiLbl(v)  { return v===null?'–':v>=150?'🟢 Actif':v>=100?'🟡 Moyen':'🔴 Faible'; }

        function row(icon, label, val, color, lbl) {
            return '<div class="flex justify-between items-center bg-slate-900/60 rounded-xl px-3 py-2">'
                + '<span class="text-slate-500">' + icon + ' ' + label + '</span>'
                + '<span class="font-bold ' + color + '">' + val + '</span>'
                + '<span class="text-[10px] ' + color + '">' + lbl + '</span>'
                + '</div>';
        }

        function renderVhf(sfi, kp, a, es, updated) {
            // ── Score global VHF ─────────────────────────────────────────────
            var score = 50;
            if (sfi !== null) score += Math.min(25, Math.max(-10, (sfi - 100) * 0.25));
            if (kp  !== null) score += kp<=2?10:kp<=4?0:kp<=6?-15:-30;
            if (a   !== null) score += a<=7?5:a<=20?-5:-20;
            if (es) score += 15;
            score = Math.max(0, Math.min(100, Math.round(score)));
            var scoreColor = score>=70?'text-emerald-400':score>=40?'text-yellow-400':'text-red-400';
            var barColor   = score>=70?'bg-emerald-500':score>=40?'bg-yellow-500':'bg-red-500';
            if (btn) btn.textContent = '↺ MAJ';

            // ── Conditions par bande ──────────────────────────────────────────
            // Chaque bande a une logique différente :
            //   HF basses (160/80/40m) : ionosphère couche F2, favorisées par SFI élevé,
            //     dégradées par Kp élevé (absorption polaire), absences de bruit à SFI faible
            //   HF hautes (20/17/15/12/10m) : ouvertures F2 fortes à SFI élevé,
            //     totalement fermées à SFI<100 sauf Sporadic-E
            //   6m : Sporadic-E saison avril-sept, ouvertures F2 exceptionnelles à SFI>200
            //   2m : trajet troposphérique standard, favorisé par stabilité géomagnétique
            //   70cm : peu affecté par le solaire, favorisé par conditions trop stables
            function bandScore(mhz) {
                var s = 50;
                if (mhz <= 3.5) {  // 160m / 80m – HF basses
                    if (sfi!==null) s += sfi>=120?10:sfi>=100?5:-5;
                    if (kp!==null)  s += kp<=2?10:kp<=4?0:kp<=6?-15:-25;
                    if (a!==null)   s += a<=7?5:a<=20?-10:-20;
                } else if (mhz <= 7.3) {  // 40m
                    if (sfi!==null) s += sfi>=130?15:sfi>=100?5:-5;
                    if (kp!==null)  s += kp<=2?8:kp<=4?0:-15;
                    if (a!==null)   s += a<=7?5:a<=20?-8:-18;
                } else if (mhz <= 14.35) { // 20m / 17m
                    if (sfi!==null) s += sfi>=150?20:sfi>=120?10:sfi>=100?0:-10;
                    if (kp!==null)  s += kp<=2?5:kp<=4?0:-10;
                } else if (mhz <= 29.7) { // 15m / 12m / 10m
                    if (sfi!==null) s += sfi>=180?25:sfi>=150?15:sfi>=120?0:-20;
                    if (kp!==null)  s += kp<=2?5:kp<=4?-5:-15;
                } else if (mhz <= 54) {  // 6m
                    if (es) s += 30; else s -= 10;
                    if (sfi!==null) s += sfi>=200?15:sfi>=150?5:-5;
                    if (kp!==null)  s += kp<=2?5:kp<=4?0:-10;
                } else {  // 2m / 70cm – VHF/UHF
                    if (kp!==null)  s += kp<=2?10:kp<=3?5:kp<=5?0:-10;
                    if (a!==null)   s += a<=7?8:a<=20?0:-10;
                    if (es && mhz<=146) s += 20;
                }
                return Math.max(0, Math.min(100, Math.round(s)));
            }

            function led(sc) {
                if (sc >= 70) return '<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#22c55e;box-shadow:0 0 6px #22c55e88;flex-shrink:0"></span>';
                if (sc >= 45) return '<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#eab308;box-shadow:0 0 6px #eab30888;flex-shrink:0"></span>';
                if (sc >= 25) return '<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#f97316;box-shadow:0 0 4px #f9731688;flex-shrink:0"></span>';
                return '<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#ef4444;box-shadow:0 0 4px #ef444488;flex-shrink:0"></span>';
            }
            function lbl(sc) {
                return sc>=70?'<span style="color:#22c55e">Bon</span>':sc>=45?'<span style="color:#eab308">Moyen</span>':sc>=25?'<span style="color:#f97316">Médiocre</span>':'<span style="color:#ef4444">Mauvais</span>';
            }

            var bands = [
                { name:'160m', mhz:1.85,  icon:'🌙' },
                { name:'80m',  mhz:3.65,  icon:'🌙' },
                { name:'40m',  mhz:7.1,   icon:'🌅' },
                { name:'20m',  mhz:14.2,  icon:'☀️' },
                { name:'15m',  mhz:21.2,  icon:'☀️' },
                { name:'10m',  mhz:28.5,  icon:'🔆' },
                { name:'6m',   mhz:50.1,  icon:'⚡' },
                { name:'2m',   mhz:144.8, icon:'📡' },
                { name:'70cm', mhz:432.0, icon:'📡' },
            ];
            var bandsHtml = '<div class="grid grid-cols-3 gap-1.5 mb-3">';
            bands.forEach(function(b) {
                var sc = bandScore(b.mhz);
                bandsHtml +=
                    '<div style="background:#0f172a;border:1px solid #1e293b;border-radius:10px;padding:6px 8px;display:flex;align-items:center;gap:6px">'
                    + led(sc)
                    + '<div style="min-width:0">'
                    +   '<div style="font-size:10px;font-weight:800;color:#cbd5e1;letter-spacing:.04em">' + b.icon + ' ' + b.name + '</div>'
                    +   '<div style="font-size:9px">' + lbl(sc) + '</div>'
                    + '</div>'
                    + '</div>';
            });
            bandsHtml += '</div>';

            box.innerHTML =
                '<div class="space-y-2 text-[11px] font-mono">'
                + bandsHtml
                + '<div style="border-top:1px solid #1e293b;padding-top:8px" class="space-y-1.5">'
                + row('☀️','SFI',     sfi!==null?sfi:'–',            sfiColor(sfi), sfiLbl(sfi))
                + row('🧲','Kp',      kp!==null?kp.toFixed(1):'–',  kpColor(kp),  kpLbl(kp))
                + row('📊','A-index', a!==null?a:'–',                aColor(a),    aLbl(a))
                + '<div class="flex justify-between items-center bg-slate-900/60 rounded-xl px-3 py-2">'
                +   '<span class="text-slate-500">⚡ Es VHF</span>'
                +   '<span class="text-[10px] font-bold ' + (es?'text-emerald-400':'text-slate-500') + '">'
                +     (es?'🟢 Probable':'⚪ Peu probable')
                +   '</span>'
                + '</div>'
                + '<div class="flex justify-between items-center mt-1">'
                +   '<span class="text-[10px] text-slate-500">Indice VHF global</span>'
                +   '<span class="font-black text-sm ' + scoreColor + '">' + score + '/100</span>'
                + '</div>'
                + '<div class="w-full bg-slate-800 rounded-full h-1.5">'
                +   '<div class="' + barColor + ' h-1.5 rounded-full" style="width:' + score + '%"></div>'
                + '</div>'
                + '</div>'
                + '<div class="text-[9px] text-slate-700 text-right mt-1">Src: NOAA SWPC · ' + (updated||'') + '</div>'
                + '</div>';
        }

        // ── Appel au proxy Flask /vhf_propagation (évite les problèmes CORS) ─
        // Le serveur Python récupère lui-même les données NOAA côté serveur.
        // Réponse : {sfi, k_index, a_index, hf_cond, vhf_cond, aurora, source}
        var updated = new Date().toLocaleTimeString();
        var month   = new Date().getMonth() + 1;

        fetch('/vhf_propagation', {cache: 'no-store'})
            .then(function(r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function(d) {
                var sfi = (d.sfi     !== undefined && d.sfi     !== null) ? parseFloat(d.sfi)     : null;
                var kp  = (d.k_index !== undefined && d.k_index !== null) ? parseFloat(d.k_index) : null;
                var a   = (d.a_index !== undefined && d.a_index !== null) ? parseFloat(d.a_index) : null;
                var esSeason = (month >= 4 && month <= 9);
                var es  = esSeason && (kp === null || kp <= 3);
                console.log('[PROP] sfi=' + sfi + ' kp=' + kp + ' a=' + a + ' src=' + d.source);
                renderVhf(sfi, kp, a, es, updated);
            })
            .catch(function(e) {
                console.error('[PROP] /vhf_propagation:', e);
                if (btn) btn.textContent = '↺ MAJ';
                box.innerHTML = '<div class="text-red-400 text-xs text-center py-2">⚠️ Serveur inaccessible — vérifier la connexion Internet du Raspberry Pi</div>';
            });
    }

    // ── Rafraîchissement automatique toutes les 10 minutes ────────────────
    setInterval(function() {
        // Ne rafraîchir que si l'onglet MAP est actif (économise les requêtes)
        var mapTab = document.getElementById('tab-map');
        if (mapTab && !mapTab.classList.contains('hidden')) vhfPropRefresh();
    }, 10 * 60 * 1000);

    // Intercept switchTab pour charger la propagation à l'ouverture de MAP
    (function() {
        var _orig = window.switchTab;
        window.switchTab = function(tabId) {
            _orig(tabId);
            if (tabId === 'map') vhfPropRefresh();
        };
    })();

    // ── iGate : calcul passcode + polling statut ───────────────────────────
    function igateCalcPasscode() {
        var call = (document.querySelector('[name=callsign]') || {}).value || '';
        call = call.toUpperCase().split('-')[0];
        if (!call) { alert('Saisissez le callsign dans les réglages station'); return; }
        var code = 0x73e2;
        for (var i = 0; i < call.length; i++) {
            if (i % 2 === 0) code ^= call.charCodeAt(i) << 8;
            else             code ^= call.charCodeAt(i);
        }
        var pc = (code & 0x7fff).toString();
        var el = document.getElementById('igate_passcode');
        if (el) { el.value = pc; el.focus(); }
    }

    function _updateIgateStatus() {
        fetch('/igate_status').then(function(r){ return r.json(); }).then(function(d) {
            var dot  = document.getElementById('igate-dot');
            var txt  = document.getElementById('igate-status-txt');
            var cnt1 = document.getElementById('igate-gated');
            var cnt2 = document.getElementById('igate-is-rx');
            if (!dot) return;
            if (!d.enabled) {
                dot.style.background = '#334155';
                dot.style.boxShadow  = 'none';
                if (txt) txt.textContent = 'Désactivé';
            } else if (d.connected) {
                dot.style.background = '#22c55e';
                dot.style.boxShadow  = '0 0 6px #22c55e88';
                if (txt) txt.textContent = 'En ligne';
            } else {
                dot.style.background = '#f59e0b';
                dot.style.boxShadow  = '0 0 5px #f59e0b88';
                if (txt) txt.textContent = d.status || 'Connexion…';
            }
            if (cnt1) cnt1.textContent = d.frames_gated || 0;
            if (cnt2) cnt2.textContent = d.frames_is_rx || 0;
        }).catch(function(){});
    }
    setInterval(_updateIgateStatus, 5000);
    _updateIgateStatus();

    // ── Répéteur toggle warning ────────────────────────────────────────────
    var _repToggle  = document.getElementById('repeater_toggle');

</script>

<!-- ══════════════════════════ MODALE AIDE ══════════════════════════════════ -->
<div id="modal-aide" class="hidden fixed inset-0 z-50 flex items-center justify-center p-4" style="background:rgba(2,6,23,0.92);backdrop-filter:blur(6px);">
    <div class="relative w-full max-w-3xl max-h-[90vh] flex flex-col glass rounded-[2rem] shadow-2xl border border-slate-700/60 text-slate-300 text-[11px] font-mono">

        <!-- En-tête -->
        <div class="sticky top-0 z-10 flex items-center justify-between px-6 py-4 bg-slate-900/90 border-b border-slate-700 rounded-t-[2rem] shrink-0">
            <span class="text-sm font-black text-amber-400 uppercase tracking-widest">❓ Notice d'utilisation — APRS Station</span>
            <button onclick="document.getElementById('modal-aide').classList.add('hidden')"
                class="text-slate-500 hover:text-red-400 font-black text-lg transition-colors">✕</button>
        </div>

        <!-- Onglets navigation -->
        <div class="flex border-b border-slate-700/60 bg-slate-900/60 shrink-0 overflow-x-auto">
            <button id="aide-tab-guide"     onclick="aideShowTab('guide')"     class="aide-tab active   px-4 py-2.5 text-[10px] font-black uppercase tracking-wider whitespace-nowrap border-b-2 border-blue-500  text-blue-400  transition-colors">📖 Guide</button>
            <button id="aide-tab-changelog" onclick="aideShowTab('changelog')" class="aide-tab inactive px-4 py-2.5 text-[10px] font-black uppercase tracking-wider whitespace-nowrap border-b-2 border-transparent text-slate-500 transition-colors hover:text-slate-300">🕓 Changelog</button>
            <button id="aide-tab-debug"     onclick="aideShowTab('debug')"     class="aide-tab inactive px-4 py-2.5 text-[10px] font-black uppercase tracking-wider whitespace-nowrap border-b-2 border-transparent text-slate-500 transition-colors hover:text-slate-300">🔧 Dépannage</button>
            <button id="aide-tab-auteur"    onclick="aideShowTab('auteur')"    class="aide-tab inactive px-4 py-2.5 text-[10px] font-black uppercase tracking-wider whitespace-nowrap border-b-2 border-transparent text-slate-500 transition-colors hover:text-slate-300">👤 Auteur</button>
        </div>

        <!-- Contenu scrollable -->
        <div class="overflow-y-auto flex-grow">

        <!-- ── GUIDE ── -->
        <div id="aide-panel-guide" class="px-6 py-5 space-y-6">

            <section>
                <h3 class="text-[10px] font-black text-blue-400 uppercase tracking-widest mb-3 flex items-center gap-2">🚀 Démarrage rapide</h3>
                <ol class="space-y-2 text-slate-400 list-decimal list-inside">
                    <li>Aller dans <span class="text-white font-bold">⚙️ RÉGLAGES</span> et renseigner l'indicatif, le locator Maidenhead et la position GPS.</li>
                    <li>Sélectionner le port série de Direwolf et cliquer <span class="text-white font-bold">💾 Sauvegarder</span>.</li>
                    <li>Activer la connexion en cliquant <span class="text-white font-bold">▶ CONNEXION</span> dans l'onglet <span class="text-white font-bold">📻 TRAFIC</span>.</li>
                    <li>Les trames APRS décodées apparaissent en temps réel dans la console.</li>
                </ol>
            </section>

            <hr class="border-slate-700/60"/>

            <section>
                <h3 class="text-[10px] font-black text-blue-400 uppercase tracking-widest mb-3 flex items-center gap-2">📻 Onglet TRAFIC</h3>
                <div class="space-y-2 text-slate-400">
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">📨 Envoyer message</span><span>Saisir indicatif destinataire + texte → <span class="text-white">ENVOYER</span>. Confirmation ACK attendue automatiquement. Les messages sont sauvegardés dans <span class="text-white font-mono">chat.json</span> et restaurés au redémarrage.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🛸 Beacon ISS</span><span>Émet une balise positionnée via ARISS (QRG 145.825 MHz).</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">📡 Beacon Station</span><span>Émet la balise de position standard avec commentaire et locator.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🌦️ Beacon Météo</span><span>Récupère les données Open-Meteo (lat/lon depuis locator) et émet un beacon APRS météo format <span class="text-emerald-400">@…_</span>.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">📶 Beacon Propagation</span><span>Interroge NOAA SWPC (SFI, Kp, A-index) et émet un beacon statut <span class="text-emerald-400">&gt;SFI:NNN K:N.N HF:xxx VHF:yyy {NOAA}</span>.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🛰️ Envoyer statut</span><span>Émet le statut texte libre configuré dans les réglages (trame <span class="text-emerald-400">&gt;</span>).</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Console</span><span>Affiche les trames TX/RX. Clic sur un indicatif → fiche QRZ.com. Clic sur les coordonnées → onglet MAP.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🛸 Passages ISS</span><span>Widget compact affichant les 5 prochains passages de l'ISS. Mis à jour toutes les 15 min. Le bouton <span class="text-white font-bold">↺ MAJ</span> force un rafraîchissement.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Beacons auto</span><span>Chaque type de balise possède son propre intervalle configurable dans <span class="text-white font-bold">⚙️ RÉGLAGES</span>. Le temps restant avant la prochaine émission est affiché en temps réel.</span></div>
                </div>
            </section>

            <hr class="border-slate-700/60"/>

            <section>
                <h3 class="text-[10px] font-black text-blue-400 uppercase tracking-widest mb-3 flex items-center gap-2">⚙️ Onglet RÉGLAGES</h3>
                <div class="space-y-2 text-slate-400">
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Indicatif</span><span>Indicatif complet avec SSID si besoin, ex : <span class="text-white">F4XXX-9</span>.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Locator</span><span>Carré Maidenhead 6 caractères, ex : <span class="text-white">JN07II</span>. Utilisé pour les beacons météo, propagation et alertes ISS.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Port série</span><span>Port KISS TCP de Direwolf (ex : <span class="text-white">/dev/ttyUSB0</span> ou <span class="text-white">COM3</span>). La PTT est gérée directement par Direwolf via <span class="text-white font-mono">direwolf.conf</span>.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Beacons auto</span><span>Chaque type (Station, ISS, Météo, Propagation) possède son propre intervalle en minutes (0 = désactivé). Recommandé : 10–30 min.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Chemin APRS</span><span><span class="text-white">WIDE1-1,WIDE2-1</span> pour portée nationale. <span class="text-white">WIDE1-1</span> pour usage local uniquement.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Commentaire</span><span>Texte libre joint à la balise station (max 43 caractères).</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🛸 Alertes ISS</span><span>Active la bannière + bip avant chaque passage de l'ISS. Nécessite un locator ou des coordonnées valides.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🌩️ Alertes Météo</span><span>Définir les seuils (température, vent, rafales, pluie, pression, codes WMO) et l'intervalle de vérification.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🔔 Notifications</span><span>Notifications Web Push navigateur pour recevoir les alertes même avec l'onglet en arrière-plan.</span></div>
                </div>
            </section>

            <hr class="border-slate-700/60"/>

            <section>
                <h3 class="text-[10px] font-black text-emerald-400 uppercase tracking-widest mb-3 flex items-center gap-2">📡 iGate APRS-IS</h3>
                <div class="space-y-2 text-slate-400">
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Rôle</span><span>Passerelle RF ↔ Internet APRS-IS. Rend les stations reçues visibles sur <span class="text-white font-mono">aprs.fi</span>.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">RX-iGate <span class="text-slate-600 font-mono">/R</span></span><span>Mode lecture seule : transfère les paquets RF reçus vers APRS-IS. Recommandé pour la plupart des installations.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Full iGate <span class="text-slate-600 font-mono">/&amp;</span></span><span>En plus du RX, retransmet en RF les paquets APRS-IS destinés aux stations locales. Nécessite licence et puissance adaptée.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Serveur</span><span>Par défaut <span class="text-white font-mono">rotate.aprs2.net:14580</span>. Serveur régional recommandé : <span class="text-white font-mono">euro.aprs2.net</span>.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Passcode</span><span>Code calculé depuis l'indicatif. Cliquer <span class="text-white font-bold">🔑 Calculer</span>. La valeur <span class="text-white">-1</span> donne accès lecture seule.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Filtre IS</span><span>Filtre serveur APRS-IS. Exemple : <span class="text-white font-mono">r/46.5/1.5/200</span> = cercle 200 km autour de lat 46.5 / lon 1.5.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Reconnexion</span><span>Reconnexion automatique toutes les 30 s. Keepalive TCP toutes les 60 s.</span></div>
                </div>
            </section>

            <hr class="border-slate-700/60"/>

            <section>
                <h3 class="text-[10px] font-black text-blue-400 uppercase tracking-widest mb-3 flex items-center gap-2">💬 QSO · 🗺️ MAP · 🛰️ ISS · 📊 STATS</h3>
                <div class="space-y-2 text-slate-400">
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">💬 QSO</span><span>Messagerie APRS par indicatif. Historique persistant dans <span class="text-white font-mono">chat.json</span>. Badge rouge = messages non lus. ACK automatique.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🗺️ MAP</span><span>Carte temps réel des stations APRS. Clic marqueur → infos PHG, vitesse, altitude. Widget <span class="text-violet-400">📶 Propagation VHF</span> NOAA en direct.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🛰️ ISS</span><span>Suivi temps réel via OrbTrack. APRS : <span class="text-emerald-400">145.825 MHz</span> · Voice FM : <span class="text-emerald-400">437.800 MHz</span>.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">📊 STATS</span><span>Histogramme 24 h (TX / RX / IS) et top 10 des stations les plus actives. Persisté dans <span class="text-white font-mono">stats.json</span>.</span></div>
                </div>
            </section>

            <hr class="border-slate-700/60"/>

            <section>
                <h3 class="text-[10px] font-black text-violet-400 uppercase tracking-widest mb-3 flex items-center gap-2">📶 Indice de propagation</h3>
                <div class="space-y-2 text-slate-400">
                    <div class="flex gap-3"><span class="text-slate-500 w-24 shrink-0">SFI</span><span>Flux solaire 10,7 cm. &lt;70 = mauvais · 70–100 = faible · 100–120 = correct · 120–150 = bon · &gt;150 = excellent.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-24 shrink-0">Kp</span><span>Indice géomagnétique planétaire (0–9). &lt;3 = calme · 3–5 = agité · &gt;5 = tempête → aurora possible en VHF.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-24 shrink-0">A-index</span><span>Indice géomagnétique journalier. &lt;7 = stable · 7–20 = instable · &gt;30 = perturbé.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-24 shrink-0">Es VHF</span><span>Sporadic-E probable en saison avril–septembre si Kp ≤ 3. Peut ouvrir des liaisons jusqu'à 2000 km.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-24 shrink-0">Score /100</span><span>Indice synthétique calculé depuis SFI + Kp + A + saison Es. Source : NOAA SWPC.</span></div>
                </div>
            </section>

            <hr class="border-slate-700/60"/>

            <section>
                <h3 class="text-[10px] font-black text-violet-400 uppercase tracking-widest mb-3 flex items-center gap-2">🛸 Alerte Passage ISS</h3>
                <div class="space-y-2 text-slate-400">
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Activation</span><span>Dans <span class="text-white font-bold">⚙️ RÉGLAGES</span>, section «&nbsp;Alertes Passage ISS&nbsp;». Activer le toggle et définir l'avance souhaitée.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Source</span><span>API <span class="text-white font-mono">open-notify.org</span> (gratuite, sans clé). Interrogée toutes les 60 s côté serveur.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Bannière &amp; bip</span><span>Bannière violette + triple bip 440/660/880 Hz X minutes avant le passage. Cliquer pour fermer.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Voyant header</span><span>Point <span class="text-violet-400 font-bold">● ISS</span> animé sous «&nbsp;Station Active&nbsp;» quand l'alerte est activée.</span></div>
                </div>
            </section>

            <hr class="border-slate-700/60"/>

            <section>
                <h3 class="text-[10px] font-black text-sky-400 uppercase tracking-widest mb-3 flex items-center gap-2">🌩️ Alertes Météo</h3>
                <div class="space-y-2 text-slate-400">
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Source</span><span>API <span class="text-white font-mono">Open-Meteo</span> (gratuite, sans clé). Température, vent, rafales, précipitations, pression, code WMO.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">6 types d'alerte</span><span><span class="text-white">Température basse/haute</span> · <span class="text-white">Vent soutenu</span> · <span class="text-white">Rafales</span> · <span class="text-white">Pluie intense</span> · <span class="text-white">Dépression</span> · <span class="text-white">Code WMO</span>. Chaque type activable indépendamment.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Codes WMO</span><span>Défaut : <span class="text-white font-mono">95,96,99</span> (orages) · <span class="text-white font-mono">71,73,75</span> (neige) · <span class="text-white font-mono">45,48</span> (brouillard).</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Bannière &amp; bip</span><span>Bannière bleue + bip 880 Hz. Diffusée via SSE même onglet fermé.</span></div>
                </div>
            </section>

            <div class="text-center pt-2 pb-1 text-slate-700 text-[9px]">Py-APRS v2.2 · Direwolf backend · 73 de F1RIQ</div>
        </div><!-- /aide-panel-guide -->

        <!-- ── CHANGELOG ── -->
        <div id="aide-panel-changelog" class="hidden px-6 py-5 space-y-5">
            <div id="changelog-container">
                <div class="text-slate-600 text-[10px] italic text-center py-8">⏳ Chargement du changelog...</div>
            </div>
        </div><!-- /aide-panel-changelog -->

        <!-- ── DÉPANNAGE ── -->
        <div id="aide-panel-debug" class="hidden px-6 py-5 space-y-5">
            <section>
                <h3 class="text-[10px] font-black text-red-400 uppercase tracking-widest mb-3 flex items-center gap-2">🔧 Dépannage</h3>
                <div class="space-y-2 text-slate-400">
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Pas de trames reçues</span><span>Vérifier que Direwolf est lancé, le port série correct et le câble audio branché sur l'entrée carte son.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Beacon météo échoue</span><span>Locator Maidenhead absent ou invalide. Vérifier dans les réglages (format JN07XX).</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">NOAA inaccessible</span><span>Vérifier la connexion Internet du serveur. Le widget propagation fonctionne aussi depuis le navigateur client si le serveur est hors ligne.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">MAP vide</span><span>Normal si aucune trame avec coordonnées GPS n'a été reçue depuis le démarrage. Les stations sans position ne sont pas cartographiées.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Passages ISS absents</span><span><span class="text-white font-mono">open-notify.org</span> inaccessible ou position non configurée. Vérifier le locator dans les réglages et la connexion Internet du serveur.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Alerte météo muette</span><span>Vérifier que le toggle est actif et que la position (locator ou lat/lon) est renseignée. Cliquer <span class="text-white font-bold">🔍 Vérifier maintenant</span> pour diagnostiquer.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Voyant ISS absent</span><span>Normal si l'alerte ISS n'est pas activée dans les Réglages. Le voyant n'apparaît que lorsque le toggle est actif.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">iGate : pastille orange</span><span>Serveur APRS-IS inaccessible. Vérifier connexion Internet, <span class="text-white font-mono">rotate.aprs2.net</span> et port <span class="text-white font-mono">14580</span>. Reconnexion automatique toutes les 30 s.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">iGate : compteur bloqué</span><span>Vérifier que l'iGate est activé, le passcode correct (bouton <span class="text-white font-bold">🔑 Calculer</span>) et que Direwolf reçoit bien des trames.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Beacon TX échoue</span><span>Socket KISS Direwolf non disponible. Vérifier que Direwolf écoute sur le port KISS TCP (défaut 8001). Redémarrer le serveur Python si nécessaire.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Lien QRZ absent</span><span>L'indicatif <span class="text-white font-mono">BEACON</span>, <span class="text-white font-mono">APRS</span> ou <span class="text-white font-mono">?</span> n'ont pas de lien QRZ (adresses non callsign). Seuls les vrais indicatifs sont cliquables.</span></div>
                </div>
            </section>
        </div><!-- /aide-panel-debug -->

        <!-- ── AUTEUR ── -->
        <div id="aide-panel-auteur" class="hidden px-6 py-5 space-y-5">
            <section>
                <h3 class="text-[10px] font-black text-amber-400 uppercase tracking-widest mb-3 flex items-center gap-2">👤 Auteur &amp; Conditions d'utilisation</h3>

                <!-- Carte auteur -->
                <div class="flex items-center gap-4 bg-slate-900/60 rounded-2xl px-4 py-3 mb-4 border border-slate-700/50">
                    <div class="w-12 h-12 rounded-xl bg-gradient-to-br from-blue-600 to-indigo-700 flex items-center justify-center text-white font-black text-lg shrink-0">F1</div>
                    <div>
                        <div class="text-white font-black text-sm tracking-tight">F1RIQ</div>
                        <div class="text-slate-400 text-[10px] mt-0.5">Radioamateur · Développeur · France · JN07II</div>
                        <div class="text-slate-600 text-[9px] mt-0.5"><a href="mailto:tonyf1riq@gmail.com" class="hover:text-blue-400 transition-colors">tonyf1riq@gmail.com</a></div>
                        <div class="text-slate-600 text-[9px] mt-1">Py-APRS · version 2.2 · Backend Dire Wolf</div>
                    </div>
                    <div class="ml-auto text-right shrink-0">
                        <div class="text-emerald-400 text-[10px] font-bold">✅ Logiciel libre</div>
                        <div class="text-slate-600 text-[9px]">usage non-commercial</div>
                    </div>
                </div>

                <div class="space-y-2 text-slate-400">
                    <div class="flex gap-3 items-start"><span class="text-emerald-400 shrink-0 mt-0.5">✅</span><span><span class="text-white font-bold">Radioamateurs titulaires d'une licence</span> — Utilisation personnelle, individuelle et non commerciale autorisée.</span></div>
                    <div class="flex gap-3 items-start"><span class="text-emerald-400 shrink-0 mt-0.5">✅</span><span><span class="text-white font-bold">SWL</span> — Utilisation en réception uniquement autorisée.</span></div>
                    <div class="flex gap-3 items-start"><span class="text-red-400/80 shrink-0 mt-0.5">⛔</span><span><span class="text-white font-bold">Usage commercial ou redistribution</span> — Interdit sans accord écrit de l'auteur.</span></div>
                    <div class="flex gap-3 items-start"><span class="text-red-400/80 shrink-0 mt-0.5">⛔</span><span><span class="text-white font-bold">Émission sans licence</span> — Illégale et expressément interdite. L'opérateur est seul responsable de ses émissions.</span></div>
                    <div class="flex gap-3 items-start"><span class="text-yellow-400/80 shrink-0 mt-0.5">⚠️</span><span><span class="text-white font-bold">Absence de garantie</span> — Logiciel fourni « tel quel ». Vérifiez la conformité avec la réglementation de votre pays.</span></div>
                </div>

                <div class="mt-6 pt-5 border-t border-slate-700/50">
                    <h3 class="text-[10px] font-black text-sky-400 uppercase tracking-widest mb-4 flex items-center gap-2">📦 Téléchargement &amp; Communauté</h3>
                    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <div class="bg-slate-900/60 rounded-2xl px-4 py-3 border border-slate-700/50">
                            <div class="flex items-center gap-2 mb-2"><span class="text-xl">📦</span><span class="text-white font-bold text-xs">Code source &amp; téléchargement</span></div>
                            <p class="text-slate-400 text-[10px] leading-relaxed mb-2">Py-APRS est distribué librement via le GitHub de la communauté LesF4. Dernière version, patches et historique complet des modifications.</p>
                            <a href="https://github.com/LesF4" target="_blank" rel="noopener" class="inline-flex items-center gap-1.5 text-sky-400 hover:text-sky-300 text-[10px] font-mono font-bold transition-colors">
                                <svg class="w-3 h-3" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844a9.59 9.59 0 012.504.337c1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.02 10.02 0 0022 12.017C22 6.484 17.522 2 12 2z"/></svg>
                                github.com/LesF4
                            </a>
                        </div>
                        <div class="bg-slate-900/60 rounded-2xl px-4 py-3 border border-slate-700/50">
                            <div class="flex items-center gap-2 mb-2"><span class="text-xl">📻</span><span class="text-white font-bold text-xs">La communauté LesF4</span></div>
                            <p class="text-slate-400 text-[10px] leading-relaxed">Groupe d'amateurs radio francophones passionnés, actifs sur les bandes, les concours et les modes numériques. Merci à eux pour leurs retours et encouragements.</p>
                            <p class="text-slate-500 text-[9px] mt-2">73 &amp; bonne propagation ! 🌍</p>
                        </div>
                    </div>
                </div>

                <div class="mt-4 text-center text-slate-600 text-[9px]">© 2024–2026 F1RIQ · Tous droits réservés · 73 &amp; bonne propagation !</div>
            </section>
        </div><!-- /aide-panel-auteur -->

        </div><!-- /overflow-y-auto -->
    </div>
</div>

<script>
// ── Navigation onglets Aide ───────────────────────────────────────────────────
function aideShowTab(name) {
    ['guide','changelog','debug','auteur'].forEach(function(t) {
        var panel = document.getElementById('aide-panel-' + t);
        var btn   = document.getElementById('aide-tab-' + t);
        if (panel) panel.classList.add('hidden');
        if (btn) {
            btn.classList.remove('border-blue-500','text-blue-400');
            btn.classList.add('border-transparent','text-slate-500');
        }
    });
    var p = document.getElementById('aide-panel-' + name);
    var b = document.getElementById('aide-tab-' + name);
    if (p) p.classList.remove('hidden');
    if (b) {
        b.classList.remove('border-transparent','text-slate-500');
        b.classList.add('border-blue-500','text-blue-400');
    }
    if (name === 'changelog') _aideLoadChangelog();
}

// ── Chargement changelog depuis /version ─────────────────────────────────────
var _changelogLoaded = false;
function _aideLoadChangelog() {
    if (_changelogLoaded) return;
    fetch('/version').then(function(r){ return r.json(); }).then(function(d) {
        _changelogLoaded = true;
        var el = document.getElementById('changelog-container');
        if (!el || !d.changelog) return;
        var labels = { current: '<span style="background:#1d4ed8;color:#bfdbfe;font-size:8px;font-weight:900;border-radius:6px;padding:1px 7px;margin-left:6px;text-transform:uppercase;letter-spacing:.07em">Actuel</span>' };
        el.innerHTML = d.changelog.map(function(v) {
            var labelHtml = v.label && labels[v.label] ? labels[v.label] : '';
            return '<div style="border-left:2px solid #1e293b;padding-left:14px;margin-bottom:20px">'
                 + '<div style="display:flex;align-items:center;margin-bottom:8px">'
                 + '<span style="font-size:13px;font-weight:900;color:#f1f5f9;font-family:monospace">v' + v.version + '</span>'
                 + labelHtml
                 + '<span style="margin-left:8px;font-size:9px;color:#475569;font-family:monospace">' + v.date + '</span>'
                 + '</div>'
                 + '<ul style="list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:5px">'
                 + v.changes.map(function(c) {
                     return '<li style="display:flex;gap:8px;font-size:10px;color:#94a3b8">'
                          + '<span style="color:#3b82f6;flex-shrink:0;margin-top:1px">▸</span>'
                          + '<span>' + c + '</span>'
                          + '</li>';
                 }).join('')
                 + '</ul></div>';
        }).join('');
    }).catch(function(){ document.getElementById('changelog-container').innerHTML = '<p style="color:#ef4444;font-size:10px;text-align:center;padding:2rem">Impossible de charger le changelog.</p>'; });
}

// Fermer avec Escape
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') document.getElementById('modal-aide').classList.add('hidden');
});
</script>

<script>
// ════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════════════
// Web Push Notifications (sans VAPID — Notification API locale)
// ══════════════════════════════════════════════════════════════
var _swReg = null;   // ServiceWorkerRegistration

function _updateNotifBtn() {
    try {
    var btn = document.getElementById('notif-btn');
    var st  = document.getElementById('notif-status');
    if (!btn) return;
    if (typeof Notification === 'undefined' || !('Notification' in window) || !('serviceWorker' in navigator)) {
        btn.textContent = '🚫 Non supporté';
        btn.disabled    = true;
        if (st) st.textContent = 'Ce navigateur ne supporte pas les notifications.';
        return;
    }
    var p = Notification.permission;
    if (p === 'granted') {
        btn.textContent  = '🔔 Notifications actives';
        btn.className    = btn.className.replace(/bg-slate-800|bg-blue-700|bg-red-900\/30/g, '')
                         + ' bg-blue-700 border-blue-500 text-white';
        if (st) st.textContent = 'QSO reçus et alertes ISS notifiés.';
    } else if (p === 'denied') {
        btn.textContent  = '🔕 Bloquées par le navigateur';
        btn.className    = btn.className.replace(/bg-slate-800|bg-blue-700|bg-red-900\/30/g, '')
                         + ' bg-red-900/30 border-red-800 text-red-400';
        if (st) st.textContent = 'Autorisez les notifications dans les réglages du navigateur.';
    } else {
        btn.textContent  = '🔔 Activer les notifications';
        btn.className    = btn.className.replace(/bg-blue-700|bg-red-900\/30/g, '')
                         + ' bg-slate-800 border-slate-700 text-slate-300 hover:border-blue-600 hover:text-blue-300';
        if (st) st.textContent = '';
    }
    } catch(e) {}
}

async function _registerSW() {
    if (!('serviceWorker' in navigator)) return null;
    try {
        _swReg = await navigator.serviceWorker.register('/sw.js', {scope: '/'});
        console.log('[SW] Enregistré — scope :', _swReg.scope);
        return _swReg;
    } catch(err) {
        console.warn('[SW] Echec enregistrement :', err);
        return null;
    }
}

async function togglePushNotif() {
    if (!('Notification' in window)) return;
    if (Notification.permission === 'granted') {
        /* Déjà actif — on informe simplement l'utilisateur */
        _pushNotif('🔔 Py-APRS', 'Les notifications sont déjà actives.', 'test');
        return;
    }
    if (Notification.permission === 'denied') { _updateNotifBtn(); return; }
    /* Demander la permission */
    var result = await Notification.requestPermission();
    _updateNotifBtn();
    if (result === 'granted') {
        if (!_swReg) await _registerSW();
        _pushNotif('🔔 Py-APRS', 'Notifications activées — QSO et alertes ISS.', 'init');
    }
}

function _pushNotif(title, body, tag) {
    if (typeof Notification === 'undefined' || !('Notification' in window) || Notification.permission !== 'granted') return;
    /* Utiliser le Service Worker si disponible (persistant en arrière-plan) */
    if (_swReg && _swReg.active) {
        _swReg.showNotification(title, {body: body, tag: tag || 'aprs', renotify: true, requireInteraction: false});
    } else {
        /* Fallback Notification() classique */
        try { new Notification(title, {body: body, tag: tag || 'aprs'}); } catch(e) {}
    }
}

/* Init au chargement — synchrone pour ne pas bloquer le script */
try { _updateNotifBtn(); } catch(e) {}
if ('serviceWorker' in navigator && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
    _registerSW().catch(function(){});
}
</script><script>
// ══════════════════════════════════════════════════════════════════════════════
// 📓 CARNET DE TRAFIC — JavaScript
// ══════════════════════════════════════════════════════════════════════════════
(function() {
    var _page    = 1;
    var _perPage = 50;
    var _total   = 0;
    var _debounceTimer = null;

    // ── Exposition globale ───────────────────────────────────────────────────
    window.lbRefresh   = lbRefresh;
    window.lbPrevPage  = lbPrevPage;
    window.lbNextPage  = lbNextPage;
    window.lbExport    = lbExport;
    window.lbImport    = lbImport;
    window.lbClearAll  = lbClearAll;
    window.lbSaveNote  = lbSaveNote;
    window.lbDelete    = lbDelete;

    // ── Helpers ──────────────────────────────────────────────────────────────
    function _e(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

    function _dirBadge(dir) {
        if (dir === 'TX') return '<span style="background:#1d4ed8;color:#bfdbfe;border-radius:6px;padding:1px 7px;font-size:8px;font-weight:900">📤 TX</span>';
        if (dir === 'RX') return '<span style="background:#065f46;color:#6ee7b7;border-radius:6px;padding:1px 7px;font-size:8px;font-weight:900">📥 RX</span>';
        return '<span style="background:#334155;color:#94a3b8;border-radius:6px;padding:1px 7px;font-size:8px">' + _e(dir) + '</span>';
    }

    function _typeBadge(t) {
        var colors = {
            'Position':'#1e3a5f','Message':'#1e293b','Mic-E':'#312e81',
            'Meteo':'#0c4a6e','Statut':'#292524','Objet':'#451a03',
            'Telemetrie':'#1e1b4b','Beacon':'#14532d','Propagation':'#2e1065',
        };
        var bg = colors[t] || '#1e293b';
        return '<span style="background:' + bg + ';color:#cbd5e1;border-radius:6px;padding:1px 6px;font-size:8px;white-space:nowrap">' + _e(t||'?') + '</span>';
    }

    function _posCell(e) {
        if (e.lat == null || e.lon == null) return '<span style="color:#334155">–</span>';
        var la = parseFloat(e.lat).toFixed(4);
        var lo = parseFloat(e.lon).toFixed(4);
        return '<a href="https://www.openstreetmap.org/?mlat=' + la + '&mlon=' + lo + '&zoom=14" target="_blank" style="color:#38bdf8;font-size:9px;font-family:monospace">📍' + la + '</a>';
    }

    function _srcBadge(src) {
        if (src === 'IS') return '<span style="color:#a78bfa;font-size:9px">🌐 IS</span>';
        if (src === 'ADIF') return '<span style="color:#fbbf24;font-size:9px">📄 ADIF</span>';
        if (src === 'import') return '<span style="color:#94a3b8;font-size:9px">📥</span>';
        return '<span style="color:#6ee7b7;font-size:9px">📡 RF</span>';
    }

    // ── Chargement / rendu ────────────────────────────────────────────────────
    function lbRefresh() {
        clearTimeout(_debounceTimer);
        _debounceTimer = setTimeout(function() { _page = 1; _load(); }, 250);
    }

    function _load() {
        var search = (document.getElementById('lb-search')||{}).value || '';
        var dir    = (document.getElementById('lb-dir')||{}).value || '';
        var type   = (document.getElementById('lb-type')||{}).value || '';
        var url    = '/logbook/entries?page=' + _page + '&per_page=' + _perPage
                   + '&search=' + encodeURIComponent(search)
                   + '&direction=' + encodeURIComponent(dir)
                   + '&aprs_type=' + encodeURIComponent(type);
        fetch(url).then(function(r){
            if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + r.statusText);
            return r.json();
        }).then(function(d) {
            _total = d.total || 0;
            _render(d.entries || []);
            _updatePager();
            // Compteur label
            var lbl = document.getElementById('lb-count-label');
            if (lbl) lbl.textContent = _total + ' entrée(s)';
        }).catch(function(err){
            var tbody = document.getElementById('lb-tbody');
            if (tbody) tbody.innerHTML = '<tr><td colspan="10" style="padding:2rem;text-align:center;'
                + 'color:#ef4444;font-family:monospace;font-size:11px">'
                + '❌ Erreur chargement carnet : ' + err + '<br>'
                + '<span style=\'color:#64748b\'>Vérifiez que le serveur Flask tourne correctement.</span>'
                + '</td></tr>';
            console.error('[logbook] _load error:', err);
        });
    }

    function _render(entries) {
        var tbody = document.getElementById('lb-tbody');
        if (!tbody) return;
        if (!entries.length) {
            tbody.innerHTML = '<tr><td colspan="10" style="padding:3rem;text-align:center;color:#475569;font-style:italic">Aucune entrée dans le carnet</td></tr>';
            return;
        }
        tbody.innerHTML = entries.map(function(e, i) {
            var rowBg = i % 2 === 0 ? '' : 'background:rgba(15,23,42,0.3)';
            var tsShort = (e.ts || '').substring(0, 16);
            return '<tr style="border-bottom:1px solid #0f172a;' + rowBg + '" id="lb-row-' + e.id + '">'
                // Date/Heure
                + '<td style="padding:6px 12px;white-space:nowrap;color:#64748b;font-size:10px">' + _e(tsShort) + '</td>'
                // Direction
                + '<td style="padding:6px 8px">' + _dirBadge(e.direction) + '</td>'
                // Callsign
                + '<td style="padding:6px 8px">'
                +   (e.callsign && e.callsign !== 'APRS' && e.callsign !== '?'
                        ? '<a href="https://www.qrz.com/db/' + encodeURIComponent(e.callsign.split('-')[0]) + '" target="_blank" style="color:#60a5fa;font-weight:700;font-family:monospace;font-size:11px" onclick="event.stopPropagation()">' + _e(e.callsign) + '</a>'
                        : '<span style="color:#475569">' + _e(e.callsign||'?') + '</span>')
                + '</td>'
                // Dest
                + '<td style="padding:6px 8px;color:#94a3b8;font-size:10px">' + _e(e.dest||'') + '</td>'
                // Type
                + '<td style="padding:6px 8px">' + _typeBadge(e.aprs_type) + '</td>'
                // Commentaire
                + '<td style="padding:6px 8px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#cbd5e1;font-size:10px" title="' + _e(e.comment||'') + '">'
                +   _e((e.comment||'').substring(0,60)) + '</td>'
                // Pos
                + '<td style="padding:6px 8px">' + _posCell(e) + '</td>'
                // Source
                + '<td style="padding:6px 8px">' + _srcBadge(e.source) + '</td>'
                // Note (éditable)
                + '<td style="padding:4px 6px;min-width:120px">'
                +   '<input type="text" value="' + _e(e.note||'') + '" placeholder="Note..." maxlength="200"'
                +   ' style="background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:3px 8px;color:#94a3b8;font-size:10px;width:100%;outline:none"'
                +   ' onblur="lbSaveNote(' + e.id + ', this.value)"'
                +   ' onkeydown="if(event.key===\'Enter\') this.blur()">'
                + '</td>'
                // Actions
                + '<td style="padding:4px 8px;text-align:center">'
                +   '<button onclick="lbDelete(' + e.id + ')" title="Supprimer" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:13px;padding:2px 5px" '
                +   'class="hover:opacity-70 transition-opacity">🗑</button>'
                + '</td>'
                + '</tr>';
        }).join('');
    }

    function _updatePager() {
        var pages = Math.max(1, Math.ceil(_total / _perPage));
        var info  = document.getElementById('lb-page-info');
        var prev  = document.getElementById('lb-prev');
        var next  = document.getElementById('lb-next');
        if (info) info.textContent = 'Page ' + _page + ' / ' + pages + ' (' + _total + ')';
        if (prev) prev.disabled = (_page <= 1);
        if (next) next.disabled = (_page >= pages);
    }

    function lbPrevPage() { if (_page > 1) { _page--; _load(); } }
    function lbNextPage() {
        var pages = Math.ceil(_total / _perPage);
        if (_page < pages) { _page++; _load(); }
    }

    // ── Actions ───────────────────────────────────────────────────────────────
    function lbSaveNote(id, note) {
        fetch('/logbook/note', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({id: id, note: note})
        }).catch(function(err){ console.error('[logbook] saveNote error:', err); });
    }

    function lbDelete(id) {
        if (!confirm('Supprimer cette entrée du carnet ?')) return;
        fetch('/logbook/delete', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({id: id})
        }).then(function(){ _load(); _loadStats(); }).catch(function(){});
    }

    function lbClearAll() {
        if (!confirm('Vider tout le carnet de trafic ? Cette action est irréversible.')) return;
        fetch('/logbook/clear', {method:'POST'}).then(function(){ _load(); _loadStats(); }).catch(function(){});
    }

    // ── Export ────────────────────────────────────────────────────────────────
    function lbExport(fmt) {
        var search = (document.getElementById('lb-search')||{}).value || '';
        var dir    = (document.getElementById('lb-dir')||{}).value || '';
        var type   = (document.getElementById('lb-type')||{}).value || '';
        var url    = '/logbook/export/' + fmt
                   + '?search=' + encodeURIComponent(search)
                   + '&direction=' + encodeURIComponent(dir)
                   + '&aprs_type=' + encodeURIComponent(type);
        window.open(url, '_blank');
    }

    // ── Import ────────────────────────────────────────────────────────────────
    function lbImport(input) {
        var file = input.files[0];
        if (!file) return;
        var fd = new FormData();
        fd.append('file', file);
        fetch('/logbook/import', {method:'POST', body: fd})
            .then(function(r){ return r.json(); })
            .then(function(d) {
                if (d.error) { alert('Erreur import : ' + d.error); return; }
                alert('✅ ' + d.imported + ' entrée(s) importée(s).');
                _load(); _loadStats();
            })
            .catch(function(){ alert('Erreur réseau lors de l\'import.'); });
        input.value = '';
    }

    // ── Statistiques rapides ──────────────────────────────────────────────────
    function _loadStats() {
        fetch('/logbook/stats').then(function(r){
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        }).then(function(d) {
            function _s(id, v){ var e=document.getElementById(id); if(e) e.textContent=v; }
            _s('lb-stat-total', d.total  || 0);
            _s('lb-stat-rx',    d.rx     || 0);
            _s('lb-stat-tx',    d.tx     || 0);
            _s('lb-stat-calls', d.unique_calls || 0);
        }).catch(function(err){ console.error('[logbook] stats error:', err); });
    }

    // ── Auto-refresh quand l'onglet devient actif ──────────────────────────
    document.addEventListener('aprs-switchtab', function(ev) {
        if (ev.detail === 'logbook') {
            _load();
            _loadStats();
        }
    });

    // ── Mise à jour des stats quand une nouvelle trame arrive (SSE) ──────────
    document.addEventListener('aprs-frame', function() {
        // Refresh stats uniquement si l'onglet est actif (pas de spam)
        var tab = document.getElementById('tab-logbook');
        if (tab && !tab.classList.contains('hidden')) {
            clearTimeout(_debounceTimer);
            _debounceTimer = setTimeout(function() { _load(); _loadStats(); }, 1500);
        }
    });

    // Chargement initial : stats + entrées du carnet
    _loadStats();
    _load();
})();
</script>

<script>
// ══════════════════════════════════════════════════════════════════════════════
// 🌊 WAVELOG — JavaScript
// ══════════════════════════════════════════════════════════════════════════════
(function() {
    function _updateWavelogStatus() {
        fetch('/wavelog/status').then(function(r){ return r.json(); }).then(function(d) {
            var dot    = document.getElementById('wavelog-dot');
            var txt    = document.getElementById('wavelog-status-txt');
            var synced = document.getElementById('wavelog-synced');
            var lastSy = document.getElementById('wavelog-last-sync');
            if (!dot) return;
            if (!d.enabled) {
                dot.style.background = '#334155'; dot.style.boxShadow = 'none';
                if (txt) txt.textContent = 'Désactivé';
            } else if (d.connected) {
                dot.style.background = '#06b6d4'; dot.style.boxShadow = '0 0 6px #06b6d488';
                if (txt) txt.textContent = d.status || 'Connecté';
            } else {
                dot.style.background = '#f59e0b'; dot.style.boxShadow = '0 0 5px #f59e0b88';
                if (txt) txt.textContent = d.status || 'Déconnecté';
            }
            if (synced) synced.textContent = d.synced_total || 0;
            if (lastSy) lastSy.textContent = d.last_sync || '–';
        }).catch(function(){});
    }
    setInterval(_updateWavelogStatus, 6000);
    _updateWavelogStatus();

    window.wavelogTestConn = async function() {
        var btn = document.getElementById('wavelog-test-btn');
        var orig = btn ? btn.textContent : '';
        if (btn) { btn.textContent = '⏳ Test...'; btn.disabled = true; }
        var url = (document.getElementById('wavelog_url')||{}).value || '';
        var key = (document.getElementById('wavelog_api_key')||{}).value || '';
        if (!url || !key) {
            if (btn) { btn.textContent = '⚠️ URL et clé requises';
                setTimeout(function(){ btn.textContent = orig; btn.disabled = false; }, 3000); }
            return;
        }
        try {
            await fetch('/update_config', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({wavelog:{url:url.trim(),api_key:key.trim(),
                    enabled:false,station_id:1,sync_interval:5,sync_rx:true,
                    sync_tx:true,only_qso:true,last_sync_id:0}})
            });
        } catch(_){}
        try {
            var r = await fetch('/wavelog/test', {method:'POST'});
            var d = await r.json();
            if (d.ok) {
                var sn = (d.info && !Array.isArray(d.info)) ? (d.info.station_name||d.info.callsign||'') : '';
                if (btn) btn.textContent = '✅ ' + (sn || 'Connexion OK');
            } else {
                if (btn) btn.textContent = '❌ ' + (d.message||'Échec');
            }
        } catch(e) { if (btn) btn.textContent = '❌ Erreur réseau'; }
        setTimeout(function(){ if(btn){btn.textContent=orig;btn.disabled=false;} }, 4000);
    };

    window.wavelogSyncNow = async function() {
        var btn = document.getElementById('wavelog-sync-btn');
        var orig = btn ? btn.textContent : '';
        if (btn) { btn.textContent = '⏳ Synchronisation...'; btn.disabled = true; }
        try {
            var r = await fetch('/wavelog/sync_now', {method:'POST'});
            var d = await r.json();
            if (btn) btn.textContent = d.ok
                ? '✅ ' + (d.pushed||0) + ' QSO envoyé(s)'
                : '❌ ' + (d.error||'Échec');
            _updateWavelogStatus();
        } catch(e) { if (btn) btn.textContent = '❌ Erreur réseau'; }
        setTimeout(function(){ if(btn){btn.textContent=orig;btn.disabled=false;} }, 4000);
    };

    window.wavelogResetSync = async function() {
        if (!confirm('Réinitialiser le curseur de synchro ?\nTous les QSO du carnet seront re-synchronisés lors de la prochaine synchro.')) return;
        try {
            await fetch('/wavelog/reset_sync', {method:'POST'});
            _updateWavelogStatus();
        } catch(e) { alert('Erreur : ' + e); }
    };
})();
</script>

</body>
</html>
"""

# ── Routes Flask ─────────────────────────────────────────────────────────────

@app.route('/send_beacon', methods=['POST'])
def send_beacon():
    _do_send_beacon()
    return jsonify({"status": "queued"})

@app.route('/send_weather', methods=['POST'])
def send_weather():
    """Récupère la météo Open-Meteo et émet un beacon APRS météo (@…_)."""
    result = _do_send_weather()
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)

@app.route('/send_propagation', methods=['POST'])
def send_propagation():
    """Récupère les indices NOAA et émet un beacon APRS propagation (>SFI:… A:… K:…)."""
    result = _do_send_propagation()
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)

@app.route('/send_status', methods=['POST'])
def send_status():
    data = request.json
    text = data.get('text', config_manager.data.get('station_status', ''))
    payload = ">" + text
    try:
        tx_queue.put_nowait({"dest": "APRS", "payload": payload, "path": None,
                             "aprs_type": "Statut", "extra": {"comment": text}})
        return jsonify({"status": "queued"})
    except Exception:
        return jsonify({"status": "busy"}), 429

@app.route('/send_raw', methods=['POST'])
def send_raw():
    data         = request.json or {}
    msg          = data.get('message', '').strip()
    dest_station = (data.get('dest_station', 'APRS') or 'APRS').strip().upper()
    is_iss       = data.get('is_iss', False)

    if not msg:
        return jsonify({"status": "error", "error": "Message vide"}), 400

    if is_iss:
        # Beacon ISS : message texte vers CQ via ARISS
        payload   = ":CQ       :" + msg
        dest_addr = "CQ"
        path      = "ARISS"
        aprs_type = "Beacon ISS"
    elif dest_station and dest_station != "APRS":
        # Message APRS vers une station
        formatted_dest = dest_station.ljust(9)
        payload   = ":" + formatted_dest + ":" + msg
        dest_addr = "APRS"
        path      = None
        aprs_type = "Message"
    else:
        # Texte libre (non standard, mais utilisé pour tests)
        payload   = ":" + "BLN0     " + ":" + msg
        dest_addr = "APRS"
        path      = None
        aprs_type = "Bulletin"

    print("[TX] send_raw -> dest=%s payload=%s" % (dest_addr, payload[:60]))
    try:
        tx_queue.put_nowait({"dest": dest_addr, "payload": payload, "path": path,
                             "aprs_type": aprs_type, "extra": {"comment": msg}})
        return jsonify({"status": "queued"})
    except queue.Full:
        return jsonify({"status": "busy", "error": "File TX pleine"}), 429

@app.route('/rx_test')
def rx_test():
    """
    Diagnostic complet du pipeline Dire Wolf.
    Retourne :
      - kiss_ok       : le port KISS TCP répond
      - frames_decoded: trames AX.25 reçues depuis le démarrage
      - dw_log        : dernières lignes de log Dire Wolf (stdout+stderr)
      - alsa_device   : device ALSA effectivement passé à Dire Wolf
    """
    import socket as _socket
    kiss_ok    = False
    kiss_error = ""
    try:
        s = _socket.create_connection(
            (APRSModem.DIREWOLF_HOST, APRSModem.DIREWOLF_KISS_PORT), timeout=1
        )
        s.close()
        kiss_ok = True
    except OSError as e:
        kiss_error = "Port KISS injoignable : " + str(e)

    # Dernières lignes de log Dire Wolf (20 max pour le JSON)
    dw_log = list(APRSModem.dw_log_lines)[-20:]

    return jsonify({
        "rx_thread":      APRSModem.rx_thread_alive,
        "bits_received":  APRSModem.rx_bit_count,
        "frames_decoded": APRSModem.dw_frames_decoded,
        "signal_level":   round(min(APRSModem.rx_energy_ema / 15.0, 1.0), 3),
        "audio_device_ok": kiss_ok,
        "audio_error":    kiss_error,
        "decoder":        "direwolf",
        "kiss_port":      APRSModem.DIREWOLF_KISS_PORT,
        "dw_log":         dw_log,
        "tx_last_ok":     APRSModem.tx_last_ok,
        "tx_last_error":  APRSModem.tx_last_error,
    })

@app.route('/rx_diag')
def rx_diag():
    """Page de diagnostic lisible : affiche les logs Dire Wolf en clair."""
    logs = list(APRSModem.dw_log_lines)
    html = "<html><body style='background:#111;color:#aef;font-family:monospace;padding:1em'>"
    html += "<h2 style='color:#6f9'>Dire Wolf diagnostic</h2>"
    html += "<b>Trames KISS recues : %d</b><br>" % APRSModem.rx_bit_count
    html += "<b>Trames AX.25 decodees : %d</b><br><br>" % APRSModem.dw_frames_decoded
    html += "<pre>" + "\n".join(logs[-60:]) + "</pre>"
    html += "</body></html>"
    return html


@app.route('/tx_diag')
def tx_diag():
    """
    Diagnostic TX complet accessible sur http://localhost:5001/tx_diag
    Teste dans l'ordre : config, port série, PTT (flash 500ms), audio (bip 200ms).
    NE PAS appeler en opération normale — déclenche PTT et audio réels.
    """
    import traceback as _tb
    steps = []

    def ok(msg):  steps.append({"st": "OK",  "msg": msg})
    def err(msg): steps.append({"st": "ERR", "msg": msg})
    def inf(msg): steps.append({"st": "INF", "msg": msg})

    # ── 1. Config ─────────────────────────────────────────────────────────────
    cfg = config_manager.data
    inf("Callsign   : %s" % cfg.get('callsign','?'))
    inf("Port serie : %s" % cfg.get('serial_port','(non configure)'))
    inf("PTT mode   : %s" % cfg.get('ptt_mode','RTS'))
    inf("PTT delay  : %d ms" % cfg.get('ptt_delay_ms', 250))
    inf("TX delay   : %d ms" % cfg.get('tx_delay_ms', 300))
    inf("Audio TX   : %s" % str(cfg.get('audio_device_tx','(defaut)')))
    inf("Volume     : %s" % cfg.get('volume', 0.5))
    inf("Path       : %s" % cfg.get('path','WIDE1-1,WIDE2-1'))

    # ── 2. Modem initialisé ? ─────────────────────────────────────────────────
    global modem
    if modem is None:
        err("modem = None — non initialise !")
    else:
        ok("Modem initialise")

    # ── 3. Port série ─────────────────────────────────────────────────────────
    port = cfg.get('serial_port','').strip()
    if not port:
        err("Port serie non configure dans les parametres")
    elif modem and modem.ser is None:
        err("Port serie configure (%s) mais non ouvert — verifier /dev/ttyUSBx et permissions" % port)
        # Tentative de réouverture à chaud
        try:
            import serial as _serial
            s = _serial.Serial(port, baudrate=9600, timeout=0.1, rtscts=False, dsrdtr=False)
            s.close()
            ok("Port serie ouvrable manuellement (sera reouvert au prochain reload config)")
        except Exception as e:
            err("Impossible d'ouvrir %s : %s" % (port, e))
    elif modem and modem.ser:
        ok("Port serie ouvert : %s" % port)
        # ── 4. Flash PTT 500 ms ───────────────────────────────────────────────
        mode = cfg.get('ptt_mode','RTS').upper()
        try:
            if 'RTS' in mode: modem.ser.setRTS(True)
            if 'DTR' in mode: modem.ser.setDTR(True)
            ok("PTT ON (%s) — flash 500 ms" % mode)
            time.sleep(0.5)
            if 'RTS' in mode: modem.ser.setRTS(False)
            if 'DTR' in mode: modem.ser.setDTR(False)
            ok("PTT OFF")
        except Exception as e:
            err("Erreur PTT : %s" % e)

    # ── 5. Test TX KISS : envoi d'une trame de test à Dire Wolf ─────────────
    inf("Audio TX gere par Dire Wolf (ADEVICE dans direwolf.conf)")
    try:
        import socket as _ds
        s = _ds.create_connection(
            (APRSModem.DIREWOLF_HOST, APRSModem.DIREWOLF_KISS_PORT), timeout=2
        )
        # Trame APRS de test : beacon vers APRS
        test_call = cfg.get("callsign", "N0CALL")
        test_payload = ">DIAG TEST de %s" % test_call
        # Encode AX.25 minimal
        def _ec(call, last=False):
            p = call.upper().split("-")
            b = p[0].ljust(6)
            ssid = int(p[1]) if len(p)>1 else 0
            r = [(ord(c)<<1) for c in b]
            r.append((ssid<<1)|(0x61 if last else 0x60))
            return r
        f  = _ec("APRS")
        f += _ec(test_call, last=True)
        f += [0x03, 0xF0] + [ord(c) for c in test_payload]
        ax25 = bytes(f)
        FEND=0xC0; FESC=0xDB; TFEND=0xDC; TFESC=0xDD
        def _ke(d):
            o=bytearray()
            for b in d:
                if b==FEND: o+=bytes([FESC,TFEND])
                elif b==FESC: o+=bytes([FESC,TFESC])
                else: o.append(b)
            return bytes(o)
        kf = bytes([FEND,0x00]) + _ke(ax25) + bytes([FEND])
        s.sendall(kf)
        s.close()
        ok("Trame KISS envoyee a Dire Wolf (%d octets) — verifier emission radio" % len(kf))
    except Exception as e:
        err("Erreur envoi KISS TX test : %s" % e)

    # ── 6. Résumé ─────────────────────────────────────────────────────────────
    has_err = any(s["st"] == "ERR" for s in steps)
    html  = "<html><head><meta charset='utf-8'></head>"
    html += "<body style='background:#111;color:#ccc;font-family:monospace;padding:1.5em;max-width:700px'>"
    html += "<h2 style='color:%s'>TX Diagnostic — %s</h2>" % (
        "#f66" if has_err else "#6f9",
        "PROBLEMES DETECTES" if has_err else "Tout semble OK"
    )
    html += "<table style='width:100%;border-collapse:collapse'>"
    for s in steps:
        col = {"OK": "#6f9", "ERR": "#f66", "INF": "#8af"}[s["st"]]
        html += "<tr><td style='color:%s;width:3em;padding:3px 8px'>%s</td>" \
                "<td style='padding:3px 4px'>%s</td></tr>" % (col, s["st"], s["msg"])
    html += "</table>"
    html += "<br><p style='color:#888;font-size:11px'>Rafraichir pour relancer le test PTT/audio.</p>"
    html += "<p><a href='/rx_diag' style='color:#8af'>→ Logs Dire Wolf (RX)</a></p>"
    html += "</body></html>"
    return html

_reload_lock  = threading.Lock()
_reload_status = {"state": "idle", "error": ""}

@app.route('/update_config', methods=['POST'])
def update_config():
    data = request.json
    def _reload():
        global modem
        with _reload_lock:
            _reload_status["state"] = "loading"
            _reload_status["error"] = ""
            try:
                config_manager.save(data)
                old = modem
                old.is_rx_running = False
                try:
                    if old.ser: old.ser.close()
                except Exception:
                    pass
                time.sleep(0.3)
                modem = APRSModem(config_manager.data)
                modem.start_rx()
                _reload_status["state"] = "ok"
            except Exception as e:
                _reload_status["state"] = "error"
                _reload_status["error"] = str(e)
    threading.Thread(target=_reload, daemon=True, name="modem-reload").start()
    return jsonify({"status": "reloading"})

@app.route('/config_status')
def config_status():
    return jsonify(_reload_status)

@app.route('/igate_status')
def igate_status():
    return jsonify({
        "enabled":      config_manager.data.get("igate_enabled", False),
        "connected":    igate_client.connected,
        "status":       igate_client.status,
        "frames_gated": igate_client.frames_rx_gated,
        "frames_is_rx": igate_client.frames_is_rx,
    })

@app.route('/rx_stream')
def rx_stream():
    def generate():
        local_q = queue.Queue()
        listeners.append(local_q)
        try:
            yield "data: {\"type\":\"connected\"}\n\n"
            while True:
                try:
                    frame = local_q.get(timeout=20)
                    yield "data: %s\n\n" % json.dumps(frame, ensure_ascii=False)
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            listeners.remove(local_q)
    return Response(generate(), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── Routes Chat ───────────────────────────────────────────────────────────────

@app.route('/chat/contacts')
def chat_contacts():
    return jsonify(chat_manager.get_contacts())

@app.route('/chat/history/<callsign>')
def chat_history(callsign):
    chat_manager.mark_read(callsign)
    return jsonify(chat_manager.get_history(callsign))

@app.route('/chat/send', methods=['POST'])
def chat_send():
    data  = request.json
    dest  = data.get('dest', '').upper().strip()
    text  = data.get('text', '').strip()[:67]
    if not dest or not text:
        return jsonify({"error": "dest/text requis"}), 400
    msgno   = chat_manager._next_msgno()
    payload = ":" + dest.ljust(9) + ":" + text + "{" + msgno + "}"
    chat_manager.add_outgoing(dest, text, msgno)
    try:
        tx_queue.put_nowait({
            "dest": "APRS", "payload": payload, "path": None,
            "aprs_type": "Message", "extra": {"comment": text, "msg_dest": dest}
        })
        return jsonify({"status": "queued", "msgno": msgno})
    except queue.Full:
        return jsonify({"status": "busy"}), 429

# ── Broadcaster SSE ───────────────────────────────────────────────────────────

listeners = []

stations_positions      = {}
stations_positions_lock = threading.Lock()



# ══════════════════════════════════════════════════════════════════════════════
# iGate APRS-IS
# ══════════════════════════════════════════════════════════════════════════════

class APRSISClient:
    """Connexion TCP à un serveur APRS-IS (rotate.aprs2.net:14580).

    Modes :
      RX-iGate  — reçoit les paquets RF et les transfère vers APRS-IS.
                  Symbole APRS : /R (antenne + iGate RX)
      Full-iGate — en plus, récupère les paquets IS destinés à des stations
                  locales et les retransmet en RF.
                  Symbole APRS : /& (iGate TX/RX)

    Protocole de login APRS-IS :
      user CALLSIGN pass PASSCODE vers SOFTWARE vers VERSION filter FILTER
    """

    RECONNECT_DELAY = 30   # secondes entre tentatives de reconnexion
    HEARTBEAT_INTERVAL = 60  # secondes entre envois de keepalive

    def __init__(self):
        self._sock      = None
        self._fobj      = None
        self._lock      = threading.Lock()
        self._stop      = threading.Event()
        self.connected  = False
        self.status     = "Déconnecté"
        self.frames_rx_gated = 0   # trames RF → IS transmises
        self.frames_is_rx    = 0   # trames IS reçues
        self._thread_rx  = None
        self._thread_hb  = None

    # ── Calcul du passcode APRS-IS ─────────────────────────────────────────
    @staticmethod
    def compute_passcode(callsign):
        """Génère le passcode numérique depuis le callsign (base sans SSID)."""
        call = callsign.upper().split('-')[0]
        code = 0x73e2
        for i, c in enumerate(call):
            if i % 2 == 0:
                code ^= ord(c) << 8
            else:
                code ^= ord(c)
        return code & 0x7fff

    # ── Connexion ──────────────────────────────────────────────────────────
    def connect(self):
        cfg      = config_manager.data
        server   = cfg.get("igate_server",   "rotate.aprs2.net")
        port     = int(cfg.get("igate_port", 14580))
        callsign = cfg.get("callsign", "N0CALL").upper()
        passcode = cfg.get("igate_passcode", "-1")
        filt     = cfg.get("igate_filter",   "")

        # Passcode auto si -1 ou vide
        if str(passcode).strip() in ("-1", ""):
            passcode = self.compute_passcode(callsign)

        try:
            self._sock = socket.create_connection((server, port), timeout=20)
            self._fobj = self._sock.makefile('r', encoding='utf-8', errors='replace')
            # Lire la bannière du serveur
            banner = self._fobj.readline()
            print("[IGATE] Connecté à %s:%d — %s" % (server, port, banner.strip()))

            # Login
            login_line = "user %s pass %s vers Py-APRS 2.0" % (callsign, passcode)
            if filt:
                login_line += " filter %s" % filt
            self._send_raw(login_line)

            # Lire la réponse au login
            resp = self._fobj.readline()
            print("[IGATE] Login réponse : %s" % resp.strip())
            if "unverified" in resp.lower():
                self.status = "⚠️ Non vérifié (passcode incorrect ?)"
                # on reste connecté en lecture seule
            elif "verified" in resp.lower() or "logresp" in resp.lower():
                self.status = "✅ Connecté (vérifié)"
            else:
                self.status = "✅ Connecté"

            self.connected = True
            return True

        except Exception as e:
            self.status = "❌ Erreur : %s" % e
            self.connected = False
            print("[IGATE] Erreur connexion : %s" % e)
            return False

    # ── Envoi d'une ligne brute ────────────────────────────────────────────
    def _send_raw(self, line):
        try:
            with self._lock:
                self._sock.sendall((line + "\r\n").encode('utf-8', errors='replace'))
        except Exception as e:
            print("[IGATE] Erreur envoi : %s" % e)
            self.connected = False

    # ── Gate RF → IS ───────────────────────────────────────────────────────
    def gate_rf_to_is(self, frame):
        """Transmet une trame reçue en RF vers APRS-IS.

        Format TNC2 : CALLSIGN>DEST,PATH:payload
        Préfixe qAR (received from RF) selon la norme APRS-IS Q-codes.
        """
        if not self.connected:
            return False
        src     = frame.get("src", "")
        dest    = frame.get("dest", "APRS")
        path    = frame.get("path", "")
        payload = frame.get("payload", "")
        my_call = config_manager.data.get("callsign", "N0CALL").upper()

        if not src or not payload:
            return False

        # Construire le chemin avec qAR (arrivée depuis RF)
        # qAR = packet received from RF and gated to IS
        path_parts = [p for p in path.split(',') if p] if path else []
        path_parts.append("qAR")
        path_parts.append(my_call)
        tnc2 = "%s>%s,%s:%s" % (src, dest, ",".join(path_parts), payload)

        self._send_raw(tnc2)
        self.frames_rx_gated += 1
        return True

    # ── Thread lecture IS ──────────────────────────────────────────────────
    def _reader_loop(self):
        """Lit les paquets entrants depuis APRS-IS et les injecte dans rx_queue."""
        while not self._stop.is_set() and self.connected:
            try:
                line = self._fobj.readline()
                if not line:
                    print("[IGATE] Connexion fermée par le serveur")
                    self.connected = False
                    break
                line = line.strip()
                if not line or line.startswith('#'):
                    continue   # commentaire / heartbeat serveur

                self.frames_is_rx += 1
                # Parser la trame TNC2 reçue depuis IS
                frame = _parse_tnc2(line)
                if frame:
                    frame["_source"] = "IS"   # marqueur : provient d'APRS-IS
                    APRSModem.rx_queue.put_nowait(frame)
            except Exception as e:
                if not self._stop.is_set():
                    print("[IGATE] Erreur lecture : %s" % e)
                self.connected = False
                break

    # ── Thread heartbeat ──────────────────────────────────────────────────
    def _heartbeat_loop(self):
        while not self._stop.is_set() and self.connected:
            time.sleep(self.HEARTBEAT_INTERVAL)
            if self.connected:
                self._send_raw("#")   # keepalive APRS-IS standard

    # ── Démarrage ─────────────────────────────────────────────────────────
    def start(self):
        self._stop.clear()
        t = threading.Thread(target=self._run_loop, daemon=True, name="igate-manager")
        t.start()

    def _run_loop(self):
        """Boucle de gestion : connexion + reconnexion automatique."""
        while not self._stop.is_set():
            cfg = config_manager.data
            if not cfg.get("igate_enabled", False):
                time.sleep(5)
                continue

            if self.connect():
                self._thread_rx = threading.Thread(
                    target=self._reader_loop, daemon=True, name="igate-reader")
                self._thread_hb = threading.Thread(
                    target=self._heartbeat_loop, daemon=True, name="igate-hb")
                self._thread_rx.start()
                self._thread_hb.start()
                self._thread_rx.join()   # attend la déconnexion
            else:
                self.status = "🔄 Reconnexion dans %ds" % self.RECONNECT_DELAY

            if not self._stop.is_set():
                print("[IGATE] Reconnexion dans %ds…" % self.RECONNECT_DELAY)
                self.status = "🔄 Reconnexion dans %ds" % self.RECONNECT_DELAY
                time.sleep(self.RECONNECT_DELAY)

    def stop(self):
        self._stop.set()
        self.connected = False
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass


def _parse_tnc2(line):
    """Parse une ligne TNC2 (APRS-IS) en dict frame compatible rx_queue."""
    try:
        # CALLSIGN>DEST,PATH:payload
        if ':' not in line:
            return None
        header, payload = line.split(':', 1)
        if '>' not in header:
            return None
        src, rest = header.split('>', 1)
        parts = rest.split(',')
        dest  = parts[0]
        path  = ','.join(parts[1:]) if len(parts) > 1 else ""
        aprs_type, extra = APRSModem._decode_aprs_payload(payload, dest)
        return {
            "src": src.strip(), "dest": dest.strip(), "path": path.strip(),
            "aprs_type": aprs_type, "payload": payload, "extra": extra,
        }
    except Exception:
        return None


# Instance globale iGate
igate_client = APRSISClient()
import socket   # utilisé par APRSISClient


# Historique circulaire des trames reçues (hors rx_level), survivant au F5
RX_HISTORY_MAX  = 500
rx_history      = collections.deque(maxlen=RX_HISTORY_MAX)
rx_history_lock = threading.Lock()

_rx_activity_ts = [0.0]   # timestamp dernière trame RX reçue
_rx_level_last  = [0.0]   # timestamp dernier envoi rx_level

# ── Métadonnées télémétrie par station ────────────────────────────────────────
# Clé : callsign source (ex: "F4ACU-3")
# Valeur : dict avec clés optionnelles :
#   parm  : list[str]  — noms des 5 canaux analogiques + 8 bits numériques
#   unit  : list[str]  — unités des 5 analogiques + labels 8 bits
#   eqns  : list[tuple(a,b,c)] — coefficients ax²+bx+c pour chaque analogique
#   bits  : list[str]  — noms des 8 bits numériques (alias de parm[5:])
_telem_meta      = {}
_telem_meta_lock = threading.Lock()


def _parse_telem_parm(body):
    """Parse PARM. ou UNIT. → liste de jusqu'à 13 champs (5 analogiques + 8 bits)."""
    raw = body.split(',')
    return [f.strip() for f in raw[:13]]


def _parse_telem_eqns(body):
    """Parse EQNS. → 5 triplets (a, b, c) pour la formule ax²+bx+c."""
    vals = [v.strip() for v in body.split(',')]
    eqns = []
    for i in range(5):
        try:
            a = float(vals[i*3])
            b = float(vals[i*3+1])
            c = float(vals[i*3+2])
        except (IndexError, ValueError):
            a, b, c = 0.0, 1.0, 0.0  # identité par défaut
        eqns.append((a, b, c))
    return eqns


def _apply_eqns(raw_val, eqn):
    """Applique ax²+bx+c à la valeur brute."""
    a, b, c = eqn
    x = float(raw_val)
    return round(a * x * x + b * x + c, 4)


def _rx_broadcaster():
    def my_calls():
        """Retourne la liste des variantes acceptables : F1RIQ, F1RIQ-9, etc."""
        full = config_manager.data.get("callsign", "").upper().strip()
        base = full.split("-")[0]
        return {full, base}

    while True:
        try:
            frame = APRSModem.rx_queue.get(timeout=1)

            # ── Capture métadonnées télémétrie (messages PARM/UNIT/EQNS/BITS) ─
            if frame.get("aprs_type") == "Message" and frame.get("type") != "rx_level":
                _extra = frame.get("extra", {})
                _src   = frame.get("src", "").upper().strip()
                _body  = _extra.get("msg_text", "")
                if _body.startswith("PARM."):
                    with _telem_meta_lock:
                        _telem_meta.setdefault(_src, {})["parm"] = _parse_telem_parm(_body[5:])
                    print("[TELEM] PARM recu de %s" % _src)
                elif _body.startswith("UNIT."):
                    with _telem_meta_lock:
                        _telem_meta.setdefault(_src, {})["unit"] = _parse_telem_parm(_body[5:])
                    print("[TELEM] UNIT recu de %s" % _src)
                elif _body.startswith("EQNS."):
                    with _telem_meta_lock:
                        _telem_meta.setdefault(_src, {})["eqns"] = _parse_telem_eqns(_body[5:])
                    print("[TELEM] EQNS recu de %s" % _src)
                elif _body.startswith("BITS."):
                    parts_b = _body[5:].split(',', 9)
                    with _telem_meta_lock:
                        meta = _telem_meta.setdefault(_src, {})
                        meta["bits_mask"]  = parts_b[0].strip() if parts_b else ""
                        meta["bits_label"] = parts_b[1].strip() if len(parts_b) > 1 else ""
                        meta["bits_names"] = [p.strip() for p in parts_b[2:10]]
                    print("[TELEM] BITS recu de %s" % _src)

            # ── Enrichissement des trames Télémétrie avec PARM/UNIT/EQNS ────
            if frame.get("aprs_type") == "Telemetrie":
                src  = frame.get("src", "").upper().strip()
                with _telem_meta_lock:
                    meta = _telem_meta.get(src, {})
                extra = frame.get("extra", {})
                parm  = meta.get("parm", [])
                unit  = meta.get("unit", [])
                eqns  = meta.get("eqns", [])
                bits_names = meta.get("bits_names", [])
                raw_vals   = extra.get("telem_analog_raw", extra.get("telem_analog", []))

                # Calcul des valeurs mises à l'échelle via EQNS
                scaled = []
                for i, rv in enumerate(raw_vals):
                    if rv is None:
                        scaled.append(None)
                    elif i < len(eqns):
                        scaled.append(_apply_eqns(rv, eqns[i]))
                    else:
                        scaled.append(float(rv))
                extra["telem_analog"] = scaled

                # Noms et unités (5 analogiques)
                extra["telem_names"] = parm[:5]   if parm else []
                extra["telem_units"] = unit[:5]   if unit else []

                # Bits numériques nommés
                bits_str = extra.get("telem_bits", "")
                named_bits = []
                for i, bit_char in enumerate(bits_str[:8]):
                    name = bits_names[i] if i < len(bits_names) else ("B%d" % (i+1))
                    named_bits.append({"name": name, "val": bit_char == "1"})
                extra["telem_named_bits"] = named_bits

                frame["extra"] = extra

            if frame.get("aprs_type") == "Message" and frame.get("type") != "rx_level":
                extra = frame.get("extra", {})
                dest  = extra.get("msg_dest", "").upper().strip()
                src   = frame.get("src", "").upper().strip()
                text  = extra.get("msg_text", "")
                msgno = extra.get("msg_msgno")
                ackto = extra.get("msg_ackno")

                if extra.get("msg_ack") and ackto:
                    chat_manager.mark_ack(ackto)
                    print("[MSG] ACK recu pour msgno=%s de %s" % (ackto, src))
                elif dest and dest in my_calls():
                    chat_manager.add_incoming(src, text, msgno)
                    frame["_chat"] = True
                    print("[MSG] Message recu de %s : %s (msgno=%s)" % (src, text[:40], msgno))
                    if msgno:
                        ack_payload = ":" + src.ljust(9) + ":ack" + msgno
                        try:
                            tx_queue.put_nowait({
                                "dest": "APRS", "payload": ack_payload, "path": None,
                                "aprs_type": "ACK", "extra": {"comment": "ack" + msgno}
                            })
                            print("[MSG] ACK envoye : ack%s -> %s" % (msgno, src))
                        except Exception as e:
                            print("[MSG] Erreur envoi ACK : %s" % e)

            # ── iGate : forward RF → APRS-IS ─────────────────────────────
            if (config_manager.data.get("igate_enabled") and
                    igate_client.connected and
                    frame.get("type") != "tx_event" and
                    frame.get("type") != "rx_level" and
                    frame.get("_source") != "IS" and          # pas de boucle
                    frame.get("aprs_type") not in ("Telemetrie",) and
                    frame.get("src")):
                igate_client.gate_rf_to_is(frame)

            # Assigner _fid AVANT de broadcaster (SSE + historique ont le même id)
            if frame.get("type") != "rx_level" and "_fid" not in frame:
                frame["_fid"] = next(_fid_counter)

            # Historique (hors rx_level)
            if frame.get("type") != "rx_level":
                with rx_history_lock:
                    rx_history.append(frame)
                # Marquer l'activité pour la jauge
                if frame.get("type") != "tx_event":
                    _rx_activity_ts[0] = time.time()
                # ── Enregistrement carnet de trafic ──────────────────────────
                _direction = "TX" if frame.get("type") == "tx_event" else "RX"
                if frame.get("aprs_type") not in (None, "ACK", ""):
                    try:
                        logbook.add(frame, direction=_direction)
                    except Exception as _le:
                        pass

            # ── Mise à jour positions stations ──────────────────────────
            _e = frame.get("extra", {})
            if _e.get("lat") is not None and frame.get("type") not in ("tx_event", "rx_level"):
                _cs = frame.get("src", "").upper().strip().split(",")[0]
                with stations_positions_lock:
                    stations_positions[_cs] = {"lat": _e["lat"], "lon": _e["lon"], "ts": time.time()}

            # Diffuser à tous les clients SSE
            for q in list(listeners):
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    pass

        except queue.Empty:
            pass

        # Jauge rx_level périodique (toutes les 500 ms) basée sur activité KISS
        now = time.time()
        if now - _rx_level_last[0] >= 0.5:
            _rx_level_last[0] = now
            elapsed = now - _rx_activity_ts[0]
            level = round(max(0.0, 1.0 - elapsed / 3.0), 3) if elapsed < 3.0 else 0.0
            lf = {"type": "rx_level", "level": level}
            for q in list(listeners):
                try: q.put_nowait(lf)
                except queue.Full: pass

threading.Thread(target=_rx_broadcaster, daemon=True).start()
igate_client.start()   # démarre le manager iGate (se connecte si igate_enabled)


@app.route('/rx_history')
def rx_history_route():
    """Retourne les dernières trames reçues (max RX_HISTORY_MAX).
    Le frontend l'appelle au chargement pour restaurer l'affichage après un F5."""
    with rx_history_lock:
        snapshot = list(rx_history)
    return jsonify(snapshot)

# ── Worker TX ─────────────────────────────────────────────────────────────────

tx_queue = queue.Queue(maxsize=10)

import itertools as _itertools
_fid_counter = _itertools.count(1)

def _broadcast_tx(job):
    cfg = config_manager.data
    APRSModem.rx_queue.put({
        "type":      "tx_event",
        "src":       cfg.get("callsign", "?"),
        "dest":      job.get("dest", "APRS"),
        "path":      job.get("path") or cfg.get("path", ""),
        "aprs_type": job.get("aprs_type", "TX"),
        "payload":   job.get("payload", ""),
        "extra":     job.get("extra", {}),
        "_fid":      next(_fid_counter),
    })

def _tx_worker():
    global modem
    while True:
        job = tx_queue.get()
        try:
            _broadcast_tx(job)
            if modem is None:
                raise RuntimeError("Modem non initialise")
            modem.send_packet(job["dest"], job["payload"], job.get("path"))
        except Exception as e:
            APRSModem.tx_last_error = str(e)
            print("[TX WORKER] ERREUR : %s" % e)
            import traceback; traceback.print_exc()
        finally:
            tx_queue.task_done()

threading.Thread(target=_tx_worker, daemon=True, name="tx-worker").start()

# ── Beacon automatique ────────────────────────────────────────────────────────

_beacon_state = {
    "next_at":  None,
    "interval": config_manager.data.get("beacon_interval", 0),
    "type":     config_manager.data.get("beacon_type", "station"),
}

# ── Scheduler multi-balises ───────────────────────────────────────────────────
# Un thread par type de balise, chacun avec son propre timer indépendant.
# _beacon_workers[btype] = {"thread": Thread, "next_at": float|None, "interval": int}

_beacon_workers = {}
_beacon_workers_lock = threading.Lock()

def _make_beacon_worker(btype):
    """Crée et démarre un thread de balise pour le type donné."""
    def _worker():
        # Résolution des fonctions à l'exécution (après définition complète du module)
        _dispatch = {
            "station":     _do_send_beacon,
            "iss":         _do_send_iss_beacon,
            "meteo":       _do_send_weather,
            "propagation": _do_send_propagation,
        }
        print("[BCN] Thread démarré pour type=%s" % btype)
        while True:
            schedules = config_manager.data.get('beacon_schedules', {})
            interval  = schedules.get(btype, 0)
            if interval and interval > 0:
                # Émettre
                print("[BCN] Émission type=%s interval=%d min" % (btype, interval))
                fn = _dispatch.get(btype)
                if fn:
                    try:
                        fn()
                    except Exception as e:
                        print("[BCN] Erreur type=%s : %s" % (btype, e))
                next_at = time.time() + interval * 60
                with _beacon_workers_lock:
                    if btype in _beacon_workers:
                        _beacon_workers[btype]["next_at"]  = next_at
                        _beacon_workers[btype]["interval"] = interval
                # Attendre par tranches de 5 s — réagit aux changements de config
                while time.time() < next_at:
                    time.sleep(5)
                    new_interval = config_manager.data.get('beacon_schedules', {}).get(btype, 0)
                    if new_interval != interval:
                        print("[BCN] Config changée type=%s (%d→%d) — redémarrage" % (btype, interval, new_interval))
                        break
                with _beacon_workers_lock:
                    if btype in _beacon_workers:
                        _beacon_workers[btype]["next_at"] = None
            else:
                # Type désactivé — polling toutes les 10 s jusqu'à réactivation
                with _beacon_workers_lock:
                    if btype in _beacon_workers:
                        _beacon_workers[btype]["next_at"]  = None
                        _beacon_workers[btype]["interval"] = 0
                time.sleep(10)

    t = threading.Thread(target=_worker, daemon=True, name="beacon-%s" % btype)
    t.start()
    return t

def _beacon_scheduler():
    """Superviseur : lance/relance les threads de balise selon la config."""
    _ALL_TYPES = ["station", "iss", "meteo", "propagation"]
    while True:
        with _beacon_workers_lock:
            for btype in _ALL_TYPES:
                worker = _beacon_workers.get(btype)
                if worker is None or not worker["thread"].is_alive():
                    t = _make_beacon_worker(btype)
                    _beacon_workers[btype] = {"thread": t, "next_at": None, "interval": 0}
        time.sleep(15)

def _do_send_beacon():
    cfg     = config_manager.data
    sym_t   = cfg.get('symbol_table', '/')
    sym_c   = cfg.get('symbol_code',  '[')
    comment = cfg.get('station_comment', '')
    grid    = cfg.get('maidenhead', '')

    # Convertir locator Maidenhead en lat/lon pour beacon position (format APRS !)
    def grid_to_latlon(grid):
        g = grid.upper()
        if len(g) < 4:
            return None, None
        try:
            lon = (ord(g[0]) - ord('A')) * 20 - 180
            lat = (ord(g[1]) - ord('A')) * 10 - 90
            lon += (int(g[2])) * 2
            lat += (int(g[3])) * 1
            if len(g) >= 6:
                lon += (ord(g[4]) - ord('A')) * (2/24.0)
                lat += (ord(g[5]) - ord('A')) * (1/24.0)
            lon += 1/24.0
            lat += 0.5/24.0
            return lat, lon
        except Exception:
            return None, None

    def deg_to_aprs(deg, is_lat):
        """Convertit degrés décimaux en format APRS DDDMM.MMH"""
        if is_lat:
            hemi = 'N' if deg >= 0 else 'S'
        else:
            hemi = 'E' if deg >= 0 else 'W'
        deg = abs(deg)
        d = int(deg)
        m = (deg - d) * 60
        if is_lat:
            return "%02d%05.2f%s" % (d, m, hemi)
        else:
            return "%03d%05.2f%s" % (d, m, hemi)

    lat, lon = grid_to_latlon(grid)

    if lat is not None:
        # Beacon avec position (type !)
        aprs_lat = deg_to_aprs(lat, True)
        aprs_lon = deg_to_aprs(lon, False)
        parts = []
        if grid:    parts.append("Grid:" + grid)
        if comment: parts.append(comment)
        info = " ".join(parts)
        payload = "!%s%s%s%s%s" % (aprs_lat, sym_t, aprs_lon, sym_c, info)
    else:
        # Beacon statut sans position si locator absent/invalide
        parts = []
        if grid:    parts.append("Grid:" + grid)
        if comment: parts.append(comment)
        info = " ".join(parts) if parts else "APRS"
        payload = ">" + info

    try:
        tx_queue.put_nowait({"dest": "APRS", "payload": payload, "path": None,
                             "aprs_type": "Beacon", "extra": {"comment": comment}})
        print("[TX] Beacon queued : %s" % payload[:60])
    except Exception as e:
        print("[TX] Beacon queue erreur : %s" % e)


def _grid_to_latlon(grid):
    """Conversion Maidenhead → (lat, lon) décimaux."""
    g = grid.upper()
    if len(g) < 4:
        return None, None
    try:
        lon = (ord(g[0]) - ord('A')) * 20 - 180
        lat = (ord(g[1]) - ord('A')) * 10 - 90
        lon += int(g[2]) * 2
        lat += int(g[3]) * 1
        if len(g) >= 6:
            lon += (ord(g[4]) - ord('A')) * (2 / 24.0)
            lat += (ord(g[5]) - ord('A')) * (1 / 24.0)
        lon += 1 / 24.0
        lat += 0.5 / 24.0
        return round(lat, 5), round(lon, 5)
    except Exception:
        return None, None


def _fetch_openmeteo(lat, lon):
    """
    Interroge l'API Open-Meteo (gratuite, sans clé) et retourne un dict météo.
    Champs retournés : temp_c, humidity_pct, wind_speed_ms, wind_dir_deg,
                       gust_ms, rain_mm, pressure_hpa, description.
    """
    import urllib.request, json as _json
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=%.5f&longitude=%.5f"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
        "precipitation,rain,wind_speed_10m,wind_direction_10m,wind_gusts_10m,"
        "surface_pressure,weather_code"
        "&wind_speed_unit=ms"
        "&timezone=auto"
    ) % (lat, lon)

    print("[WX] Requete Open-Meteo : lat=%.4f lon=%.4f" % (lat, lon))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "aprs_direwolf/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read())
        cur = data.get("current", {})

        # WMO weather code → description courte
        WMO = {
            0:"Ciel clair", 1:"Peu nuageux", 2:"Partiellement nuageux",
            3:"Couvert", 45:"Brouillard", 48:"Brouillard givrant",
            51:"Bruine legere", 53:"Bruine moderee", 55:"Bruine dense",
            61:"Pluie faible", 63:"Pluie moderee", 65:"Pluie forte",
            71:"Neige faible", 73:"Neige moderee", 75:"Neige forte",
            80:"Averses faibles", 81:"Averses moderees", 82:"Averses fortes",
            95:"Orage", 96:"Orage avec gresil", 99:"Orage violent",
        }
        code = cur.get("weather_code", 0)
        return {
            "temp_c":       cur.get("temperature_2m"),
            "humidity_pct": cur.get("relative_humidity_2m"),
            "wind_speed_ms":cur.get("wind_speed_10m"),
            "wind_dir_deg": cur.get("wind_direction_10m"),
            "gust_ms":      cur.get("wind_gusts_10m"),
            "rain_mm":      cur.get("rain") or cur.get("precipitation") or 0,
            "pressure_hpa": cur.get("surface_pressure"),
            "description":  WMO.get(code, "Inconnu"),
            "wmo_code":     code,
        }
    except Exception as e:
        print("[WX] Erreur Open-Meteo : %s" % e)
        return None


def _fetch_solar_indices():
    """
    Récupère les indices solaires/géomagnétiques depuis NOAA Space Weather.
    Retourne un dict : sfi, a_index, k_index, hf_cond, vhf_cond, aurora.

    Endpoints vérifiés mai 2026 :
      Kp + A : products/noaa-planetary-k-index.json
               Format : [{time_tag, Kp, a_running, station_count}, ...]
      SFI    : json/f107_cm_flux.json
               Format : [{time_tag, flux, ...}, ...]
    """
    import urllib.request, json as _json

    result = {}

    # ── Kp + A-index ─────────────────────────────────────────────────────────
    url_kp = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
    try:
        req = urllib.request.Request(url_kp, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = _json.loads(resp.read())
        # Tableau d'objets — dernier = période 3h la plus récente
        if isinstance(raw, list) and raw:
            last = raw[-1]
            if isinstance(last, dict):
                if last.get("Kp")        is not None: result["k_index"] = float(last["Kp"])
                if last.get("a_running") is not None: result["a_index"] = float(last["a_running"])
                print("[PROP] Kp=%.2f A=%.0f (%s)" % (
                    result.get("k_index", 0), result.get("a_index", 0),
                    last.get("time_tag", "?")))
    except Exception as e:
        print("[PROP] Erreur Kp NOAA : %s" % e)

    # ── SFI (F10.7 cm flux) ───────────────────────────────────────────────────
    url_sfi = "https://services.swpc.noaa.gov/json/f107_cm_flux.json"
    try:
        req = urllib.request.Request(url_sfi, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = _json.loads(resp.read())
        if isinstance(raw, list) and raw:
            # Parcourir depuis la fin pour la dernière valeur non nulle
            for entry in reversed(raw):
                v = entry.get("flux") if isinstance(entry, dict) else None
                if v is None and isinstance(entry, dict):
                    v = entry.get("f107")
                if v is not None:
                    result["sfi"] = float(v)
                    print("[PROP] SFI=%.0f (%s)" % (result["sfi"], entry.get("time_tag", "?")))
                    break
    except Exception as e:
        print("[PROP] Erreur SFI NOAA : %s" % e)

    sfi = result.get("sfi")
    k   = result.get("k_index")
    a   = result.get("a_index")

    def hf_conditions(sfi, a, k):
        if sfi is None:
            return "?"
        if k is not None and k >= 5:
            return "MAUVAIS"
        if a is not None and a >= 30:
            return "PERTURBE"
        if sfi >= 150: return "EXCEL"
        if sfi >= 120: return "BON"
        if sfi >= 90:  return "CORRECT"
        if sfi >= 70:  return "FAIBLE"
        return "MAUVAIS"

    def vhf_conditions(k):
        if k is None: return "?"
        if k >= 5:    return "AURORA"
        if k <= 2:    return "CALME"
        if k <= 3:    return "LEGER"
        return "ACTIVE"

    result["hf_cond"]  = hf_conditions(sfi, a, k)
    result["vhf_cond"] = vhf_conditions(k)
    result["aurora"]   = (k is not None and k >= 5)

    print("[PROP] SFI=%s A=%s K=%s HF=%s VHF=%s" % (
        ("%.0f" % sfi) if sfi else "?",
        ("%.0f" % a)   if a   else "?",
        ("%.1f" % k)   if k   else "?",
        result["hf_cond"], result["vhf_cond"]
    ))
    return result


def _do_send_propagation():
    """
    Construit et émet un beacon APRS de type '>' (Status) avec les indices
    de propagation solaire/géomagnétique récupérés depuis NOAA Space Weather.
    Format : >SFI:NNN A:NN K:N HF:XXXX VHF:YYYY {NOAA}
    """
    data = _fetch_solar_indices()

    sfi = data.get("sfi")
    a   = data.get("a_index")
    k   = data.get("k_index")
    hf  = data.get("hf_cond", "?")
    vhf = data.get("vhf_cond", "?")

    sfi_str = ("%.0f" % sfi) if sfi is not None else "?"
    a_str   = ("%.0f" % a)   if a   is not None else "?"
    k_str   = ("%.1f" % k)   if k   is not None else "?"

    payload = ">SFI:%s A:%s K:%s HF:%s VHF:%s {NOAA}" % (
        sfi_str, a_str, k_str, hf, vhf
    )

    try:
        tx_queue.put_nowait({
            "dest":      "APRS",
            "payload":   payload,
            "path":      None,
            "aprs_type": "Propagation",
            "extra": {
                "sfi":      sfi,
                "a_index":  a,
                "k_index":  k,
                "hf_cond":  hf,
                "vhf_cond": vhf,
                "aurora":   data.get("aurora", False),
                "comment":  payload[1:],
            }
        })
        print("[PROP] Beacon propagation queued : %s" % payload)
        return {"status": "queued", "payload": payload, "data": data}
    except queue.Full:
        return {"error": "File TX pleine"}


def _do_send_iss_beacon():
    """Envoie un beacon APRS vers CQ via ARISS (contact ISS)."""
    cfg     = config_manager.data
    comment = cfg.get('station_comment', '')
    grid    = cfg.get('maidenhead', '')
    parts   = []
    if grid:    parts.append("Grid:" + grid)
    if comment: parts.append(comment)
    info    = " ".join(parts) if parts else "QRV ISS"
    payload = ":CQ       :" + info
    try:
        tx_queue.put_nowait({
            "dest":      "CQ",
            "payload":   payload,
            "path":      "ARISS",
            "aprs_type": "Beacon ISS",
            "extra":     {"comment": info}
        })
        print("[TX] Beacon ISS queued : %s" % payload[:60])
    except Exception as e:
        print("[TX] Beacon ISS erreur : %s" % e)


def _do_send_weather():
    """
    Construit et émet un beacon météo APRS (symbole _ = weather station).
    Format : @DDHHMMzDDMM.MMN/DDDMM.MME_CSE/SPDgGGGtTTTrRRRpPPPbBBBBBhHH commentaire
    Unités APRS : vent en knots, temp en °F, pluie en 1/100 inch, pression en 1/10 mbar.
    """
    cfg      = config_manager.data
    geo_mode = cfg.get('geo_mode', 'locator')

    if geo_mode == 'coords':
        # ── Mode coordonnées géographiques directes ──────────────────────────
        try:
            lat = float(cfg.get('lat_manual', '') or '')
            lon = float(cfg.get('lon_manual', '') or '')
        except (ValueError, TypeError):
            lat = lon = None
        if lat is None or lon is None:
            print("[WX] Coordonnées manuelles absentes ou invalides — beacon météo annulé")
            return {"error": "Latitude et longitude requises dans les réglages"}
    else:
        # ── Mode Locator Maidenhead (défaut) ──────────────────────────────────
        grid = cfg.get('maidenhead', '')
        lat, lon = _grid_to_latlon(grid)
        if lat is None:
            print("[WX] Locator Maidenhead absent ou invalide — beacon météo annulé")
            return {"error": "Locator Maidenhead requis dans les réglages"}

    wx = _fetch_openmeteo(lat, lon)
    if wx is None:
        return {"error": "Impossible de récupérer les données Open-Meteo"}

    # ── Conversions vers unités APRS ────────────────────────────────────────
    def ms_to_knots(ms):
        return int(round((ms or 0) * 1.94384))

    def c_to_f(c):
        return int(round((c or 0) * 9 / 5 + 32))

    def mm_to_hundredths_inch(mm):
        return int(round((mm or 0) * 3.93701))

    def hpa_to_tenths_mbar(hpa):
        # pression en 1/10 mbar = 1/10 hPa, sur 5 chiffres
        return int(round((hpa or 0) * 10))

    def deg_to_cardinal(deg):
        """Convertit un angle en degrés vers un point cardinal (16 directions)."""
        cardinals = [
            "N", "NNE", "NE", "ENE",
            "E", "ESE", "SE", "SSE",
            "S", "SSO", "SO", "OSO",
            "O", "ONO", "NO", "NNO",
        ]
        idx = int(((deg or 0) + 11.25) / 22.5) % 16
        return cardinals[idx]

    wind_dir  = int(wx["wind_dir_deg"] or 0)
    wind_spd  = ms_to_knots(wx["wind_speed_ms"])
    gust      = ms_to_knots(wx["gust_ms"])
    temp_f    = c_to_f(wx["temp_c"])
    rain_1h   = mm_to_hundredths_inch(wx["rain_mm"])
    pressure  = hpa_to_tenths_mbar(wx["pressure_hpa"])
    humidity  = int(wx["humidity_pct"] or 0) % 100  # 00 = 100%

    # ── Position APRS ────────────────────────────────────────────────────────
    def deg_to_aprs(deg, is_lat):
        hemi = ('N' if deg >= 0 else 'S') if is_lat else ('E' if deg >= 0 else 'W')
        deg  = abs(deg); d = int(deg); m = (deg - d) * 60
        return ("%02d%05.2f%s" if is_lat else "%03d%05.2f%s") % (d, m, hemi)

    aprs_lat = deg_to_aprs(lat, True)
    aprs_lon = deg_to_aprs(lon, False)

    # ── Horodatage UTC ───────────────────────────────────────────────────────
    import time as _time
    ts = _time.gmtime()
    timestamp = "%02d%02d%02dz" % (ts.tm_mday, ts.tm_hour, ts.tm_min)

    # ── Payload APRS météo (@…_) ─────────────────────────────────────────────
    # Symbole fixe : table '/' code '_' = weather station
    payload = (
        "@%s%s/%s_"
        "%03d/%03d"          # wind direction / speed (knots)
        "g%03d"              # gust (knots)
        "t%03d"              # temp °F (peut être négatif → signe inclus)
        "r%03d"              # rain last hour (1/100 inch)
        "p%03d"              # rain last 24h (on remet la même valeur faute de données)
        "b%05d"              # pressure (1/10 mbar)
        "h%02d"              # humidity %
        " %s"                # commentaire lisible
    ) % (
        timestamp, aprs_lat, aprs_lon,
        wind_dir, wind_spd,
        gust,
        temp_f,
        rain_1h, rain_1h,
        pressure,
        humidity,
        wx["description"],
    )

    wind_cardinal = deg_to_cardinal(wind_dir)
    wind_kmh = round((wx["wind_speed_ms"] or 0) * 3.6, 1)
    gust_kmh  = round((wx["gust_ms"] or 0) * 3.6, 1)
    comment_human = (
        "%.1f°C %d%% HR Vent %s %.1f km/h Rafales %.1f km/h Pression %.1f hPa %s"
        % (wx["temp_c"], wx["humidity_pct"],
           wind_cardinal, wind_kmh, gust_kmh,
           wx["pressure_hpa"], wx["description"])
    )

    print("[WX] Payload : %s" % payload[:80])
    try:
        tx_queue.put_nowait({
            "dest":      "APRS",
            "payload":   payload,
            "path":      None,
            "aprs_type": "Météo",
            "extra":     {
                "temp_c":        wx["temp_c"],
                "humidity_pct":  wx["humidity_pct"],
                "wind_dir":      wind_dir,
                "wind_speed_kmh": wind_kmh,
                "gust_kmh":      gust_kmh,
                "pressure_hpa":  wx["pressure_hpa"],
                "rain_1h_mm":    wx["rain_mm"],
                "description":   wx["description"],
            }
        })
        return {"status": "queued", "wx": wx, "payload": payload}
    except queue.Full:
        return {"error": "File TX pleine"}

threading.Thread(target=_beacon_scheduler, daemon=True, name="beacon-supervisor").start()


@app.route('/version')
def get_version():
    import json as _j
    return _j.dumps({
        "version": APP_VERSION,
        "date":    APP_VERSION_DATE,
        "changelog": APP_CHANGELOG,
    }), 200, {'Content-Type': 'application/json'}

@app.route('/known_callsigns')
def known_callsigns():
    """Retourne la liste des indicatifs dont la position a été reçue depuis le démarrage."""
    with stations_positions_lock:
        cs_list = sorted(stations_positions.keys())
    return jsonify(cs_list)

@app.route('/beacon_status')
def beacon_status():
    """Retourne l'état de chaque type de balise : interval + next_in."""
    _ALL_TYPES = ["station", "iss", "meteo", "propagation"]
    schedules = config_manager.data.get('beacon_schedules', {})
    result = {}
    with _beacon_workers_lock:
        for btype in _ALL_TYPES:
            worker   = _beacon_workers.get(btype, {})
            interval = schedules.get(btype, 0)
            next_at  = worker.get("next_at")
            next_in  = max(0, int(next_at - time.time())) if next_at else None
            result[btype] = {"interval": interval, "next_in": next_in}
    return jsonify({"schedules": result})

@app.route('/vhf_propagation')
def vhf_propagation():
    """
    Proxy serveur pour les indices NOAA SWPC.
    Permet de contourner un éventuel blocage CORS ou réseau côté navigateur.
    Retourne : {sfi, k_index, a_index, hf_cond, vhf_cond, aurora, source}
    """
    import json as _j
    data = _fetch_solar_indices()
    data["source"] = "NOAA SWPC via serveur"
    return _j.dumps(data), 200, {'Content-Type': 'application/json'}


STATS_FILE = 'stats.json'

@app.route('/stats/load')
def stats_load():
    """Charge l'historique des statistiques depuis stats.json."""
    import json as _j
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                return _j.dumps(_j.load(f)), 200, {'Content-Type': 'application/json'}
        except Exception:
            pass
    return _j.dumps({}), 200, {'Content-Type': 'application/json'}


@app.route('/stats/save', methods=['POST'])
def stats_save():
    """Persiste l'historique des statistiques dans stats.json."""
    import json as _j
    try:
        data = request.get_json(force=True, silent=True) or {}
        with open(STATS_FILE, 'w') as f:
            _j.dump(data, f)
        return _j.dumps({'ok': True}), 200, {'Content-Type': 'application/json'}
    except Exception as e:
        return _j.dumps({'ok': False, 'error': str(e)}), 500, {'Content-Type': 'application/json'}


@app.route('/sw.js')
def service_worker():
    """Service Worker minimal pour les Web Push Notifications (pas de VAPID)."""
    sw_code = r"""
self.addEventListener('push', function(event) {
    var data = {};
    try { data = event.data.json(); } catch(e) { data = {title: 'Py-APRS', body: event.data ? event.data.text() : ''}; }
    event.waitUntil(
        self.registration.showNotification(data.title || 'Py-APRS', {
            body:  data.body  || '',
            icon:  data.icon  || '/favicon.ico',
            tag:   data.tag   || 'aprs',
            renotify: true,
            requireInteraction: false
        })
    );
});

self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    event.waitUntil(clients.matchAll({type:'window'}).then(function(cs) {
        if (cs.length) { cs[0].focus(); } else { clients.openWindow('/'); }
    }));
});

/* Activation immédiate */
self.addEventListener('install',  function(e) { self.skipWaiting(); });
self.addEventListener('activate', function(e) { e.waitUntil(self.clients.claim()); });
"""
    from flask import Response as _Resp
    return _Resp(sw_code, mimetype='application/javascript',
                 headers={'Service-Worker-Allowed': '/'})



def _grid_to_latlon_prox(grid):
    """Maidenhead 4 ou 6 chars vers (lat, lon) centre de la case."""
    import math as _math
    grid = grid.upper().strip()
    if len(grid) < 4:
        return None, None
    try:
        lon = (ord(grid[0]) - 65) * 20 - 180 + (int(grid[2])) * 2 + 1.0
        lat = (ord(grid[1]) - 65) * 10 - 90  + (int(grid[3])) + 0.5
        if len(grid) >= 6:
            lon += (ord(grid[4]) - 65) * 2/24.0 - 1.0 + 1/24.0
            lat += (ord(grid[5]) - 65) * 1/24.0 - 0.5 + 0.5/24.0
        return round(lat, 5), round(lon, 5)
    except Exception:
        return None, None


# ══════════════════════════════════════════════════════════════════════════════
# Passages ISS — prédiction & alertes
# ══════════════════════════════════════════════════════════════════════════════

_iss_alerted_passes = set()   # risetime déjà alertés

def _fetch_iss_passes(lat, lon, n=5):
    """
    Interroge open-notify.org pour obtenir les n prochains passages ISS.
    Retourne une liste de dicts : {risetime, duration, risetime_fmt}.
    """
    import urllib.request as _ureq, json as _j
    url = "https://api.open-notify.org/iss-pass.json?lat=%.4f&lon=%.4f&n=%d" % (lat, lon, n)
    try:
        req = _ureq.Request(url, headers={"User-Agent": "Py-APRS/1.0"})
        with _ureq.urlopen(req, timeout=8) as resp:
            data = _j.loads(resp.read())
        passes = data.get("response") or data.get("passes") or []
        result = []
        import datetime as _dt
        for p in passes:
            rt = p.get("risetime", 0)
            dur = p.get("duration", 0)
            result.append({
                "risetime":     rt,
                "duration":     dur,
                "risetime_fmt": _dt.datetime.fromtimestamp(rt).strftime("%d/%m %H:%M"),
                "duration_min": round(dur / 60.0, 1),
            })
        return result
    except Exception as e:
        print("[ISS] Erreur open-notify : %s" % e)
        return []


def _iss_get_latlon():
    """Résout la position station (locator → lat/lon ou manuel)."""
    lat = lon = None
    grid = config_manager.data.get("maidenhead", "").strip()
    if grid and len(grid) >= 4:
        lat, lon = _grid_to_latlon_prox(grid)
    if lat is None:
        try:
            lat = float(config_manager.data.get("lat_manual", ""))
            lon = float(config_manager.data.get("lon_manual", ""))
        except (TypeError, ValueError):
            pass
    return lat, lon


def _iss_pass_worker():
    """Thread daemon : vérifie toutes les minutes si un passage ISS approche."""
    import time as _t
    while True:
        _t.sleep(60)
        try:
            cfg = config_manager.data.get("iss_alert", {})
            if not cfg.get("enabled"):
                continue
            advance_s = float(cfg.get("advance_min", 10)) * 60
            lat, lon = _iss_get_latlon()
            if lat is None:
                continue
            passes = _fetch_iss_passes(lat, lon, n=5)
            now = _t.time()
            for p in passes:
                rt = p["risetime"]
                if rt in _iss_alerted_passes:
                    continue
                delta = rt - now
                if 0 < delta <= advance_s:
                    _iss_alerted_passes.add(rt)
                    frame = {
                        "type":         "iss_pass_alert",
                        "risetime":     rt,
                        "risetime_fmt": p["risetime_fmt"],
                        "duration_min": p["duration_min"],
                        "advance_min":  round(delta / 60.0, 1),
                    }
                    for _q in list(listeners):
                        try: _q.put_nowait(frame)
                        except: pass
                    print("[ISS] Alerte passage dans %.1f min (début %s, durée %s min)" % (
                        delta / 60.0, p["risetime_fmt"], p["duration_min"]))
        except Exception as _e:
            print("[ISS] Erreur worker : %s" % _e)

threading.Thread(target=_iss_pass_worker, daemon=True).start()


@app.route('/iss_passes')
def iss_passes():
    """Retourne les prochains passages ISS pour la position de la station."""
    import json as _j, time as _t
    lat, lon = _iss_get_latlon()
    if lat is None:
        return _j.dumps({"error": "Position non configurée", "passes": []}), 200,                {'Content-Type': 'application/json'}
    passes = _fetch_iss_passes(lat, lon, n=5)
    now = _t.time()
    for p in passes:
        p["in_min"] = round((p["risetime"] - now) / 60.0, 0)
    return _j.dumps({"passes": passes, "lat": lat, "lon": lon}), 200,            {'Content-Type': 'application/json'}


@app.route('/iss_alert_config', methods=['GET', 'POST'])
def iss_alert_config():
    import json as _j
    if request.method == 'POST':
        data = request.get_json(force=True, silent=True) or {}
        ia = config_manager.data.get("iss_alert", {}).copy()
        if "enabled" in data:
            ia["enabled"] = bool(data["enabled"])
        if "advance_min" in data:
            try: ia["advance_min"] = float(data["advance_min"])
            except (TypeError, ValueError): pass
        config_manager.data["iss_alert"] = ia
        with open("config.json", "w") as _f:
            _j.dump(config_manager.data, _f, ensure_ascii=False)
        return _j.dumps({"status": "ok", "iss_alert": ia}), 200,                {'Content-Type': 'application/json'}
    return _j.dumps(config_manager.data.get("iss_alert", {})), 200,            {'Content-Type': 'application/json'}

# ── Routes Carnet de Trafic ───────────────────────────────────────────────────

@app.route('/logbook/entries')
def logbook_entries():
    page      = int(request.args.get('page', 1))
    per_page  = int(request.args.get('per_page', 50))
    search    = request.args.get('search', '').strip()
    direction = request.args.get('direction', '').strip()
    aprs_type = request.args.get('aprs_type', '').strip()
    entries, total = logbook.get_entries(page=page, per_page=per_page,
                                          search=search, direction=direction, aprs_type=aprs_type)
    return jsonify({"entries": entries, "total": total, "page": page, "per_page": per_page})

@app.route('/logbook/note', methods=['POST'])
def logbook_note():
    data = request.get_json(force=True, silent=True) or {}
    ok = logbook.update_note(int(data.get('id', 0)), data.get('note', ''))
    return jsonify({"ok": ok})

@app.route('/logbook/delete', methods=['POST'])
def logbook_delete():
    data = request.get_json(force=True, silent=True) or {}
    ok = logbook.delete(int(data.get('id', 0)))
    return jsonify({"ok": ok})

@app.route('/logbook/clear', methods=['POST'])
def logbook_clear():
    logbook.clear_all()
    return jsonify({"ok": True})

@app.route('/logbook/stats')
def logbook_stats():
    return jsonify(logbook.get_stats())

@app.route('/logbook/export/csv')
def logbook_export_csv():
    from flask import Response as _Resp
    search    = request.args.get('search', '')
    direction = request.args.get('direction', '')
    aprs_type = request.args.get('aprs_type', '')
    csv_data  = logbook.export_csv(search=search, direction=direction, aprs_type=aprs_type)
    fname = "pyaprs_logbook_%s.csv" % time.strftime("%Y%m%d_%H%M%S")
    return _Resp(csv_data, mimetype='text/csv',
                 headers={"Content-Disposition": "attachment; filename=%s" % fname})

@app.route('/logbook/export/adif')
def logbook_export_adif():
    from flask import Response as _Resp
    search    = request.args.get('search', '')
    direction = request.args.get('direction', '')
    aprs_type = request.args.get('aprs_type', '')
    adif_data = logbook.export_adif(search=search, direction=direction, aprs_type=aprs_type)
    fname = "pyaprs_logbook_%s.adi" % time.strftime("%Y%m%d_%H%M%S")
    return _Resp(adif_data, mimetype='text/plain',
                 headers={"Content-Disposition": "attachment; filename=%s" % fname})

@app.route('/logbook/import', methods=['POST'])
def logbook_import():
    """Import CSV ou ADIF — fusion avec le carnet existant."""
    import io, csv as _csv
    file = request.files.get('file')
    if not file:
        return jsonify({"error": "Aucun fichier"}), 400
    raw = file.read().decode('utf-8', errors='replace')
    fname = (file.filename or '').lower()
    count = 0

    if fname.endswith('.csv'):
        reader = _csv.DictReader(io.StringIO(raw))
        with logbook._lock:
            for row in reader:
                entry = {
                    "id":        logbook._counter,
                    "ts":        row.get("date","") + " " + row.get("time",""),
                    "date":      row.get("date",""),
                    "time":      row.get("time",""),
                    "direction": row.get("direction","RX"),
                    "callsign":  (row.get("callsign","") or "").upper()[:20],
                    "dest":      (row.get("dest","") or "").upper()[:20],
                    "path":      row.get("path","")[:100],
                    "aprs_type": row.get("aprs_type","")[:40],
                    "payload":   row.get("payload","")[:200],
                    "comment":   row.get("comment","")[:100],
                    "lat":       _safe_float(row.get("lat")),
                    "lon":       _safe_float(row.get("lon")),
                    "speed_kmh": _safe_float(row.get("speed_kmh")),
                    "alt_m":     _safe_float(row.get("alt_m")),
                    "symbol":    row.get("symbol","")[:10],
                    "source":    row.get("source","import"),
                    "freq":      row.get("freq","144.800"),
                    "band":      row.get("band","2m"),
                    "mode":      row.get("mode","APRS"),
                    "note":      row.get("note","")[:200],
                }
                logbook._counter += 1
                logbook.entries.append(entry)
                count += 1
            logbook.entries.sort(key=lambda x: x.get("ts",""), reverse=True)
            logbook._save()

    elif fname.endswith('.adi') or fname.endswith('.adif'):
        import re as _re2
        records = _re2.split(r'<EOR>', raw, flags=_re2.IGNORECASE)
        with logbook._lock:
            for rec in records:
                def _field(tag):
                    m = _re2.search(r'<%s:\d+(?::[A-Z])?>(.*?)(?=<|\Z)' % tag, rec, _re2.IGNORECASE|_re2.DOTALL)
                    return m.group(1).strip() if m else ""
                cs = _field("CALL")
                if not cs:
                    continue
                date_raw = _field("QSO_DATE")
                time_raw = _field("TIME_ON")
                date_fmt = "%s-%s-%s" % (date_raw[:4], date_raw[4:6], date_raw[6:8]) if len(date_raw) >= 8 else ""
                time_fmt = "%s:%s:%s" % (time_raw[:2], time_raw[2:4], time_raw[4:6]) if len(time_raw) >= 4 else ""
                entry = {
                    "id":        logbook._counter,
                    "ts":        date_fmt + " " + time_fmt,
                    "date":      date_fmt,
                    "time":      time_fmt,
                    "direction": "RX",
                    "callsign":  cs.upper()[:20],
                    "dest":      "APRS",
                    "path":      "",
                    "aprs_type": "Import ADIF",
                    "payload":   "",
                    "comment":   _field("COMMENT")[:100],
                    "lat":       None,
                    "lon":       None,
                    "speed_kmh": None,
                    "alt_m":     None,
                    "symbol":    "",
                    "source":    "ADIF",
                    "freq":      _field("FREQ") or "144.800",
                    "band":      _field("BAND") or "2m",
                    "mode":      _field("MODE") or "APRS",
                    "note":      _field("NOTES")[:200],
                }
                logbook._counter += 1
                logbook.entries.append(entry)
                count += 1
            logbook.entries.sort(key=lambda x: x.get("ts",""), reverse=True)
            logbook._save()
    else:
        return jsonify({"error": "Format non supporté (CSV ou ADIF/ADI)"}), 400

    return jsonify({"ok": True, "imported": count})


@app.route('/logbook/add', methods=['POST'])
def logbook_add():
    """Ajout manuel d'un contact dans le carnet de trafic."""
    data = request.get_json(force=True, silent=True) or {}
    cs = (data.get('callsign') or '').upper().strip()[:20]
    if not cs:
        return jsonify({"error": "Indicatif requis"}), 400

    # Date/heure : utiliser la valeur saisie ou l'heure actuelle
    date_str = (data.get('date') or '').strip()
    time_str = (data.get('time') or '').strip()
    if not date_str:
        date_str = time.strftime("%Y-%m-%d")
    if not time_str:
        time_str = time.strftime("%H:%M:%S")
    elif len(time_str) == 5:          # HH:MM → HH:MM:00
        time_str = time_str + ":00"
    ts_str = date_str + " " + time_str

    direction = data.get('direction', 'RX').upper()
    if direction not in ('RX', 'TX'):
        direction = 'RX'

    with logbook._lock:
        entry = {
            "id":         logbook._counter,
            "ts":         ts_str,
            "date":       date_str,
            "time":       time_str,
            "direction":  direction,
            "callsign":   cs,
            "dest":       (data.get('dest') or 'APRS').upper().strip()[:20],
            "path":       (data.get('path') or '')[:100],
            "aprs_type":  (data.get('aprs_type') or 'Manuel')[:40],
            "payload":    '',
            "comment":    (data.get('comment') or '')[:100],
            "lat":        _safe_float(data.get('lat')),
            "lon":        _safe_float(data.get('lon')),
            "speed_kmh":  None,
            "alt_m":      None,
            "symbol":     '',
            "source":     'Manuel',
            "freq":       (data.get('freq') or '144.800')[:20],
            "band":       (data.get('band') or '2m')[:10],
            "mode":       (data.get('mode') or 'APRS')[:20],
            "note":       (data.get('note') or '')[:200],
        }
        logbook._counter += 1
        logbook.entries.insert(0, entry)
        if len(logbook.entries) > 5000:
            logbook.entries = logbook.entries[:5000]
        logbook._save()

    return jsonify({"ok": True, "entry": entry})


def _safe_float(v):
    try:
        return float(v) if v not in (None, '', 'None') else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Synchronisation Wavelog
# ══════════════════════════════════════════════════════════════════════════════

class WavelogSync:
    """
    Synchronise le carnet de trafic Py-APRS vers Wavelog via son API REST.

    API Wavelog utilisée :
      POST /index.php/api/qso        — ajout d'un QSO
      GET  /index.php/api/station_info  — vérification connexion + infos station

    Doc officielle : https://github.com/wavelog/wavelog/wiki/API
    """

    # Statut exposé en temps réel à l'interface
    status        = "–"
    last_sync_ts  = ""
    synced_count  = 0
    last_error    = ""
    connected     = False

    def __init__(self):
        self._stop   = threading.Event()
        self._thread = None

    # ── Vérification connexion ─────────────────────────────────────────────
    def test_connection(self):
        """
        Teste la connexion à Wavelog.
        Retourne (ok: bool, message: str, info: dict|None).
        """
        import urllib.request as _ur, json as _j
        cfg = config_manager.data.get("wavelog", {})
        url = (cfg.get("url") or "").rstrip("/")
        key = cfg.get("api_key", "").strip()
        if not url or not key:
            return False, "URL ou clé API manquante", None
        endpoint = url + "/index.php/api/station_info"
        try:
            req = _ur.Request(
                endpoint,
                data=_j.dumps({"api": key}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "Py-APRS/2.2"},
                method="GET"
            )
            with _ur.urlopen(req, timeout=8) as resp:
                data = _j.loads(resp.read())
            if data.get("status") == "ok" or "station_name" in data or isinstance(data, list):
                self.connected = True
                self.last_error = ""
                return True, "Connexion OK", data
            else:
                msg = data.get("message") or data.get("error") or "Réponse inattendue"
                self.connected = False
                self.last_error = msg
                return False, msg, data
        except Exception as e:
            self.connected = False
            self.last_error = str(e)
            return False, str(e), None

    # ── Envoi d'un QSO ────────────────────────────────────────────────────
    def _push_qso(self, entry):
        """
        Envoie une entrée du carnet vers l'API Wavelog.
        Retourne True si succès, False sinon.
        """
        import urllib.request as _ur, json as _j
        cfg        = config_manager.data.get("wavelog", {})
        url        = (cfg.get("url") or "").rstrip("/")
        key        = cfg.get("api_key", "").strip()
        station_id = int(cfg.get("station_id", 1) or 1)

        if not url or not key:
            return False

        cs   = entry.get("callsign", "") or ""
        if not cs or cs in ("APRS","BEACON","CQ","?",""):
            return False

        # Formatage des champs Wavelog
        date_raw = (entry.get("date","") or "").replace("-","")   # YYYYMMDD
        time_raw = (entry.get("time","") or "").replace(":","")[:6]  # HHMMSS
        if not date_raw or not time_raw:
            return False

        band = entry.get("band","2m") or "2m"
        mode = entry.get("mode","APRS") or "APRS"
        freq = entry.get("freq","144.800") or "144.800"
        rst  = "599"
        comment = entry.get("comment","") or ""
        note    = entry.get("note","") or ""
        aprs_t  = entry.get("aprs_type","") or ""
        my_call = config_manager.data.get("callsign","N0CALL").upper()

        # Wavelog attend le champ "call" (indicatif distant), sans SSID pour la base
        # mais on envoie le callsign complet dans les notes pour traçabilité APRS
        qso_body = {
            "api":            key,
            "station_id":     station_id,
            "call":           cs.split("-")[0],   # sans SSID pour compatibilité ADIF
            "rst_sent":       rst,
            "rst_rcvd":       rst,
            "qso_date":       date_raw,
            "time_on":        time_raw,
            "band":           band,
            "mode":           mode,
            "freq":           freq,
            "station_callsign": my_call,
            "comment":        ("[%s] %s %s" % (aprs_t, comment, note)).strip()[:255],
        }

        # Coordonnées GPS si disponibles
        if entry.get("lat") is not None and entry.get("lon") is not None:
            try:
                qso_body["gridsquare"] = _latlon_to_maidenhead(float(entry["lat"]), float(entry["lon"]))
            except Exception:
                pass

        endpoint = url + "/index.php/api/qso"
        try:
            req = _ur.Request(
                endpoint,
                data=_j.dumps(qso_body).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "Py-APRS/2.2"},
                method="POST"
            )
            with _ur.urlopen(req, timeout=10) as resp:
                result = _j.loads(resp.read())
            # Wavelog renvoie {"status":"created"} ou {"status":"ok"} en succès
            ok = result.get("status") in ("created","ok","success","200")
            if not ok:
                print("[WAVELOG] Erreur QSO %s : %s" % (cs, result))
            return ok
        except Exception as e:
            print("[WAVELOG] Exception push_qso %s : %s" % (cs, e))
            return False

    # ── Synchro par lot ────────────────────────────────────────────────────
    def sync_pending(self):
        """
        Synchronise les nouvelles entrées du carnet (id > last_sync_id).
        Retourne le nombre de QSO envoyés avec succès.
        """
        cfg      = config_manager.data.get("wavelog", {})
        only_qso = cfg.get("only_qso", True)
        sync_rx  = cfg.get("sync_rx", True)
        sync_tx  = cfg.get("sync_tx", True)
        last_id  = int(cfg.get("last_sync_id", 0) or 0)

        # Types de trames à synchroniser selon le mode "QSO seulement"
        QSO_TYPES = {"Message", "Mic-E", "Position", "Position+Msg",
                     "Position+TS", "Position+TS+Msg", "Beacon ISS"}

        with logbook._lock:
            candidates = [
                e for e in reversed(logbook.entries)   # du plus ancien au plus récent
                if e["id"] > last_id
                and ((e["direction"] == "RX" and sync_rx) or (e["direction"] == "TX" and sync_tx))
                and (not only_qso or e.get("aprs_type","") in QSO_TYPES)
                and (e.get("callsign","") not in ("","APRS","BEACON","CQ","?"))
            ]

        if not candidates:
            return 0

        pushed   = 0
        max_id   = last_id
        for entry in candidates:
            ok = self._push_qso(entry)
            if ok:
                pushed += 1
            if entry["id"] > max_id:
                max_id = entry["id"]

        # Mise à jour last_sync_id même si certains échouent (évite boucle infinie)
        if max_id > last_id:
            wl_cfg = config_manager.data.get("wavelog", {}).copy()
            wl_cfg["last_sync_id"] = max_id
            config_manager.data["wavelog"] = wl_cfg
            try:
                with open("config.json", "w") as _f:
                    json.dump(config_manager.data, _f, ensure_ascii=False)
            except Exception:
                pass

        self.synced_count += pushed
        self.last_sync_ts  = time.strftime("%H:%M:%S")
        print("[WAVELOG] Sync : %d/%d QSO envoyés (last_id=%d)" % (pushed, len(candidates), max_id))
        return pushed

    # ── Thread de synchro automatique ─────────────────────────────────────
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="wavelog-sync")
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                cfg = config_manager.data.get("wavelog", {})
                if cfg.get("enabled"):
                    interval_min = max(1, int(cfg.get("sync_interval", 5) or 5))
                    ok, msg, _ = self.test_connection()
                    if ok:
                        self.status = "✅ Connecté"
                        pushed = self.sync_pending()
                        if pushed:
                            self.status = "✅ Sync OK (%d)" % pushed
                    else:
                        self.status = "❌ " + msg
                    # Attente découpée en tranches de 10 s (réactif aux changements config)
                    for _ in range(interval_min * 6):
                        if self._stop.is_set():
                            break
                        time.sleep(10)
                else:
                    self.status = "–"
                    time.sleep(15)
            except Exception as e:
                self.status = "❌ Erreur: %s" % e
                print("[WAVELOG] Erreur boucle : %s" % e)
                time.sleep(30)

    def stop(self):
        self._stop.set()


def _latlon_to_maidenhead(lat, lon):
    """Conversion lat/lon décimaux → Maidenhead 6 caractères."""
    lon += 180; lat += 90
    f1 = chr(65 + int(lon / 20))
    f2 = chr(65 + int(lat / 10))
    f3 = str(int((lon % 20) / 2))
    f4 = str(int(lat % 10))
    f5 = chr(65 + int((lon % 2) / (2/24.0)))
    f6 = chr(65 + int((lat % 1) / (1/24.0)))
    return (f1+f2+f3+f4+f5+f6).upper()


wavelog_sync = WavelogSync()
wavelog_sync.start()


# ── Routes Wavelog ────────────────────────────────────────────────────────────

@app.route('/wavelog/status')
def wavelog_status():
    return jsonify({
        "enabled":      config_manager.data.get("wavelog", {}).get("enabled", False),
        "connected":    wavelog_sync.connected,
        "status":       wavelog_sync.status,
        "last_sync":    wavelog_sync.last_sync_ts,
        "synced_total": wavelog_sync.synced_count,
        "last_error":   wavelog_sync.last_error,
        "last_sync_id": config_manager.data.get("wavelog", {}).get("last_sync_id", 0),
    })

@app.route('/wavelog/test', methods=['POST'])
def wavelog_test():
    ok, msg, info = wavelog_sync.test_connection()
    return jsonify({"ok": ok, "message": msg, "info": info})

@app.route('/wavelog/sync_now', methods=['POST'])
def wavelog_sync_now():
    """Force une synchro immédiate (bouton "Synchroniser maintenant")."""
    cfg = config_manager.data.get("wavelog", {})
    if not cfg.get("enabled"):
        return jsonify({"error": "Wavelog non activé"}), 400
    ok, msg, _ = wavelog_sync.test_connection()
    if not ok:
        return jsonify({"error": "Connexion échouée : " + msg}), 502
    pushed = wavelog_sync.sync_pending()
    return jsonify({"ok": True, "pushed": pushed, "status": wavelog_sync.status})

@app.route('/wavelog/reset_sync', methods=['POST'])
def wavelog_reset_sync():
    """Réinitialise le curseur de synchro (re-enverra tout depuis le début)."""
    wl = config_manager.data.get("wavelog", {}).copy()
    wl["last_sync_id"] = 0
    config_manager.data["wavelog"] = wl
    try:
        with open("config.json", "w") as f:
            json.dump(config_manager.data, f, ensure_ascii=False)
    except Exception:
        pass
    return jsonify({"ok": True})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)
