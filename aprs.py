# WX_APRS_BULLETIN_FIX_v1
# PATCH_VERSION_268_HTML_APPLIED
# PATCH_RX_BROADCASTER_CRASH_FIX_APPLIED
# PATCH_POSITION_REGEX_SPACES_APPLIED
# PATCH_TRAFFIC_FREEZE_FIX_APPLIED
# PATCH_VERSION_BUMP_SSTV_REMOVE_APPLIED
# PATCH_SSTV_REMOVE_APPLIED
# PATCH_BLACKOUT_ORDER_APPLIED
# -*- coding: utf-8 -*-
import os
import json
import numpy as np
import sounddevice as sd
import serial
import time
import crcmod
from flask import Flask, render_template, request, jsonify, Response, session, redirect, url_for
import threading
import platform
import collections
import queue
import re as _re
import re
import hashlib
import logging
import logging.handlers
import gzip
import shutil

# ── Configuration du logger APRS ────────────────────────────────────────────
# Rotation quotidienne à minuit + compression gzip des archives
# aprs.log (courant) → aprs.log.2025-01-15.gz (archive)
# Rétention : 2 jours (adapté SD card / Raspberry Pi)

_LOG_FILE = "aprs.log"
_LOG_BACKUP_COUNT = 2   # 2 jours d'archives gzippées

class _GzipTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Rotation quotidienne à minuit avec compression gzip automatique."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def rotation_filename(self, default_name):
        """Ajoute .gz à l'extension pour le fichier archivé."""
        return default_name + ".gz"

    def rotate(self, source, dest):
        """Compresse le fichier source vers dest (gzip)."""
        try:
            with open(source, "rb") as f_in, gzip.open(dest, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            os.remove(source)
        except Exception as _e:
            # Fallback sans compression si erreur
            shutil.move(source, dest)


_log_handler = _GzipTimedRotatingFileHandler(
    _LOG_FILE,
    when="midnight",          # rotation à 00h00 heure locale
    interval=1,               # tous les jours
    backupCount=_LOG_BACKUP_COUNT,
    encoding="utf-8",
    delay=False,
    utc=False,
)

_LOG_FORMAT = logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_log_handler.setFormatter(_LOG_FORMAT)
_log_handler.suffix = "%Y-%m-%d"   # ex: aprs.log.2025-01-15.gz

# Console (stdout) — même format, niveau INFO par défaut
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
))
_console_handler.setLevel(logging.INFO)

logger = logging.getLogger("aprs")
logger.setLevel(logging.DEBUG)
logger.addHandler(_log_handler)
logger.addHandler(_console_handler)
logger.propagate = False

logger.info("=== APRS Dashboard démarré — log rotation: quotidienne, rétention %d jours ===",
            _LOG_BACKUP_COUNT)

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
    "beacon_text1": "",          # Texte libre balise automatique 1
    "beacon_text2": "",          # Texte libre balise automatique 2

    # ── Mode HF 300 bauds ────────────────────────────────────────────────────
    "hf_mode":          False,          # True = 300 bauds HF (APRS-HF / USB)
    "hf_mark_hz":       1600,           # fréquence MARK  (défaut AX.25 HF : 1600 Hz)
    "hf_space_hz":      1800,           # fréquence SPACE (défaut AX.25 HF : 1800 Hz)
    # ── iGate APRS-IS ────────────────────────────────────────────────────────
    "igate_enabled":  False,
    "igate_server":   "rotate.aprs2.net",
    "igate_port":     14580,
    "igate_passcode": "-1",
    "igate_filter":   "r/46.5/1.5/200",   # filtre région Centre-Val de Loire
    "igate_rx_only":  True,                # True = RX-iGate seulement, False = TX aussi
    # ── Digipeater ───────────────────────────────────────────────────────────
    "digi_enabled":   False,
    "digi_aliases":   ["WIDE1-1", "WIDE1", "RELAY"],  # alias acceptés
    "digi_limit":     2,                   # nb max de sauts WIDEn-N tolérés
    # ── Alertes météo (Open-Meteo) ───────────────────────────────────────────
    "weather_alert": {
        "enabled":        False,
        "interval_min":   30,     # intervalle de vérification en minutes
        "temp_max":       38.0,   # °C  — alerte chaleur extrême
        "temp_min":       -5.0,   # °C  — alerte gel
        "wind_max":       60.0,   # km/h (= ~16.7 m/s)
        "gust_max":       80.0,   # km/h
        "rain_mm":        10.0,   # mm/h
        "wmo_severe":     True,   # alertes sur codes WMO ≥ 65 (pluie forte, orages…)
        "bulletin_aprs":  True,   # émettre bulletin APRS RF (BLNWXn) lors d'une alerte
    },
        # ── Alertes passage ISS ──────────────────────────────────────────────────
    "iss_alert": {
        "enabled":      False,
        "advance_min":  10,    # minutes avant le passage pour envoyer l'alerte
    },
    # ── Alertes propagation / blackout ──────────────────────────────────────
    "prop_alert": {
        "enabled":       False,
        "interval_min":  15,      # vérification toutes les N minutes
        "kp_max":         5.0,    # alerte tempête géomagnétique
        "sfi_min":       70.0,    # alerte SFI trop bas (blackout HF)
        "xray_class":   "M1",     # alerte éruption solaire : M1 | M5 | X1 | X5
    },
    # ── API aprs.fi (optionnel) — clé pour /signal_path ───────────────────────
    "aprsfi_key": "",   # obtenir sur https://aprs.fi/page/api
}

app = Flask(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# ── LOGIN_PATCH_APPLIED ──
# Système d'authentification Login / Password
# ══════════════════════════════════════════════════════════════════════════════
import secrets as _secrets
from werkzeug.security import generate_password_hash as _gen_hash, \
                              check_password_hash   as _chk_hash
from functools import wraps as _wraps

_USERS_FILE   = "users.json"
_DEFAULT_USER = "admin"
_DEFAULT_PASS = "aprs1200"

# Clé secrète Flask (persistante entre redémarrages)
_SECRET_KEY_FILE = ".flask_secret"
if os.path.exists(_SECRET_KEY_FILE):
    with open(_SECRET_KEY_FILE) as _f:
        app.secret_key = _f.read().strip()
else:
    app.secret_key = _secrets.token_hex(32)
    with open(_SECRET_KEY_FILE, "w") as _f:
        _f.write(app.secret_key)

# ── Gestion users.json ────────────────────────────────────────────────────────

def _load_users():
    if os.path.exists(_USERS_FILE):
        try:
            with open(_USERS_FILE, "r", encoding="utf-8") as _f:
                return json.load(_f)
        except Exception:
            pass
    # Créer utilisateur par défaut
    users = {_DEFAULT_USER: _gen_hash(_DEFAULT_PASS)}
    _save_users(users)
    logger.info("[AUTH] Fichier users.json créé avec compte par défaut (admin/aprs1200)")
    return users

def _save_users(users):
    tmp = _USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as _f:
        json.dump(users, _f, ensure_ascii=False, indent=2)
    os.replace(tmp, _USERS_FILE)

_users_db = _load_users()

# ── Décorateur de protection des routes ──────────────────────────────────────

def _login_required(f):
    @_wraps(f)
    def _decorated(*args, **kwargs):
        if not session.get("logged_in"):
            # SSE (EventSource) : retourner 401, pas une redirection HTML
            if request.headers.get("Accept", "").startswith("text/event-stream"):
                return Response("data: {\"type\":\"auth_required\"}\n\n",
                                status=401, mimetype="text/event-stream")
            # Requêtes JSON/API
            if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"error": "Non authentifié", "redirect": "/login"}), 401
            return redirect("/login?next=" + request.path)
        return f(*args, **kwargs)
    return _decorated

# ── Page de login ─────────────────────────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>APRS Station — Connexion</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0a0f1e;font-family:'Segoe UI',system-ui,sans-serif;
       display:flex;align-items:center;justify-content:center;min-height:100vh;
       color:#e2e8f0}
  .card{background:#111827;border:1px solid #1e3a5f;border-radius:16px;
        padding:2.5rem 2rem;width:100%;max-width:400px;
        box-shadow:0 0 40px rgba(59,130,246,.15)}
  .logo{text-align:center;margin-bottom:2rem}
  .logo .icon{font-size:3rem;display:block;margin-bottom:.5rem}
  .logo h1{font-size:1.4rem;font-weight:700;color:#60a5fa;letter-spacing:.05em}
  .logo p{font-size:.8rem;color:#64748b;margin-top:.25rem}
  label{display:block;font-size:.75rem;color:#94a3b8;margin-bottom:.4rem;
        text-transform:uppercase;letter-spacing:.08em}
  input{width:100%;background:#0f172a;border:1px solid #1e3a5f;border-radius:8px;
        padding:.75rem 1rem;color:#e2e8f0;font-size:.95rem;outline:none;
        transition:border .2s}
  input:focus{border-color:#3b82f6}
  .field{margin-bottom:1.25rem}
  .btn{width:100%;background:linear-gradient(135deg,#1d4ed8,#2563eb);
       border:none;border-radius:8px;padding:.85rem;color:#fff;font-size:1rem;
       font-weight:600;cursor:pointer;transition:opacity .2s;margin-top:.5rem}
  .btn:hover{opacity:.9}
  .error{background:rgba(239,68,68,.15);border:1px solid rgba(239,68,68,.4);
         border-radius:8px;padding:.75rem 1rem;color:#f87171;
         font-size:.85rem;margin-bottom:1.25rem;display:none}
  .error.show{display:block}
  .footer{text-align:center;margin-top:1.5rem;font-size:.7rem;color:#334155}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <span class="icon">📡</span>
    <h1>APRS Station</h1>
    <p>Identification requise</p>
  </div>
  <div class="error {ERR_CLASS}" id="err">{ERR_MSG}</div>
  <form method="POST" action="/login">
    <input type="hidden" name="next" value="{NEXT}">
    <div class="field">
      <label>Identifiant</label>
      <input type="text" name="username" autofocus autocomplete="username" value="{USERNAME}">
    </div>
    <div class="field">
      <label>Mot de passe</label>
      <input type="password" name="password" autocomplete="current-password">
    </div>
    <button class="btn" type="submit">Se connecter</button>
  </form>
  <div class="footer">APRS Station v{VERSION}</div>
</div>

</body>
</html>"""

@app.route('/login', methods=['GET', 'POST'])
def login():
    next_url = request.args.get("next", "/")
    err = ""
    username = ""
    if request.method == "POST":
        next_url  = request.form.get("next", "/")
        username  = request.form.get("username", "").strip()
        password  = request.form.get("password", "")
        ph = _users_db.get(username)
        if ph and _chk_hash(ph, password):
            session.permanent = True
            session["logged_in"] = True
            session["username"]  = username
            logger.info("[AUTH] Connexion réussie : %s", username)
            return redirect(next_url or "/")
        err = "Identifiant ou mot de passe incorrect."
        logger.warning("[AUTH] Échec connexion : %s", username)
    html = _LOGIN_HTML.replace("{ERR_CLASS}", "show" if err else "") \
                      .replace("{ERR_MSG}",   err) \
                      .replace("{NEXT}",      next_url) \
                      .replace("{USERNAME}",  username) \
                      .replace("{VERSION}",   APP_VERSION)
    return Response(html, mimetype="text/html", status=200 if not err else 401)

@app.route('/logout')
def logout():
    user = session.get("username", "?")
    session.clear()
    logger.info("[AUTH] Déconnexion : %s", user)
    return redirect("/login")

# ── Changement de mot de passe ────────────────────────────────────────────────

@app.route('/auth/change_password', methods=['POST'])
def auth_change_password():
    if not session.get("logged_in"):
        return jsonify({"error": "Non authentifié"}), 401
    data = request.json or {}
    current  = data.get("current_password", "")
    new_pass = data.get("new_password", "").strip()
    username = session.get("username", "")
    ph = _users_db.get(username)
    if not ph or not _chk_hash(ph, current):
        return jsonify({"ok": False, "error": "Mot de passe actuel incorrect"}), 403
    if len(new_pass) < 6:
        return jsonify({"ok": False, "error": "Le mot de passe doit faire au moins 6 caractères"}), 400
    _users_db[username] = _gen_hash(new_pass)
    _save_users(_users_db)
    logger.info("[AUTH] Mot de passe changé pour %s", username)
    return jsonify({"ok": True})

@app.route('/auth/whoami')
def auth_whoami():
    return jsonify({
        "logged_in": bool(session.get("logged_in")),
        "username":  session.get("username", ""),
    })

# ── Durée de session (30 jours) ───────────────────────────────────────────────
from datetime import timedelta as _timedelta
app.permanent_session_lifetime = _timedelta(days=30)



# ── Version applicative ──────────────────────────────────────────────────────
APP_VERSION = "2.6.9"
APP_VERSION_DATE = "2026-06-21"
APP_CHANGELOG = [
        {"version": "2.6.9", "date": "2026-06-21", "label": "current", "changes": [
            "Bulletin APRS RF alerte meteo : titres sans accents (ex. 'Chaleur extreme' au lieu de 'Chaleur extrême') pour compatibilite TNC/terminaux",
            "Bulletin APRS RF alerte meteo : retrait du seuil configure du payload RF, conserve uniquement dans l'alerte SSE/web",
            "Bulletin APRS RF alerte meteo : affichage avec le badge et la couleur Meteo dans le moniteur de trafic au lieu du badge TX generique",
            "Log systeme : retention reduite de 30 a 2 jours d'archives gzippees",
        ]},
        {"version": "2.6.8", "date": "2026-06-18", "label": "", "changes": [
            "QSO : barre de macros en 3 groupes — QSO standard (CQ/73/QTH/QRZ?/RST/QSL/POTA), APRS direct (?APRST/?APRSV/?WX/?PING?, envoi immediat sans confirmation), ISS/ARISS (CQ ARISS/QTH/73 ISS/PSE QSL/Test)",
            "QSO : substitution dynamique {MY}/{LOC}/{DEST} dans les macros via _macroExpand",
            "QSO : envoi direct _chatSendDirect() pour les macros APRS (contourne le champ de saisie)",
            "QSO : mode ISS isole (openISS) — path ARISS force automatiquement a l envoi, badge path dans la barre de macros",
            "QSO : panel ISS en vue integre dans la fenetre chat — elevation, azimut, boussole SVG animee, Doppler, frequence corrigee",
            "QSO : appel general CQ (openCQGeneral) — destination APRS broadcast, bulle dediee avec avatar 📢",
            "QSO : separateur de date dans l historique des messages (regroupement par jour)",
        ]},
        {"version": "2.6.7", "date": "2026-06-16", "label": "", "changes": [
            "Moniteur de trafic : correction du gel après un certain temps de fonctionnement",
            "Broadcaster SSE : retrait automatique des listeners dont la queue est saturée (>5 erreurs consécutives)",
            "Watchdog KISS : redémarrage automatique de Direwolf si aucun octet reçu depuis 120 s",
            "SSE keepalive réduit à 8 s pour détection rapide des clients déconnectés",
        ]},
        {"version": "2.6.6", "date": "2026-06-14", "label": "", "changes": [
        "Log systeme : rotation quotidienne a minuit avec compression gzip automatique (aprs.log.YYYY-MM-DD.gz, 30 jours de retention)",
        "Routes /log?n=&level= et /log/archives pour consulter le journal et lister les archives depuis le tableau de bord",
        "Moniteur de trafic SSE : reconnexion automatique avec backoff exponentiel (3 s -> 30 s) en cas de coupure ou redemarrage serveur",
        "SSE : handler decoupe en _sseOnMessage / _sseOnError / _handleSseFrame, evite les doublons d'event listener apres reconnexion",
        "SSE cote Flask : gestion GeneratorExit explicite, compteur consecutive_errors, nettoyage listeners.remove() protege par try/except",
        "Optimisation dedup : _isDupContent ne purge _contentDedup que toutes les 60 s (pas a chaque trame), _seenFids tronque si > 800",
        "Beacon ISS : bouton desactive (grise) par defaut, s'active automatiquement (fond indigo pulse) uniquement quand l'ISS est en vue",
        "Sidebar Trafic : panel ISS EN VUE deplace en haut de la sidebar, avant le badge prochain passage",
    ]},
        {"version": "2.6.5", "date": "2026-06-11", "label": "", "changes": [
        "Suppression du recepteur SSTV integre (decodeur Python, monitoring signal, galerie locale)",
        "Onglet SSTV remplace par un panneau de liens vers des outils externes : Robot36 WebAssembly, QSSTV (Linux/Raspberry Pi), ColorSSTV/MMSSTV (Windows), galerie ISS SSTV",
        "Tableau des frequences SSTV usuelles (ISS 145.800, HF 14.230/14.233, signal VIS 1900 Hz)",
        "Balises texte libre : boutons 📝 Texte1 / Texte2 dans le panneau QSO (visibles uniquement si le texte est configure)",
        "Routes POST /send_text1 et /send_text2, fonctions _do_send_text1/_do_send_text2",
    ]},
    {"version": "2.6.4", "date": "2026-06-11", "label": "", "changes": [
        "Correction decodage Mic-E : le flag Ouest (West) est lu sur d[5] (spec APRS 1.0.1 §10) et non d[4] — corrige les distances farfelues pour stations françaises a 7°E et stations americaines",
    ]},
    {"version": "2.6.3", "date": "2026-06-10", "label": "", "changes": [
        "Balises texte libre : deux nouvelles balises Text1 et Text2 a parametrer librement (texte + intervalle), visibles dans le panneau Statut Station et dans les Reglages",
    ]},
    {"version": "2.6.2", "date": "2026-06-10", "label": "", "changes": [
        "Badge alertes propagation/blackout dans le header a cote du voyant PTT : 🧲 Kp / 📉 SFI / ☀️ X-ray, pulsation rouge, clic pour fermer toutes les bannieres",
    ]},
    {"version": "2.6.1", "date": "2026-06-10", "label": "", "changes": [
        "Mobile TRAFIC : console APRS en premier sur smartphone (CSS order), panneau Envoyer/Statut en accordeon repliable",
        "Mobile TRAFIC : header console sticky (compteurs TX/RX/IS + filtre + barre RX), scroll-to-top auto a l'activation de l'onglet",
        "Mobile TRAFIC : cartes APRS ultra-compactes (8px padding, 9-10px font), hauteur calc(100vh-180px)",
    ]},
    {"version": "2.6.0", "date": "2026-06-10", "label": "", "changes": [
        "Recherche périmétrique : route /nearby_stations (rayon, âge max, filtre mobile) + modale carte avec cercle Leaflet et liste triée par distance",
    ]},
    {"version": "2.5.9", "date": "2026-06-09", "label": "", "changes": [
        "Stats : maillage RF — carte de chaleur SVG des cases Maidenhead 4 car. entendues",
        "Cases colorées par densité (bleu→cyan→vert), cercles 50/100/200 km, croix station locale",
        "Compteur de cases distinctes, bouton reset, persistance dans stats.json (gridCells)",
        "Route POST /stats/reset_mesh pour effacer uniquement les données maillage",
    ]},
    {"version": "2.5.8", "date": "2026-06-09", "label": "", "changes": [
        "Stats : meilleur DX entendu (indicatif, distance, azimut, date) persisté dans stats.json",
        "Stats : distance maximale atteinte affichée dans un badge dédié",
        "Stats : 2 nouvelles cartes dans la grille des compteurs (Best DX + Distance max)",
    ]},
    {"version": "2.5.7", "date": "2026-06-05", "label": "", "changes": [
        "ISS 24h : redesign complet des cartes de passage (layout 2 colonnes, zones claires)",
        "ISS 24h : bloc probabilite contact APRS (barre + % + label qualitatif) sur chaque passage futur",
        "ISS 24h : passages passes rendus discrets (opacite reduite, probabilite masquee)",
        "Miroir Reglages ISS : badge 📡XX% inline sur chaque passage",
    ]},
    {"version": "2.5.6", "date": "2026-06-03", "label": "", "changes": [
        "Alertes propagation / blackout : bannière SSE + bip sur tempête géomagnétique (Kp), dégradation SFI et éruption solaire X-ray GOES",
        "3 seuils configurables : Kp max (G1–G3), SFI min, classe X-ray (C1 à X5)",
        "Worker thread _prop_alert_worker + routes /prop_alert_config et /prop_alert_test",
        "Passages ISS 24h glissantes : nouveau bloc dans l'onglet ISS (SGP4, n=20, cache 15 min)",
        "Widget TRAFIC ISS : passages du jour (minuit→minuit) remplacent les 5 prochains passages",
        "Correction bugs ISS : countdown sur 1er passage futur, badge PASSÉ/EN COURS, isNext sur bon index, race condition cache dict",
    ]},
    {"version": "2.5.5", "date": "2026-06-03", "label": "", "changes": [
        "HF 300 bauds : forçage automatique path vide si WIDE détecté dans send_packet",
        "UI : presets path dédiés HF (Direct RF, GATE, RELAY) dans le select Digi Path",
        "UI : warning visuel si hf_mode actif et path incompatible WIDE",
    ]},
    {"version": "2.5.4", "date": "2026-06-02", "label": "", "changes": [
        "Mode HF 300 bauds : directive Dire Wolf MODEM 300 MARK:SPACE (défaut 1600/1800 Hz)",
        "TXDELAY automatiquement fixé à 500 ms minimum en mode HF",
        "Section dédiée dans ⚙️ RÉGLAGES : toggle, tonalités MARK/SPACE configurables, aide fréquences APRS-HF",
        "config.json : nouveaux champs hf_mode, hf_mark_hz, hf_space_hz",
    ]},
    {"version": "2.5.3", "date": "2026-06-02", "label": "", "changes": [
        "Alertes météo : popup + bip audio sur dépassement de seuils (temp, vent, rafales, pluie, WMO)",
        "Worker thread vérifiant Open-Meteo selon intervalle configurable",
        "Routes /weather_alert_config (GET/POST) et /weather_alert_test",
        "Seuils : température max/min, vitesse vent, rafales, précipitations, codes WMO sévères",
    ]},
        {"version": "2.5.2", "date": "2026-06-01", "label": "", "changes": [
        "Carte : suppression des tracés parasites mobiles — filtre distance > 300 km (rupture de traînée) et filtre vitesse implicite > 500 km/h",
    ]},
    {"version": "2.5.1", "date": "2026-06-01", "label": "", "changes": [
        "ISS : heure de fin de passage affichée (ex : 14:32 → 14:40) dans l'onglet ISS et dans les réglages",
        "Backend : champ set_fmt ajouté dans /iss_passes (heure locale de fin du passage)",
    ]},
    {"version": "2.5.0", "date": "2026-05-31", "label": "", "changes": [
        "Digipeater APRS logiciel : WIDEn-N, RELAY, WIDE1, aliases configurables",
        "Anti-dupli SHA1 (fenêtre 30 s), filtre anti-boucle IS, filtre propre station",
        "Section Digipeater dans ⚙️ RÉGLAGES : toggle, aliases, limite sauts, compteur",
        "Route /digi/status + polling JS temps réel (voyant vert / compteur session)",
        "Import hashlib ajouté, re aliasé globalement",
    ]},
    {"version": "2.4.9", "date": "2026-05-31", "label": "", "changes": [
        "Aide mise à jour : section 🔑 Sécurité (changement MDP, stockage hash, session)",
        "Aide mise à jour : QSO — bouton 🗑️ delete conversation documenté",
        "Aide mise à jour : MAP — OSM France mentionné",
        "Dépannage : nouvelle entrée 'Mot de passe oublié' avec commande werkzeug",
    ]},
    {"version": "2.4.8", "date": "2026-05-31", "label": "", "changes": [
        "Changement de mot de passe intégré dans l'onglet ⚙️ RÉGLAGES (section dédiée en bas)",
        "Validations : longueur min 6 car., confirmation, erreur mot de passe actuel incorrect",
    ]},
    {"version": "2.4.7", "date": "2026-05-31", "label": "", "changes": [
        "Carte mode nuit : retour CartoDB Dark Matter (Stadia nécessite une clé API)",
        "Mode jour : OSM France osmfr (labels français)",
    ]},
    {"version": "2.4.6", "date": "2026-05-31", "label": "", "changes": [
        "Carte en français : tuiles OSM France (tile.openstreetmap.fr/osmfr) en mode jour",
        "Labels français : noms de communes, routes, régions en français",
        "Mode nuit conserve le fond sombre CartoDB Dark",
    ]},
    {"version": "2.4.5", "date": "2026-05-31", "label": "", "changes": [
        "Fonction delete QSO : bouton 🗑️ dans le header chat pour supprimer une conversation",
        "Route API DELETE /chat/delete/<callsign> avec sauvegarde atomique de chat.json",
        "Bouton activé/désactivé automatiquement selon l'état du contact sélectionné",
    ]},
    {"version": "2.4.4", "date": "2026-05-29", "label": "", "changes": [
        "Widget météo discret entre header et moniteur de trafic (Open-Meteo, gratuit, sans clé API)",
        "Affiche : icône WMO, température, ressenti, description, vent (vitesse + direction), humidité, précipitations",
        "Position : coordonnées de la station (config) en priorité, géolocalisation navigateur en fallback",
        "Cache localStorage 15 min, rafraîchissement automatique, adaptatif mode jour/nuit",
    ]},
    {"version": "2.4.3", "date": "2026-05-29", "label": "", "changes": [
        "Ville la plus proche : badge 🏙️ affiché sur chaque trame de position (console trafic + popup carte)",
        "Route API GET /nearest_city?lat=&lon= : reverse geocoding via Nominatim (OSM), cache serveur LRU 2000 entrées",
        "Requêtes asynchrones — la carte et la console s'affichent immédiatement, la ville apparaît en différé",
    ]},
    {"version": "2.4.2", "date": "2026-05-29", "label": "", "changes": [
        "Mode Jour / Nuit : bouton ☀️/🌙 fixe en haut à droite, persisté via localStorage",
        "Carte Leaflet : fond sombre (CartoDB Dark) en mode nuit, OSM standard en mode jour",
        "Toutes les couleurs de l'interface (panneaux, textes, inputs, popups) s'adaptent au thème",
    ]},
    {"version": "2.4.1", "date": "2026-05-29", "label": "", "changes": [
        "Nouveau bouton '〰️ Trajets' sur la carte : efface uniquement les traînées (breadcrumbs) des mobiles sans supprimer les marqueurs",
        "Route API DELETE /map_clear_trails : remet à zéro le champ 'trail' dans stations_positions côté serveur",
        "Fonction JS mapClearTrails() : retire les polylines et points du trail de la carte Leaflet",
    ]},
    {"version": "2.4", "date": "2026-05-27", "label": "", "changes": [
        "Passage en version 2.4",
        "Mise à jour du changelog intégré",
    ]},
    {"version": "2.3", "date": "2026-05-26", "label": "", "changes": [
        "Passages ISS : moteur SGP4 embarqué (stdlib pure) — remplace open-notify.org hors-service",
        "TLE ISS rechargé toutes les 6h depuis CelesTrak (3 sources + cache disque 7j hors-ligne)",
        "Élévation maximale affichée pour chaque passage ISS dans le widget",
        "Fermeture propre (Graceful Shutdown) : SIGTERM/SIGINT + atexit",
        "Sauvegarde synchrone et atomique de chat.json et config.json à l'arrêt",
        "Fermeture ordonnée des sockets KISS TX, iGate et Wavelog",
    ]},
    {"version": "2.2", "date": "2025-05-25", "label": "", "changes": [
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
            logger.info("[QSO] %d conversation(s) restaurée(s) depuis %s",
                        len(self.conversations), CHAT_FILE)
        except Exception as e:
            logger.error("[QSO] Impossible de charger %s : %s", CHAT_FILE, e)

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
            logger.error("[QSO] Erreur sauvegarde %s : %s", CHAT_FILE, e)

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
                "dir":           "tx",
                "from":          config_manager.data.get("callsign", "?"),
                "text":          text,
                "ts":            time.strftime("%d/%m %H:%M"),
                "msgno":         msgno,
                "acked":         False,
                "acked_failed":  False,
                "retry_count":   0,
                "retry_at":      time.time() + 30,   # premier retry dans 30 s
            })
            self._save()

    def mark_ack(self, msgno):
        """Marque un message TX comme acquitté. Retourne (dest, msgno) si trouvé."""
        with self._lock:
            changed = False
            found_dest = None
            for dest, msgs in self.conversations.items():
                for m in msgs:
                    if m.get("msgno") == msgno and m["dir"] == "tx" and not m.get("acked"):
                        m["acked"]        = True
                        m["acked_failed"] = False
                        m["retry_at"]     = None   # stop retry
                        changed    = True
                        found_dest = dest
            if changed:
                self._save()
        return (found_dest, msgno) if changed else (None, None)

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

    def delete_conversation(self, callsign):
        """Supprime toute la conversation avec un indicatif. Retourne True si trouvé."""
        cs = callsign.upper()
        with self._lock:
            if cs not in self.conversations:
                return False
            del self.conversations[cs]
            self._save()
        return True

    # ── Retry ACK ─────────────────────────────────────────────────────────────

    MAX_RETRIES = 3          # nombre max de re-émissions
    RETRY_INTERVAL = 30.0    # secondes entre chaque retry

    def get_pending_retries(self):
        """Retourne les messages TX en attente de retry (retry_at <= now, non ackés).
        Appelé par le thread _ack_retry_worker."""
        now = time.time()
        pending = []
        with self._lock:
            for dest, msgs in self.conversations.items():
                for m in msgs:
                    if (m["dir"] == "tx"
                            and not m.get("acked")
                            and not m.get("acked_failed")
                            and m.get("msgno")
                            and m.get("retry_at") is not None
                            and m["retry_at"] <= now):
                        pending.append({
                            "dest":   dest,
                            "text":   m["text"],
                            "msgno":  m["msgno"],
                            "retry":  m.get("retry_count", 0),
                        })
        return pending

    def bump_retry(self, msgno):
        """Incrémente retry_count et planifie le prochain retry (ou marque échec)."""
        with self._lock:
            for msgs in self.conversations.values():
                for m in msgs:
                    if m.get("msgno") == msgno and m["dir"] == "tx":
                        cnt = m.get("retry_count", 0) + 1
                        m["retry_count"] = cnt
                        if cnt >= self.MAX_RETRIES:
                            m["acked_failed"] = True
                            m["retry_at"]     = None
                            logger.warning("[MSG] Abandon retry msgno=%s (%d essais)", msgno, cnt)
                        else:
                            m["retry_at"] = time.time() + self.RETRY_INTERVAL
                        break
            self._save()

chat_manager = APRSChat()


# ── Thread de retry ACK ───────────────────────────────────────────────────────
# Conforme APRS spec : re-émet les messages TX non acquittés jusqu'à 3 fois
# (intervalle 30 s). Marque acked_failed après MAX_RETRIES tentatives.

def _ack_retry_worker():
    """Vérifie toutes les 10 s les messages TX en attente d'ACK et les re-émet."""
    while True:
        time.sleep(10)
        try:
            pending = chat_manager.get_pending_retries()
            for job in pending:
                dest   = job["dest"]
                text   = job["text"]
                msgno  = job["msgno"]
                retry  = job["retry"]
                payload = ":" + dest.ljust(9) + ":" + text + "{" + msgno + "}"
                logger.info("[MSG] Retry %d/%d msgno=%s -> %s",
                            retry + 1, APRSChat.MAX_RETRIES, msgno, dest)
                try:
                    tx_queue.put_nowait({
                        "dest":      "APRS",
                        "payload":   payload,
                        "path":      None,
                        "aprs_type": "Message",
                        "extra":     {"comment": text, "msg_dest": dest, "_retry": retry + 1},
                    })
                except Exception:
                    pass
                chat_manager.bump_retry(msgno)
                # Diffuser l'état retry au frontend
                retry_event = {
                    "type":  "msg_retry",
                    "msgno": msgno,
                    "dest":  dest,
                    "retry": retry + 1,
                    "max":   APRSChat.MAX_RETRIES,
                    "failed": (retry + 1) >= APRSChat.MAX_RETRIES,
                }
                for _q in list(listeners):
                    try: _q.put_nowait(retry_event)
                    except Exception: pass
        except Exception as _e:
            logger.error("[ACK-RETRY] Erreur : %s", _e)


# LOGBOOK_REMOVED


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
        logger.info("[HW] init_hardware : PTT geree par Dire Wolf (cf. direwolf.conf)")

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
            logger.info("[TX] Nouvelle connexion KISS persistante établie")
        return cls._tx_sock

    def send_packet(self, dest, payload, custom_path=None, custom_src=None):
        """
        Envoie une trame APRS via Dire Wolf (port KISS TCP).
        Utilise une connexion TCP persistante — pas de reconnexion à chaque trame.
        custom_src : callsign source à substituer (retransmission digipeater).
        """
        import socket as _sock
        with self.tx_lock:
            # ── Encodage AX.25 (sans CRC — Dire Wolf l'ajoute) ───────────────
            def encode_call(call, last=False):
                parts = call.upper().split('-')
                base  = parts[0].strip('*').ljust(6)
                has_repeated = call.endswith('*')
                ssid  = int(parts[1].strip('*')) if len(parts) > 1 else 0
                res   = [(ord(c) << 1) for c in base]
                # bit 7 = H (has-been-repeated), bit 0 = end-of-address, bits 1-4 = SSID
                flags = (0x61 if last else 0x60) | (0x80 if has_repeated else 0x00)
                res.append((ssid << 1) | flags)
                return res

            source    = custom_src if custom_src else self.cfg.get('callsign', 'N0CALL')
            _hf_active = bool(self.cfg.get('hf_mode', False))
            path_str   = custom_path if custom_path else self.cfg.get('path', 'WIDE1-1,WIDE2-1')
            # En mode HF 300 bauds, les paths WIDE sont inopérants et inondent le réseau.
            # On force automatiquement un chemin vide (direct RF uniquement).
            if _hf_active and 'WIDE' in path_str.upper():
                logger.warning('[TX] hf_mode actif — path "%s" contient WIDE, forcé à vide (direct RF)', path_str)
                path_str = ''
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

            logger.debug("[TX] -> dest=%s payload=%s (%d octets AX.25)",
                dest, payload[:50], len(ax25))

            # ── Envoi via connexion persistante — 1 retry si socket morte ────
            for attempt in range(2):
                try:
                    s = APRSModem._get_tx_sock()
                    s.settimeout(4)
                    s.sendall(kiss_frame)
                    s.settimeout(None)
                    APRSModem.tx_last_ok    = time.strftime("%H:%M:%S")
                    APRSModem.tx_last_error = ""
                    logger.debug("[TX] Trame KISS envoyée OK (%d octets)", len(kiss_frame))
                    break
                except Exception as e:
                    # Forcer la fermeture pour provoquer une reconnexion au prochain appel
                    try: APRSModem._tx_sock.close()
                    except: pass
                    APRSModem._tx_sock = None
                    if attempt == 0:
                        logger.warning("[TX] Socket morte — reconnexion...")
                    else:
                        APRSModem.tx_last_error = str(e)
                        logger.error("[TX] ERREUR envoi KISS : %s", e)
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
        # Filtrer les caractères de padding non imprimables (espaces AX.25, bits parasites)
        call = "".join(c for c in call if c.isalnum() or c in '-')
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
        m = _re.search(r'(\d{4}\.\d+)\s*([NS])(.)(\d{5}\.\d+)\s*([EW])(.)', info)
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
                    # Décodage Base91 — spec APRS 1.0.1 §9
                    # Lat  : plage 90°  → diviseur 380926 (= 91^4 / 180)
                    # Lon  : plage 180° → diviseur 190463 (= 91^4 / 360)
                    lat_raw = ((ord(lat_c[0])-33)*753571 + (ord(lat_c[1])-33)*8281
                               + (ord(lat_c[2])-33)*91   + (ord(lat_c[3])-33))
                    lon_raw = ((ord(lon_c[0])-33)*753571 + (ord(lon_c[1])-33)*8281
                               + (ord(lon_c[2])-33)*91   + (ord(lon_c[3])-33))
                    lat_val = 90.0  - lat_raw / 380926.0
                    lon_val = -180.0 + lon_raw / 190463.0
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
                south = (d[3] == 'L')  # APRS 1.0.1 §10 : seul L = hémisphère Sud (P-Y=North, K/Z=ambiguity North)  # PATCH_MICE_SOUTH_v1
                if south: lat_dd = -lat_dd

                # Longitude : octets info[1..3]
                if len(info) < 4:
                    raise ValueError("info trop court pour longitude")
                lon_raw_deg = ord(info[1]) - 28
                lon_raw_min = ord(info[2]) - 28
                lon_raw_frc = ord(info[3]) - 28

                # Sanity check: minutes/centièmes hors plage → trame corrompue
                if lon_raw_min > 59 or lon_raw_frc > 99 or lon_raw_min < 0 or lon_raw_frc < 0:
                    raise ValueError("Mic-E lon min/frc hors plage: %d %d" % (lon_raw_min, lon_raw_frc))

                # Offset longitude : dest[4] in P-Y ou L → +100°
                # (couvre les plages 0°-9° et 100°-109°)
                # Réf. APRS 1.0.1 §10 Table 10-2
                if d[4] in ('P','Q','R','S','T','U','V','W','X','Y','L'):
                    lon_raw_deg += 100
                # Correction de plage (valeur après offset) :
                #   180-189 → -80  → donne 100-109°
                #   190-199 → -190 → donne 0-9°
                if lon_raw_deg >= 180 and lon_raw_deg <= 189:
                    lon_raw_deg -= 80
                elif lon_raw_deg >= 190 and lon_raw_deg <= 199:
                    lon_raw_deg -= 190
                lon_min = lon_raw_min + lon_raw_frc / 100.0
                lon_dd  = lon_raw_deg + lon_min / 60.0
                # Flag Ouest encodé dans dest[5] (PAS dest[4])
                # dest[4] = offset +100°, dest[5] = hémisphère Est/Ouest
                # Réf. APRS 1.0.1 §10 — erreur courante de confondre les deux
                west = d[5] in ('P','Q','R','S','T','U','V','W','X','Y','L')
                if west: lon_dd = -lon_dd

                # Sanity check finale : coordonnées dans les plages légales
                if lon_dd < -180.0 or lon_dd > 180.0:
                    raise ValueError("Mic-E lon hors plage: %.4f" % lon_dd)
                if lat_dd < -90.0 or lat_dd > 90.0:
                    raise ValueError("Mic-E lat hors plage: %.4f" % lat_dd)

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
                logger.warning("[MIC-E] Erreur décodage : %s", _mic_err)

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

    # ── Suspension Direwolf pour SSTV ────────────────────────────────────────
    # Setter cet event AVANT de démarrer SSTV pour que _rx_loop tue Direwolf
    # et attende. Le clearer à l'arrêt SSTV pour que Direwolf redémarre.
    _dw_pause = threading.Event()          # setté = Direwolf suspendu
    _current_dw_proc = None               # référence subprocess.Popen Direwolf
    _dw_proc_lock    = threading.Lock()   # protection de _current_dw_proc

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
            logger.debug("[RX] Device RX non configure -> default")
            return "default"

        dev_str = str(dev_cfg)

        # Cas 2 : déjà un nom ALSA direct (plughw:X,Y ou hw:X,Y)
        if dev_str.startswith("plughw:") or dev_str.startswith("hw:"):
            logger.debug("[RX] Device ALSA direct : %s", dev_str)
            return dev_str

        # Cas 3 : index entier PortAudio (ancien format) — résoudre vers ALSA
        try:
            idx = int(dev_str)
            devs = sd.query_devices()
            if idx < len(devs):
                pa_name = devs[idx]["name"]
                logger.debug("[RX] Résolution index PA %d (%s) -> ALSA", idx, pa_name)

                # Tentative via arecord -l
                try:
                    out = _sp.run(["arecord", "-l"], capture_output=True, text=True).stdout
                    keywords = [w for w in _re.split(r"[\s:,\-\(\)]+", pa_name) if len(w) >= 3]
                    for line in out.splitlines():
                        if any(kw.lower() in line.lower() for kw in keywords):
                            m = _re.search(r"card (\d+)[^,]*,\s*device (\d+)", line)
                            if m:
                                dev = "plughw:%s,%s" % (m.group(1), m.group(2))
                                logger.debug("[RX] Résolu : %s", dev)
                                return dev
                except Exception:
                    pass

                # Tentative : hw:X,Y dans le nom PA
                m = _re.search(r"hw:(\d+),(\d+)", pa_name)
                if m:
                    dev = "plughw:%s,%s" % (m.group(1), m.group(2))
                    logger.debug("[RX] Extrait du nom PA : %s", dev)
                    return dev

                logger.debug("[RX] Passage nom brut PA : %s", pa_name)
                return pa_name
        except (ValueError, Exception) as ex:
            logger.error("[RX] _find_alsa_device_name : %s", ex)

        # Cas 4 : string inconnue, on la passe directement
        logger.debug("[RX] Device brut : %s", dev_str)
        return dev_str

    def _write_direwolf_conf(self, alsa_dev, conf_path):
        """
        Genere direwolf.conf pour RX ET TX.
        Dire Wolf gere lui-meme la PTT et l'audio TX via KISS.

        Mode HF 300 bauds (hf_mode=True) :
          - Vitesse  : 300 baud (AFSK)
          - Tonalités: MARK 1600 Hz / SPACE 1800 Hz (standard AX.25 HF / APRS-HF)
          - Modulation: USB (radio réglée en USB, signal audio centré autour de 1700 Hz)
          - Directive Dire Wolf : MODEM 300 1600:1800
          - TXDELAY augmenté automatiquement à 500 ms minimum (radio HF plus lente à monter)
          - Compatible APRS-HF (10.151 MHz USB / 14.105 MHz USB / 144.800 MHz pour tests)
        """
        port     = self.cfg.get("serial_port", "").strip()
        ptt_mode = self.cfg.get("ptt_mode", "RTS").upper()
        hf_mode  = bool(self.cfg.get("hf_mode", False))

        # ── Sélection de la vitesse et des tonalités ──────────────────────────
        if hf_mode:
            mark_hz  = int(self.cfg.get("hf_mark_hz",  1600))
            space_hz = int(self.cfg.get("hf_space_hz", 1800))
            modem_line  = "MODEM 300 %d:%d" % (mark_hz, space_hz)
            # TXDELAY minimum 500 ms en HF (temps de montée de la radio + propagation)
            tx_delay = max(500, int(self.cfg.get("tx_delay_ms", 500)))
            logger.info("[DW] Mode HF 300 bauds — MARK %d Hz / SPACE %d Hz — TXDELAY %d ms",
                        mark_hz, space_hz, tx_delay)
        else:
            modem_line = "MODEM 1200"
            tx_delay   = max(100, int(self.cfg.get("tx_delay_ms", 300)))

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
            modem_line,
            ptt_line,
            "TXDELAY %d"   % tx_delay,
            "KISSPORT %d"  % APRSModem.DIREWOLF_KISS_PORT,
            "AGWPORT %d"   % APRSModem.DIREWOLF_AGW_PORT,
        ]
        with open(conf_path, "w") as f:
            f.write("\n".join(lines) + "\n")
        logger.debug("[DW] direwolf.conf:")
        for l in lines:
            logger.debug("[DW]   %s", l)

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
            logger.error("[KISS] Erreur parseur AX.25 : %s", ex)
            return None

    def _rx_loop(self):
        import subprocess, shutil, socket, os as _os, tempfile

        if not shutil.which('direwolf'):
            logger.critical("[RX] direwolf introuvable -- sudo apt install direwolf -y")
            APRSModem.rx_thread_alive = False
            return

        alsa_dev  = self._find_alsa_device_name()
        conf_path = _os.path.join(tempfile.gettempdir(), 'aprs_direwolf.conf')
        self._write_direwolf_conf(alsa_dev, conf_path)
        logger.info("[RX] Démarrage Dire Wolf sur %s (KISS TCP :%d)",
            alsa_dev, APRSModem.DIREWOLF_KISS_PORT)

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
                # Exposer la référence pour la suspension SSTV
                with APRSModem._dw_proc_lock:
                    APRSModem._current_dw_proc = dw_proc
                # Thread de capture stderr (erreurs audio, init device…)
                def _dw_stderr(proc):
                    for line in proc.stderr:
                        line = line.rstrip()
                        APRSModem.dw_log_lines.append("[ERR] " + line)
                        if line.strip():
                            logger.debug("[DW] %s", line.rstrip())
                threading.Thread(target=_dw_stderr, args=(dw_proc,), daemon=True, name="dw-stderr").start()
                # Thread de capture stdout (trames décodées, stats…)
                def _dw_stdout(proc):
                    for line in proc.stdout:
                        line = line.rstrip()
                        APRSModem.dw_log_lines.append(line)
                        if line.strip():
                            logger.debug("[DW] %s", line)
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
                    logger.error("[RX] Impossible de se connecter au port KISS de Dire Wolf")
                    raise RuntimeError("KISS port unreachable")

                sock.settimeout(1.0)
                APRSModem.rx_thread_alive = True
                logger.info("[RX] Connecté au port KISS -- en écoute...")

                # PATCH_MONITOR_FREEZE_267 — watchdog silence KISS
                KISS_SILENCE_TIMEOUT = 120   # secondes sans aucun octet → redémarrage
                _last_data_ts = time.time()

                buf = bytearray()
                while self.is_rx_running:
                    # ── Niveau audio : estimation via sounddevice ─────────────
                    # (Dire Wolf gère l'audio ; on mesure à part si possible)
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        # Watchdog : si silence trop long, forcer redémarrage Direwolf
                        if time.time() - _last_data_ts > KISS_SILENCE_TIMEOUT:
                            logger.warning(
                                "[RX] Watchdog KISS : aucun octet depuis %ds — redémarrage Direwolf",
                                KISS_SILENCE_TIMEOUT,
                            )
                            break
                        # Pas de donnée, on continue la boucle
                        continue
                    except OSError:
                        break

                    if not data:
                        break

                    _last_data_ts = time.time()

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
                            try:
                                APRSModem.rx_queue.put_nowait(frame)
                            except queue.Full:
                                logger.warning("[KISS] rx_queue pleine — trame ignorée (broadcaster mort ?)")
                            logger.debug("[KISS] Trame RX : %s>%s %s",
                                frame.get("src","?"), frame.get("dest","?"),
                                frame.get("aprs_type","?"))
                        else:
                            logger.warning("[KISS] Trame AX.25 non parsée (%d octets)", len(ax25_bytes))

            except Exception as e:
                logger.error("[RX] Erreur pipeline Dire Wolf : %s", e)
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
                # Effacer la référence process maintenant qu'il est mort
                with APRSModem._dw_proc_lock:
                    if APRSModem._current_dw_proc is dw_proc:
                        APRSModem._current_dw_proc = None

            if self.is_rx_running:
                # ── Vérifier si SSTV a demandé la suspension ─────────────────
                logger.warning("[RX] Pipeline interrompu -- redémarrage dans 3 s...")
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

    # ── Source 1b : /proc/asound/cards — cartes même si "busy" (tenues par Dire Wolf) ──
    # arecord -l peut omettre une carte si son device 0 est exclusivement ouvert.
    # /proc/asound/cards liste TOUTES les cartes présentes, quel que soit leur état.
    try:
        with open("/proc/asound/cards", "r") as _fac:
            for line in _fac:
                # Format : " N [ID           ]: driver - Nom long"
                mp = _re.match(r'\s*(\d+)\s+\[(\S+)\s*\]:\s*\S+\s+-\s+(.+)', line)
                if not mp:
                    continue
                card_n   = mp.group(1)
                card_id  = mp.group(2)
                card_long = mp.group(3).strip()
                # Scanner tous les devices PCM disponibles sur cette carte
                # (pas seulement device 0 -- le MicroHAM et certains USB audio
                #  exposent leurs PCM sur device 1, 2, etc.)
                card_dir = "/proc/asound/card%s" % card_n
                pcm_devs_found = set()
                if os.path.isdir(card_dir):
                    for entry in os.listdir(card_dir):
                        # Les entrees PCM sont nommees pcm0p, pcm0c, pcm1p, pcm1c...
                        m_pcm = _re.match(r"pcm(\d+)([pc])", entry)
                        if m_pcm:
                            pcm_devs_found.add(m_pcm.group(1))
                # Si aucun PCM trouve via /proc, essayer 0 par defaut
                if not pcm_devs_found:
                    pcm_devs_found = {"0"}
                for dev_n in sorted(pcm_devs_found, key=int):
                    hw_key = "plughw:%s,%s" % (card_n, dev_n)
                    if hw_key in seen_hw:
                        continue
                    pcm_path   = "/proc/asound/card%s/pcm%sp" % (card_n, dev_n)
                    pcm_c_path = "/proc/asound/card%s/pcm%sc" % (card_n, dev_n)
                    has_out = os.path.exists(pcm_path)
                    has_in  = os.path.exists(pcm_c_path)
                    if not (has_in or has_out):
                        if not os.path.isdir(card_dir):
                            continue
                        has_in = has_out = True
                    else:
                        # Forcer has_in=True : le device peut etre busy (Dire Wolf)
                        # mais il est physiquement disponible
                        has_in = True
                    # Label : distinguer les devices si plusieurs PCM sur la meme carte
                    if len(pcm_devs_found) > 1:
                        label = "%s — device %s (plughw:%s,%s)" % (card_long, dev_n, card_n, dev_n)
                    else:
                        label = "%s (card %s)" % (card_long, card_n)
                    seen_hw.add(hw_key)
                    devices.append({
                        "id":   hw_key,
                        "name": label,
                        "hw":   hw_key,
                        "in":   has_in,
                        "out":  has_out,
                    })
    except Exception:
        pass

    # ── Source 2 : PortAudio (fallback ou devices supplémentaires) ────────────
    pa_devices = []
    try:
        dev_list = sd.query_devices()
        for i, d in enumerate(dev_list):
            if d['max_output_channels'] > 0 or d['max_input_channels'] > 0:
                pa_name = d['name']
                # Mots génériques à ignorer dans la comparaison (évite les faux matchs
                # sur "USB", "Audio", "CODEC" qui sont communs à des devices différents)
                _GENERIC = {"usb", "audio", "codec", "device", "input", "output",
                            "interface", "sound", "card", "default", "plug", "alsa"}
                def _sig_words(s):
                    return {w.lower() for w in s.split() if len(w) >= 4 and w.lower() not in _GENERIC}
                pa_sig = _sig_words(pa_name)
                # Deduplication par mots significatifs communs.
                # Si pa_sig est vide (nom full-generique ex. "USB Audio CODEC"),
                # on compare le nom complet normalise pour eviter les faux doublons
                # mais on NE SUPPRIME PAS le device -- un nom generique peut correspondre
                # a une carte completement differente (MicroHAM, SignaLink, etc.)
                already = False
                if pa_sig:
                    for dev in devices:
                        dev_sig = _sig_words(dev["name"])
                        if dev_sig and pa_sig & dev_sig:
                            already = True
                            break
                # Cas nom full-generique : dedup uniquement si nom EXACT deja vu
                # (evite de supprimer deux cartes "USB Audio CODEC" differentes)
                # -> on ne deduplique pas, on laisse passer avec son index PA
                if not already:
                    pa_devices.append({
                        "id":   i,
                        "name": pa_name + " (PA:%d)" % i,
                        "hw":   None,
                        "in":   d["max_input_channels"] > 0,
                        "out":  d["max_output_channels"] > 0,
                    })
    except Exception:
        pass

    all_devices = devices + pa_devices

    # ── Source 3 : PulseAudio / PipeWire — sources monitor ───────────────────
    # Ces sources capturent la sortie audio du système (loopback logiciel),
    # ce qui permet a SSTV de s'accrocher au meme flux qu'APRS/Dire Wolf.
    try:
        import subprocess as _sp2
        out = _sp2.run(['pactl', 'list', 'sources', 'short'],
                       capture_output=True, text=True, timeout=2).stdout
        for line in out.splitlines():
            parts = line.split('\t')
            if len(parts) < 2:
                continue
            src_name = parts[1].strip()
            if '.monitor' in src_name or 'monitor' in src_name.lower():
                label = "Loopback Monitor : " + src_name
                if not any(d['id'] == src_name for d in all_devices):
                    all_devices.append({
                        "id":    src_name,
                        "name":  label,
                        "hw":    None,
                        "in":    True,
                        "out":   False,
                        "monitor": True,
                    })
    except Exception:
        pass

    # ── Source 4 : ALSA loopback snd-aloop ───────────────────────────────────
    try:
        import subprocess as _sp3
        aloop_out = _sp3.run(['arecord', '-l'], capture_output=True, text=True).stdout
        for line in aloop_out.splitlines():
            if 'Loopback' in line or 'loopback' in line:
                m2 = _re.search(r'card (\d+).*device (\d+)', line)
                if m2:
                    c, d2 = m2.group(1), m2.group(2)
                    hw_key = "plughw:%s,%s" % (c, d2)
                    if not any(dev['id'] == hw_key for dev in all_devices):
                        all_devices.append({
                            "id":    hw_key,
                            "name":  "ALSA Loopback : " + hw_key,
                            "hw":    hw_key,
                            "in":    True,
                            "out":   False,
                            "loopback": True,
                        })
    except Exception:
        pass

    # S'assurer que le device actuellement configure est toujours present
    # meme si ALSA ne le voit plus (Dire Wolf le tient) -- on l'ajoute depuis config
    cfg_rx   = config_manager.data.get('audio_device_rx')
    cfg_tx   = config_manager.data.get('audio_device_tx')
    for cfg_val in set([cfg_rx, cfg_tx]):
        if cfg_val is None:
            continue
        if not any(str(d['id']) == str(cfg_val) for d in all_devices):
            all_devices.insert(0, {
                "id":   cfg_val,
                "name": "config sauvegardee : %s" % cfg_val,
                "hw":   None, "in": True, "out": True,
            })

    return all_devices


@app.route('/audio_devices')
@_login_required
def audio_devices_route():
    """Retourne la liste des devices audio (appelable par AJAX pour refresh)."""
    return jsonify(_enum_audio_devices())


@app.route('/')
@_login_required
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

    _cfg_json = json.dumps({
        'maidenhead': config_manager.data.get('maidenhead', ''),
        'geo_mode':   config_manager.data.get('geo_mode',   'locator'),
        'lat_manual': config_manager.data.get('lat_manual', ''),
        'lon_manual': config_manager.data.get('lon_manual', ''),
    })
    _callsign_json = json.dumps(config_manager.data.get('callsign', 'N0CALL'))
    return ("""<!DOCTYPE html>
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
        @keyframes wxBadgePulse {
            0%,100% { opacity:.75; box-shadow:0 0 4px #f59e0b44; }
            50%      { opacity:1;   box-shadow:0 0 10px #f59e0b88; }
        }
        @keyframes propBadgePulse {
            0%,100% { opacity:.80; box-shadow:0 0 5px #ef444455; }
            50%      { opacity:1;   box-shadow:0 0 12px #ef4444aa; }
        }
        @keyframes propPillPulse {
            0%,100% { box-shadow:0 0 4px #ef444433; }
            50%      { box-shadow:0 0 10px #ef4444cc; }
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
            /* Tooltip waypoints trail */
            .aprs-trail-tip {
                background: rgba(15,23,42,0.92) !important;
                border: 1px solid #334155 !important;
                border-radius: 5px !important;
                color: #94a3b8 !important;
                font-size: 10px !important;
                font-family: monospace !important;
                padding: 2px 6px !important;
                box-shadow: 0 2px 8px #0006 !important;
                white-space: nowrap !important;
            }
            .aprs-trail-tip::before { display: none !important; }
            /* Animation entrée nouvelle trame */
            @keyframes aprsCardIn {
                from { opacity: 0; transform: translateY(-6px) scale(.99); }
                to   { opacity: 1; transform: translateY(0)    scale(1); }
            }
            /* Hover sur carte trame */
            .aprs-card:hover { background: rgba(15,23,42,0.75) !important; }
            /* Bouton trame brute : override min-height mobile */
            .aprs-card button { min-height: unset !important; }
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
            input[type=text]:not(.modal-input), input[type=number], input[type=password],
            input[type=email], textarea, select:not(.modal-select) {
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

            /* ── Cartes trafic APRS : lisibilité mobile ───────── */
            /* Conteneur console : padding réduit */
            #console { padding: 8px !important; }
            /* En-tête de trame : empilé en 2 lignes propres */
            .aprs-card { padding: 10px 11px !important; }
            .aprs-card-hdr {
                flex-direction: column !important;
                align-items: flex-start !important;
                gap: 5px !important;
            }
            .aprs-card-hdr-left {
                width: 100%;
                display: flex;
                align-items: center;
                gap: 6px;
                flex-wrap: wrap;
            }
            .aprs-card-hdr-right {
                width: 100%;
                display: flex;
                align-items: center;
                gap: 6px;
                justify-content: space-between;
            }
            /* Indicatif plus lisible */
            .aprs-cs-link { font-size: 13px !important; padding: 3px 8px !important; }
            /* Badges direction TX/RX/IS plus grands */
            .aprs-dir-badge { font-size: 10px !important; padding: 3px 9px !important; }
            /* Badge type APRS lisible */
            .aprs-type-badge { font-size: 10px !important; padding: 3px 8px !important; }
            /* Heure en gris discret */
            .aprs-time { font-size: 10px !important; color: #475569; margin-left: auto; }
            /* Masquer dest/path sur très petit écran */
            .aprs-dest-path { display: none !important; }
            /* Champs décodés : badges plus grands */
            .aprs-fields { margin-top: 8px !important; gap: 5px !important; }
            .aprs-fields span[style*="font-size:10px"],
            .aprs-fields span[style*="font-size:9px"] {
                font-size: 11px !important;
                padding: 3px 8px !important;
            }
            /* Bouton trame brute */
            .raw-toggle { font-size: 10px !important; padding: 4px 0 !important; }

            /* ── TRAFIC mobile-first : console en premier ─────────────── */
            /* Reorder : console (col-9) avant panneau (col-3)             */
            #traffic-console-col  { order: -1; }
            #traffic-sidebar-col  { order:  1; }
            /* Console plein écran sans sidebar au-dessus                  */
            #traffic-console-col .glass { height: calc(100vh - 180px) !important; min-height: 320px; }
            /* Header console sticky sur mobile                            */
            #console-sticky-hdr   { position: sticky; top: 0; z-index: 20;
                                    background: rgba(9,16,32,0.98);
                                    backdrop-filter: blur(10px);
                                    border-bottom: 1px solid #1e293b; }
            /* Cartes APRS ultra-compactes                                 */
            .aprs-card {
                padding: 8px 10px !important;
                border-radius: 9px !important;
            }
            /* Header carte : indicatif + badge direction sur une ligne,
               type + heure sur la deuxième                                */
            .aprs-card-hdr {
                flex-direction: column !important;
                align-items: flex-start !important;
                gap: 3px !important;
            }
            .aprs-card-hdr-left {
                width: 100%;
                display: flex;
                align-items: center;
                gap: 5px;
                flex-wrap: nowrap;
                overflow: hidden;
            }
            .aprs-card-hdr-right {
                width: 100%;
                display: flex;
                align-items: center;
                gap: 5px;
                justify-content: space-between;
            }
            .aprs-cs-link  { font-size: 12px !important; padding: 2px 7px !important; }
            .aprs-dir-badge { font-size: 9px !important; padding: 2px 7px !important; flex-shrink: 0; }
            .aprs-type-badge { font-size: 9px !important; padding: 2px 6px !important; }
            .aprs-time  { font-size: 9px !important; color: #475569; margin-left: auto; flex-shrink: 0; }
            .aprs-dest-path { display: none !important; }
            .aprs-fields { margin-top: 6px !important; gap: 4px !important; flex-wrap: wrap; }
            .aprs-fields span { font-size: 10px !important; padding: 2px 6px !important; }
            /* Panneau gauche : réduit avec accordéon                      */
            #traffic-sidebar-toggle { display: flex; }
            #traffic-sidebar-body   { display: none; }
            #traffic-sidebar-body.open { display: block; }
            /* Barre filtre/compteur intégrée dans le sticky header        */
            #monitor-filter-row { flex-wrap: wrap; gap: 5px; }
        }
        @media (min-width: 768px) {
            #mobile-nav { display: none !important; }
        }

        /* ══════════════════════════════════════════════════════
           MODE JOUR / NUIT  —  DAYNIGHT_PATCH_APPLIED
           ══════════════════════════════════════════════════════ */
        /* -- Bouton toggle -- */
        #daynight-btn {
            position: fixed; top: 12px; right: 16px; z-index: 2000;
            background: rgba(30,41,59,.85); border: 1px solid #334155;
            border-radius: 50%; width: 38px; height: 38px;
            display: flex; align-items: center; justify-content: center;
            cursor: pointer; font-size: 18px; line-height: 1;
            box-shadow: 0 2px 10px #0005; transition: background .2s, border-color .2s;
        }
        #daynight-btn:hover { background: rgba(51,65,85,.95); }

        /* ── Palette "Brume" — bleu-gris très pâle, reposant ────────────────
           Fond global    : #eceff5  (brume bleue très pâle)
           Fond panneaux  : #f4f6fa  (blanc bleuté)
           Fond inputs    : #f8f9fc  (blanc quasi pur, teinté bleu)
           Bordures       : #d4d9e6  (gris-bleu doux)
           Texte principal: #242c3a  (ardoise foncée, jamais noir)
           Texte secondaire: #6a7490 (gris-bleu moyen)
           Texte tertiaire : #8a92a8 (gris-bleu clair)
           Accent bleu     : #6a9acc (bleu acier pastel)
           Accent vert     : #5a9e78 (vert doux)
           Accent violet   : #8878b8 (indigo poudré)
        ── */
        /* -- Mode jour (classe .day-mode sur <body>) -- */
        body.day-mode {
            background-color: #eceff5 !important;
            color: #242c3a !important;
        }
        body.day-mode #daynight-btn {
            background: rgba(244,246,250,0.96);
            border-color: #c8cedc;
            box-shadow: 0 2px 8px rgba(36,44,58,.08);
        }
        /* Panneaux glass */
        body.day-mode .glass {
            background: rgba(244,246,250,0.86) !important;
            border-color: rgba(200,206,220,0.55) !important;
            backdrop-filter: blur(12px);
        }
        /* Fond principal des cartes/sections */
        body.day-mode .bg-slate-900,
        body.day-mode .bg-slate-900\/80,
        body.day-mode [class*="bg-slate-900"] {
            background-color: #f4f6fa !important;
        }
        body.day-mode .bg-slate-800,
        body.day-mode [class*="bg-slate-800"] {
            background-color: #eceff5 !important;
        }
        body.day-mode .bg-\[\#020617\],
        body.day-mode .bg-\[#020617\] {
            background-color: #eceff5 !important;
        }
        body.day-mode .bg-\[#0f172a\],
        body.day-mode .bg-\[#0f172a\]\/80 {
            background-color: #f4f6fa !important;
        }
        /* Textes — ardoise douce, jamais noir pur */
        body.day-mode .text-slate-300 { color: #54607a !important; }
        body.day-mode .text-slate-400 { color: #6a7490 !important; }
        body.day-mode .text-slate-500 { color: #8a92a8 !important; }
        body.day-mode .text-white     { color: #242c3a !important; }
        body.day-mode .text-slate-200 { color: #2e3850 !important; }
        /* Accents désaturés et froids */
        body.day-mode .text-blue-400  { color: #6a9acc !important; }
        body.day-mode .text-blue-500  { color: #5280b8 !important; }
        body.day-mode .text-emerald-400 { color: #5a9e78 !important; }
        body.day-mode .text-violet-400  { color: #8878b8 !important; }
        /* Bordures légères */
        body.day-mode .border-slate-800 { border-color: #d4d9e6 !important; }
        body.day-mode .border-slate-700 { border-color: #c8cedc !important; }
        body.day-mode .border-\[#1e3a5f\] { border-color: #bacad8 !important; }
        /* Inputs */
        body.day-mode input, body.day-mode select, body.day-mode textarea {
            background-color: #f8f9fc !important;
            color: #242c3a !important;
            border-color: #c8cedc !important;
        }
        body.day-mode input:focus, body.day-mode select:focus, body.day-mode textarea:focus {
            border-color: #8878b8 !important;
            box-shadow: 0 0 0 2px rgba(136,120,184,.15) !important;
            outline: none !important;
        }
        /* Console trafic */
        body.day-mode .rx-entry {
            background: rgba(244,246,250,0.90) !important;
            border-left-color: #6a9acc !important;
        }
        body.day-mode .aprs-card:hover {
            background: rgba(232,236,244,0.78) !important;
        }
        /* Bottom nav mobile */
        body.day-mode #mobile-nav {
            background: rgba(244,246,250,0.97) !important;
            border-top-color: #d4d9e6 !important;
        }
        body.day-mode #mobile-nav button { color: #8a92a8; }
        body.day-mode #mobile-nav button.active { color: #5280b8; background: rgba(82,128,184,.10); }
        /* Leaflet popups */
        body.day-mode .leaflet-popup-content-wrapper {
            background: #f4f6fa !important;
            color: #242c3a !important;
            border-color: #c8cedc !important;
        }
        body.day-mode .leaflet-popup-tip { background: #f4f6fa !important; }
        /* Marqueurs labels */
        body.day-mode .aprs-marker-label {
            background: rgba(244,246,250,0.93) !important;
            color: #3e5882 !important;
            border-color: #bacad8 !important;
        }
        /* Trail tooltip */
        body.day-mode .aprs-trail-tip {
            background: rgba(244,246,250,0.97) !important;
            border-color: #c8cedc !important;
            color: #54607a !important;
        }
        /* Nav desktop */
        body.day-mode nav.desktop-nav {
            background: rgba(236,239,245,0.90) !important;
            border-color: #d4d9e6 !important;
        }
        /* Scrollbar */
        body.day-mode .custom-scrollbar::-webkit-scrollbar-thumb {
            background: #c8cedc;
        }
        /* ── Boutons — texte lisible en mode jour ──────────────────────────── */
        /* Nav desktop : inactifs */
        body.day-mode nav.desktop-nav button {
            color: #2e3850 !important;
        }
        /* Nav desktop : survol */
        body.day-mode nav.desktop-nav button:hover {
            color: #0f1a2e !important;
            background: rgba(82,128,184,.12) !important;
        }
        /* Nav desktop : actif (bg-blue-600) */
        body.day-mode nav.desktop-nav button.bg-blue-600 {
            background: #3a6aaa !important;
            color: #ffffff !important;
        }
        /* Tous les boutons génériques dans les panneaux */
        body.day-mode button {
            color: #2e3850 !important;
        }
        /* Boutons action (texte gris slate) */
        body.day-mode .text-slate-600 { color: #3a4460 !important; }
        body.day-mode .hover\:text-slate-200:hover { color: #0f1a2e !important; }
        body.day-mode .hover\:text-red-400:hover   { color: #b83232 !important; }
        body.day-mode .hover\:text-blue-400:hover  { color: #3a6aaa !important; }
        body.day-mode .hover\:text-orange-400:hover{ color: #b86a1a !important; }
        body.day-mode .hover\:text-emerald-300:hover{ color: #2a7a50 !important; }
        body.day-mode .hover\:text-amber-300:hover { color: #a06010 !important; }
        body.day-mode .hover\:text-blue-300:hover  { color: #2a5a9a !important; }
        body.day-mode .hover\:text-violet-300:hover{ color: #5a42a8 !important; }
        body.day-mode .hover\:text-yellow-300:hover{ color: #906800 !important; }
        /* Boutons mobiles */
        body.day-mode #mobile-nav button { color: #6a7490 !important; }
        body.day-mode #mobile-nav button.active { color: #3a6aaa !important; }
        /* Login card */
        body.day-mode .card {
            background: #f4f6fa !important;
            border-color: #c8cedc !important;
        }
    </style>
<script>
    /* ── Mode Jour / Nuit ─────────────────────────────────────────────────── */
    (function() {
        var _DN_KEY = 'aprs_daymode';
        function _applyTheme(day) {
            if (day) {
                document.body.classList.add('day-mode');
            } else {
                document.body.classList.remove('day-mode');
            }
            var btn = document.getElementById('daynight-btn');
            if (btn) btn.textContent = day ? '🌙' : '☀️';
            btn && (btn.title = day ? 'Passer en mode nuit' : 'Passer en mode jour');
            // Changer le fond de la carte Leaflet si elle est initialisée
            if (typeof _map !== 'undefined' && _map) {
                _map.eachLayer(function(l) {
                    if (l._url) _map.removeLayer(l);
                });
                var tileUrl = day
                    ? 'https://{s}.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png'
                    : 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png';
                L.tileLayer(tileUrl, { maxZoom: 20, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributeurs' }).addTo(_map);
            }
        }
        window._isDayMode = function() {
            return document.body.classList.contains('day-mode');
        };
        window.toggleDayNight = function() {
            var day = !_isDayMode();
            localStorage.setItem(_DN_KEY, day ? '1' : '0');
            _applyTheme(day);
        };
        // Appliquer au chargement (avant render pour éviter le flash)
        document.addEventListener('DOMContentLoaded', function() {
            var saved = localStorage.getItem(_DN_KEY);
            _applyTheme(saved === '1');
        });
    })();

    function switchTab(tabId) {
        ['terminal', 'config', 'qso', 'map', 'iss', 'sstv', 'stats', 'notes'].forEach(function(t) {
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
        if (tabId === 'qso') setTimeout(function(){ if(typeof refreshContacts==='function') refreshContacts(); }, 50);
        if (tabId === 'iss') {
            var iframe = document.getElementById('iss-iframe');
            if (iframe && iframe.src === 'about:blank') iframe.src = iframe.dataset.src;
            // Rafraîchir le bandeau prochain passage à chaque ouverture de l'onglet ISS
            if (typeof issPassRefresh === 'function') issPassRefresh();
            if (typeof iss24hRefresh === 'function') iss24hRefresh();
        }
        document.dispatchEvent(new CustomEvent('aprs-switchtab', {detail: tabId}));
        /* Scroll top sur mobile */
        if (window.innerWidth < 768) window.scrollTo({top: 0, behavior: 'smooth'});
    }
</script>
</head>
<body class="bg-[#020617] text-slate-300 min-h-screen font-sans">
<!-- ── Bouton Mode Jour / Nuit ── DAYNIGHT_PATCH_APPLIED -->
<button id="daynight-btn" onclick="toggleDayNight()" title="Passer en mode jour">☀️</button>
<div class="max-w-6xl mx-auto p-4 lg:p-8">

    <header class="flex flex-col lg:flex-row items-center justify-between mb-4 lg:mb-10 gap-2 lg:gap-6">
        <div class="flex items-center gap-5">
            <a href="/" onclick="location.reload(); return false;" title="Rafraîchir la page"
               class="bg-gradient-to-br from-blue-600 to-indigo-700 p-4 rounded-2xl shadow-xl hover:from-blue-500 hover:to-indigo-600 transition-all cursor-pointer">
                <i class="fas fa-broadcast-tower text-3xl text-white"></i>
            </a>
            <div>
                <h1 class="text-3xl font-black tracking-tight text-white italic">Py-APRS <span id="app-version-badge" class="text-blue-500 text-sm not-italic font-medium ml-2 cursor-pointer hover:text-blue-300 transition-colors" onclick="document.getElementById('modal-aide').classList.remove('hidden');aideShowTab('changelog')" title="Voir le changelog">v2.6.9</span></h1>
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
                <!-- ── Badge alertes météo (à côté du PTT) ── -->
                <div id="wx-alert-badge" title=""
                     style="display:none;align-items:center;gap:4px;padding:3px 9px;border-radius:10px;
                            background:rgba(120,53,15,0.55);border:1px solid #f59e0b55;
                            cursor:pointer;transition:all .3s"
                     onclick="_wxAlertBadgeClick()">
                    <span id="wx-alert-badge-icon" style="font-size:14px;line-height:1">⚠️</span>
                    <span id="wx-alert-badge-txt"  style="font-size:9px;font-weight:900;color:#fcd34d;
                           letter-spacing:.12em;text-transform:uppercase;white-space:nowrap"></span>
                </div>
                <!-- Badge alertes propagation / blackout HF -->
                <div id="prop-alert-badge" title=""
                     style="display:none;align-items:center;gap:4px;padding:3px 9px;border-radius:10px;background:rgba(120,20,20,0.6);border:1px solid #ef444455;cursor:pointer;transition:all .3s"
                     onclick="_propAlertBadgeClick()">
                    <span id="prop-alert-badge-icon" style="font-size:14px;line-height:1">📶</span>
                    <span id="prop-alert-badge-txt"  style="font-size:9px;font-weight:900;color:#fca5a5;letter-spacing:.12em;text-transform:uppercase;white-space:nowrap"></span>
                </div>
                <!-- Pill propagation discrète (toujours visible) -->
                <div id="prop-status-pill"
                     title="Indices de propagation HF — Kp / SFI / X-ray"
                     style="display:flex;align-items:center;gap:6px;padding:3px 8px;border-radius:10px;
                            background:rgba(15,23,42,0.7);border:1px solid #1e293b;
                            cursor:default;transition:border-color .4s,box-shadow .4s">
                    <span id="prop-pill-kp"
                          style="font-size:9px;font-weight:800;font-family:monospace;
                                 color:#64748b;letter-spacing:.05em;white-space:nowrap;
                                 transition:color .3s">🧲 –</span>
                    <span style="color:#1e293b;font-size:9px">·</span>
                    <span id="prop-pill-sfi"
                          style="font-size:9px;font-weight:800;font-family:monospace;
                                 color:#64748b;letter-spacing:.05em;white-space:nowrap;
                                 transition:color .3s">📻 –</span>
                    <span style="color:#1e293b;font-size:9px">·</span>
                    <span id="prop-pill-xray"
                          style="font-size:9px;font-weight:800;font-family:monospace;
                                 color:#64748b;letter-spacing:.05em;white-space:nowrap;
                                 transition:color .3s">☀️ –</span>
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
            <button id="btn-sstv"     onclick="switchTab('sstv')"     class="px-6 py-2.5 rounded-xl font-bold transition-all text-slate-500 hover:text-slate-200">📷 SSTV</button>
            <button id="btn-stats"    onclick="switchTab('stats')"    class="px-6 py-2.5 rounded-xl font-bold transition-all text-slate-500 hover:text-slate-200">📊 STATS</button>
            <button id="btn-notes"    onclick="switchTab('notes')"    class="px-6 py-2.5 rounded-xl font-bold transition-all text-slate-500 hover:text-slate-200">📝 NOTES</button>
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
        <button id="mnav-sstv" onclick="switchTab('sstv')">
            <span class="nav-icon">📷</span>
            <span>SSTV</span>
        </button>
        <button id="mnav-stats" onclick="switchTab('stats')">
            <span class="nav-icon">📊</span>
            <span>STATS</span>
        </button>
        <button id="mnav-notes" onclick="switchTab('notes')">
            <span class="nav-icon">📝</span>
            <span>NOTES</span>
        </button>
        <button onclick="document.getElementById('modal-aide').classList.remove('hidden')">
            <span class="nav-icon">❓</span>
            <span>AIDE</span>
        </button>
    </nav>

    <!-- ── Météo discrète ── WEATHER_PATCH_APPLIED ──────────────────────────────
         Données : Open-Meteo (gratuit, sans clé API)
         Position: coordonnées de la station (config) ou géolocalisation navigateur
    ──────────────────────────────────────────────────────────────────────────── -->
    <div id="weather-bar" style="
        display:flex; align-items:center; gap:10px; flex-wrap:wrap;
        background:rgba(15,23,42,0.45); border:1px solid rgba(148,163,184,0.08);
        border-radius:1rem; padding:8px 18px; margin-bottom:18px;
        font-size:11px; color:#94a3b8; backdrop-filter:blur(8px);
        min-height:36px; transition:opacity .3s;
    ">
        <span id="wx-icon"  style="font-size:18px;line-height:1">⏳</span>
        <span id="wx-temp"  style="font-weight:700;color:#e2e8f0;font-size:13px"></span>
        <span id="wx-feels" style="color:#64748b;font-size:10px"></span>
        <span id="wx-sep1"  style="color:#334155">│</span>
        <span id="wx-desc"  style="color:#94a3b8;font-style:italic"></span>
        <span id="wx-sep2"  style="color:#334155">│</span>
        <span id="wx-wind"  style="color:#7dd3fc"></span>
        <span id="wx-sep3"  style="color:#334155">│</span>
        <span id="wx-hum"   style="color:#86efac"></span>
        <span id="wx-sep4"  style="color:#334155">│</span>
        <span id="wx-rain"  style="color:#93c5fd"></span>
        <span id="wx-loc"   style="margin-left:auto;color:#475569;font-size:9px;white-space:nowrap;cursor:pointer" onclick="_wxRefresh()" title="Actualiser">📍 <span id="wx-loc-name">…</span></span>
    </div>

    <main>
        <!-- ═══════════════════════════════ TRAFIC ═══════════════════════════════ -->
        <div id="tab-terminal" class="grid grid-cols-1 lg:grid-cols-12 gap-8">
            <div class="lg:col-span-3 space-y-6" id="traffic-sidebar-col">
                <!-- Bouton accordéon mobile (masqué desktop) -->
                <button id="traffic-sidebar-toggle"
                        onclick="(function(){var b=document.getElementById('traffic-sidebar-body');b.classList.toggle('open');this.querySelector('.tsb-arrow').textContent=b.classList.contains('open')?'▴':'▾'}).call(this)"
                        style="display:none;width:100%;background:rgba(15,23,42,0.6);border:1px solid #1e293b;border-radius:12px;padding:10px 14px;color:#94a3b8;font-size:12px;font-weight:700;text-align:left;cursor:pointer;align-items:center;gap:8px;justify-content:space-between;">
                    <span>📤 Envoyer / Statut station</span>
                    <span class="tsb-arrow" style="color:#475569;font-size:14px;">▾</span>
                </button>
                <div id="traffic-sidebar-body">
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
                            """ + (('<button onclick="sendText1(this)" class="bg-slate-800/50 hover:bg-emerald-600/30 border border-slate-700 p-3 rounded-xl text-xs font-bold transition-all flex items-center justify-center gap-2">📝 ' + (config_manager.data.get('beacon_text1') or '')[:32] + '</button>') if config_manager.data.get('beacon_text1', '').strip() else '') + """
                            """ + (('<button onclick="sendText2(this)" class="bg-slate-800/50 hover:bg-teal-600/30 border border-slate-700 p-3 rounded-xl text-xs font-bold transition-all flex items-center justify-center gap-2">📝 ' + (config_manager.data.get('beacon_text2') or '')[:32] + '</button>') if config_manager.data.get('beacon_text2', '').strip() else '') + """
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
                                <!-- Texte 1 -->
                                <div id="badge-text1" class="flex items-center justify-between px-2.5 py-1.5 rounded-xl border transition-all bg-slate-800/40 border-slate-700/50 text-slate-600">
                                    <div class="flex items-center gap-1.5">
                                        <span id="led-text1" class="w-2 h-2 rounded-full bg-slate-700 flex-shrink-0"></span>
                                        <span class="text-[11px]">📝 Texte 1</span>
                                    </div>
                                    <span id="countdown-text1" class="text-[10px] font-mono tabular-nums"></span>
                                </div>
                                <!-- Texte 2 -->
                                <div id="badge-text2" class="flex items-center justify-between px-2.5 py-1.5 rounded-xl border transition-all bg-slate-800/40 border-slate-700/50 text-slate-600">
                                    <div class="flex items-center gap-1.5">
                                        <span id="led-text2" class="w-2 h-2 rounded-full bg-slate-700 flex-shrink-0"></span>
                                        <span class="text-[11px]">📝 Texte 2</span>
                                    </div>
                                    <span id="countdown-text2" class="text-[10px] font-mono tabular-nums"></span>
                                </div>
                            </div>
                        </div>

                        <button onclick="testRX(this)" class="w-full mt-1 bg-slate-800 hover:bg-emerald-900/30 border border-slate-700 py-2 rounded-lg text-[10px] font-bold uppercase tracking-widest transition-all">
                            🎧 Tester la reception
                        </button>
                    </div>
                </div>


                <!-- ── Prochain passage ISS (badge léger) ── -->
                <div style="border-radius:1.5rem;overflow:hidden;box-shadow:0 10px 30px #0006;background:rgba(15,23,42,0.6);border:1px solid #1e293b">
                    <div style="padding:10px 16px;display:flex;align-items:center;justify-content:space-between">
                        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                            <span style="font-size:10px;font-weight:900;color:#a78bfa;text-transform:uppercase;letter-spacing:.1em">🛸 ISS</span>
                            <span id="iss-next-countdown" style="font-size:9px;font-weight:700;color:#c4b5fd;background:rgba(167,139,250,.12);border:1px solid #7c3aed44;border-radius:6px;padding:1px 6px;display:none"></span>
                            <span id="iss-next-az-badge" style="display:none;font-size:9px;font-weight:700;color:#67e8f9;background:rgba(103,232,249,.08);border:1px solid rgba(103,232,249,.2);border-radius:6px;padding:1px 7px;font-family:monospace"></span>
                        </div>
                        <a onclick="switchTab('iss')" href="#"
                           style="font-size:9px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:.08em;text-decoration:none">
                            ↗ Voir
                        </a>
                    </div>
                </div>

            </div><!-- /traffic-sidebar-body -->
          </div><!-- /traffic-sidebar-col -->

            <div class="lg:col-span-9" id="traffic-console-col">
                <div class="glass rounded-[2.5rem] overflow-hidden flex flex-col h-[780px] shadow-2xl">
                    <div class="px-6 py-4 bg-slate-900/50 border-b border-slate-800" id="console-sticky-hdr">
                        <div class="flex justify-between items-center mb-3">
                            <div class="flex items-center gap-3">
                                <span id="rx-led" class="w-2.5 h-2.5 rounded-full bg-slate-700 inline-block transition-colors duration-300"></span>
                                <span class="text-xs font-black text-slate-400 uppercase tracking-widest">📻 Moniteur de Trafic APRS</span>
                            </div>
                            <div class="flex items-center gap-3 flex-wrap" id="monitor-filter-row">
                                <span class="text-[10px] font-mono text-slate-600">
                                    📤 TX&thinsp;<span id="tx-count" class="text-blue-400 font-bold">0</span>
                                    &ensp;📥 RX&thinsp;<span id="rx-count" class="text-emerald-400 font-bold">0</span>
                                    &ensp;🌐 IS&thinsp;<span id="is-count" class="text-violet-400 font-bold">0</span>
                                </span>
                                <select id="monitor-filter" onchange="applyMonitorFilter()"
                                    style="background:#0f172a;border:1px solid #1e3a5f;border-radius:7px;padding:2px 8px;color:#94a3b8;font-size:10px;outline:none;cursor:pointer;min-height:unset">
                                    <option value="">Tout afficher</option>
                                    <option value="RX">📥 RX uniquement</option>
                                    <option value="TX">📤 TX uniquement</option>
                                    <option value="IS">🌐 IS uniquement</option>
                                    <option value="pos">📍 Positions</option>
                                    <option value="msg">💬 Messages</option>
                                    <option value="wx">🌦️ Météo</option>
                                </select>
                                <button onclick="clearConsole()" class="text-[10px] text-slate-600 hover:text-red-400 font-bold uppercase tracking-widest transition-colors" style="min-height:unset">
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
                    <div id="console" class="flex-grow p-6 font-mono text-sm overflow-y-auto custom-scrollbar space-y-3 bg-slate-950/30" style="padding:12px;gap:8px">
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
                            <label class="block text-[10px] font-black text-slate-500 uppercase mb-3 ml-1">📻 Mon Indicatif (SSID)
                                <button type="button" onclick="document.getElementById('ssid-popover').classList.toggle('hidden')"
                                        class="ml-2 normal-case font-normal text-blue-400 hover:text-blue-300 transition-colors align-middle">❓</button>
                            </label>
                            <input type="text" name="callsign" value='""" + config_manager.data['callsign'] + """' class="w-full bg-slate-900 border border-slate-800 rounded-xl p-4 text-white outline-none focus:border-blue-500 transition-all">
                            <div id="ssid-popover" class="hidden mt-2 bg-slate-950 border border-slate-700 rounded-xl p-3 text-[10px] text-slate-500 shadow-xl">
                                <div class="text-slate-400 font-bold mb-2">SSID — Suffixe d'identification secondaire</div>
                                <div class="grid grid-cols-2 gap-x-4 gap-y-0.5 font-mono">
                                    <span><span class="text-slate-300">-0</span> Station principale</span>
                                    <span><span class="text-slate-300">-7</span> Portable / HT</span>
                                    <span><span class="text-slate-300">-5</span> Autre équipement</span>
                                    <span><span class="text-slate-300">-9</span> Mobile (voiture)</span>
                                    <span><span class="text-slate-300">-6</span> Satellite / ballon</span>
                                    <span><span class="text-slate-300">-10</span> iGate Internet</span>
                                    <span><span class="text-slate-300">-11</span> Avion</span>
                                    <span><span class="text-slate-300">-13</span> Station météo</span>
                                    <span><span class="text-slate-300">-12</span> Portable (autre)</span>
                                    <span><span class="text-slate-300">-15</span> Station fixe</span>
                                </div>
                                <div class="pt-2 text-slate-600">Ex : <span class="text-slate-400 font-mono">F1RIQ-9</span> mobile · <span class="text-slate-400 font-mono">F1RIQ-7</span> portable</div>
                            </div>
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
                                        <optgroup label="─── Mode HF 300 bauds ───">
                                        <option value="">Direct RF (recommandé HF)</option>
                                        <option value="GATE">GATE &nbsp;(passerelle HF→IS)</option>
                                        <option value="RELAY">RELAY &nbsp;(digi HF ancien)</option>
                                        </optgroup>
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
                                            <div class="mt-3 space-y-2">
                                              <div class="text-[9px] font-black text-slate-600 uppercase tracking-widest mb-1">📝 Textes libres</div>
                                              <!-- Text1 -->
                                              <div class="bg-slate-900/60 rounded-xl px-3 py-2 space-y-1.5">
                                                <div class="flex items-center justify-between gap-3">
                                                  <span class="text-[11px] font-bold text-slate-300 w-32">📝 Texte 1</span>
                                                  """ + _interval_select("text1", _scheds) + """
                                                </div>
                                                <input type="text" name="beacon_text1"
                                                       value='""" + (config_manager.data.get("beacon_text1") or "") + """'
                                                       maxlength="62" placeholder="ex: F1RIQ QRP portable JN07II"
                                                       class="w-full bg-slate-950/60 border border-slate-700 rounded-lg px-2 py-1.5
                                                              text-[11px] text-white outline-none focus:border-blue-500 transition-all
                                                              font-mono placeholder-slate-600">
                                              </div>
                                              <!-- Text2 -->
                                              <div class="bg-slate-900/60 rounded-xl px-3 py-2 space-y-1.5">
                                                <div class="flex items-center justify-between gap-3">
                                                  <span class="text-[11px] font-bold text-slate-300 w-32">📝 Texte 2</span>
                                                  """ + _interval_select("text2", _scheds) + """
                                                </div>
                                                <input type="text" name="beacon_text2"
                                                       value='""" + (config_manager.data.get("beacon_text2") or "") + """'
                                                       maxlength="62" placeholder="ex: 73 de F1RIQ - En ecoute VHF"
                                                       class="w-full bg-slate-950/60 border border-slate-700 rounded-lg px-2 py-1.5
                                                              text-[11px] text-white outline-none focus:border-blue-500 transition-all
                                                              font-mono placeholder-slate-600">
                                              </div>
                                            </div>
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



                    <!-- ── Digipeater APRS ── -->
                    <div class="border-t border-slate-800/60 pt-8">
                        <div class="flex items-center justify-between mb-5">
                            <h2 class="text-sm font-black text-white uppercase tracking-widest">🔁 Digipeater APRS</h2>
                            <div id="digi-counter-badge" class="flex items-center gap-2 text-[10px] font-mono text-slate-500">
                                <span id="digi-dot" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#334155"></span>
                                <span id="digi-count-txt">0 retransmissions</span>
                            </div>
                        </div>

                        <!-- Toggle activer/désactiver -->
                        <div class="flex items-center justify-between bg-slate-900/60 rounded-2xl px-4 py-3 mb-4">
                            <div>
                                <div class="text-white font-bold text-xs">Activer le digipeater</div>
                                <div class="text-slate-500 text-[10px] mt-0.5">Retransmet les trames RF reçues (WIDEn-N, RELAY, WIDE1)</div>
                            </div>
                            <label class="relative inline-flex items-center cursor-pointer">
                                <input type="checkbox" name="digi_enabled" id="digi_enabled"
                                    """ + ('checked' if config_manager.data.get('digi_enabled') else '') + """
                                    class="sr-only peer">
                                <div class="w-11 h-6 bg-slate-700 peer-focus:ring-2 peer-focus:ring-emerald-500 rounded-full peer
                                    peer-checked:after:translate-x-full peer-checked:bg-emerald-600
                                    after:content-[''] after:absolute after:top-[2px] after:left-[2px]
                                    after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all"></div>
                            </label>
                        </div>

                        <!-- Aliases -->
                        <div class="mb-4">
                            <label class="block text-[10px] font-black text-slate-500 uppercase mb-2 ml-1">
                                Alias acceptés <span class="text-slate-700 normal-case">(séparés par des virgules)</span>
                            </label>
                            <input type="text" name="digi_aliases" id="digi_aliases"
                                value='""" + ','.join(config_manager.data.get('digi_aliases', ['WIDE1-1','WIDE1','RELAY'])) + """'
                                class="w-full bg-slate-900 border border-slate-800 rounded-xl p-3 text-white text-sm font-mono outline-none focus:border-emerald-500 transition-all"
                                placeholder="WIDE1-1,WIDE1,RELAY">
                            <p class="text-slate-600 text-[9px] mt-1 ml-1">Alias standards : <span class="text-slate-500">WIDE1-1, WIDE1, RELAY, TRACE</span></p>
                        </div>

                        <!-- Limite de sauts -->
                        <div class="mb-4">
                            <label class="block text-[10px] font-black text-slate-500 uppercase mb-2 ml-1">
                                Limite WIDEn-N <span class="text-slate-700 normal-case">(n max accepté)</span>
                            </label>
                            <div class="flex items-center gap-3">
                                <input type="number" name="digi_limit" id="digi_limit" min="1" max="7"
                                    value='""" + str(config_manager.data.get('digi_limit', 2)) + """'
                                    class="w-24 bg-slate-900 border border-slate-800 rounded-xl p-3 text-white text-sm font-mono outline-none focus:border-emerald-500 transition-all text-center">
                                <span class="text-slate-500 text-[10px]">Recommandé : 2 · WIDE3-3 et au-delà = flood</span>
                            </div>
                        </div>

                        <!-- Compteur -->
                        <div class="bg-slate-900/60 rounded-xl px-4 py-3 text-center">
                            <div class="text-slate-500 text-[9px] uppercase mb-1">Trames retransmises (session)</div>
                            <div id="digi-count-big" class="text-emerald-400 font-black text-2xl tabular-nums">0</div>
                        </div>
                    </div>

                    <!-- ── Mode HF 300 bauds ── -->
                    <div class="border-t border-slate-800/60 pt-8">
                        <div class="flex items-center justify-between mb-5">
                            <h2 class="text-sm font-black text-amber-400 uppercase tracking-widest">📻 Mode HF 300 bauds</h2>
                            <span class="text-[10px] font-mono text-slate-600 bg-slate-900 rounded-lg px-2 py-1">AX.25 / APRS-HF</span>
                        </div>
                        <p class="text-slate-500 text-[10px] mb-5">
                            Active la transmission <strong class="text-amber-300">HF 300 bauds</strong> (AFSK AX.25 — fréquences de référence APRS-HF).<br>
                            Réglez la radio en <span class="text-amber-300 font-mono">USB</span> · fréquences recommandées :
                            <span class="font-mono text-slate-400">10.151 MHz</span> · <span class="font-mono text-slate-400">14.105 MHz</span><br>
                            Le modem Dire Wolf passe automatiquement en <span class="font-mono text-amber-300">MODEM 300 MARK:SPACE</span>.
                        </p>

                        <!-- Toggle HF mode -->
                        <div class="flex items-center justify-between bg-slate-900/60 rounded-2xl px-4 py-3 mb-5">
                            <div>
                                <div class="text-white font-bold text-xs">Activer le mode HF 300 bauds</div>
                                <div class="text-slate-500 text-[10px] mt-0.5">Remplace 1200 bauds VHF — redémarrage Dire Wolf requis</div>
                            </div>
                            <label class="relative inline-flex items-center cursor-pointer">
                                <input type="checkbox" name="hf_mode" id="hf_mode"
                                    """ + ('checked' if config_manager.data.get('hf_mode') else '') + """
                                    onchange="document.getElementById('hf_tone_fields').style.display=this.checked?'block':'none';checkHfPathWarn()"
                                    class="sr-only peer">
                                <div class="w-11 h-6 bg-slate-700 peer-focus:ring-2 peer-focus:ring-amber-500 rounded-full peer
                                    peer-checked:after:translate-x-full peer-checked:bg-amber-600
                                    after:content-[''] after:absolute after:top-[2px] after:left-[2px]
                                    after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all"></div>
                            </label>
                        </div>

                        <!-- Tonalités MARK / SPACE -->
                        <div id="hf_tone_fields" """ + ('' if config_manager.data.get('hf_mode') else 'style="display:none"') + """>
                            <div class="grid grid-cols-2 gap-4 mb-4">
                                <div>
                                    <label class="block text-[10px] font-black text-slate-500 uppercase mb-2 ml-1">
                                        Tonalité MARK (Hz)
                                    </label>
                                    <input type="number" name="hf_mark_hz" id="hf_mark_hz"
                                        min="300" max="3000" step="10"
                                        value='""" + str(config_manager.data.get('hf_mark_hz', 1600)) + """'
                                        class="w-full bg-slate-900 border border-slate-800 rounded-xl p-3 text-white text-sm font-mono outline-none focus:border-amber-500 transition-all text-center">
                                    <p class="text-slate-600 text-[9px] mt-1 ml-1">Standard AX.25 HF : <span class="text-amber-500">1600 Hz</span></p>
                                </div>
                                <div>
                                    <label class="block text-[10px] font-black text-slate-500 uppercase mb-2 ml-1">
                                        Tonalité SPACE (Hz)
                                    </label>
                                    <input type="number" name="hf_space_hz" id="hf_space_hz"
                                        min="300" max="3000" step="10"
                                        value='""" + str(config_manager.data.get('hf_space_hz', 1800)) + """'
                                        class="w-full bg-slate-900 border border-slate-800 rounded-xl p-3 text-white text-sm font-mono outline-none focus:border-amber-500 transition-all text-center">
                                    <p class="text-slate-600 text-[9px] mt-1 ml-1">Standard AX.25 HF : <span class="text-amber-500">1800 Hz</span></p>
                                </div>
                            </div>
                            <div class="bg-amber-900/20 border border-amber-800/40 rounded-xl px-4 py-3 text-[10px] text-amber-300/80">
                                ⚠️ <strong>Rappel :</strong> en mode HF, le TXDELAY minimum est automatiquement fixé à 500 ms.
                                Vérifiez que votre radio est en mode <strong>USB</strong> et que le niveau audio est calibré.
                            </div>
                        </div>
                    </div>

                    <!-- ── Warning HF path WIDE ── HF_TX_PATH_PATCH_APPLIED -->
                    <div id="hf_wide_warn" style="display:none" class="mt-2 bg-red-900/30 border border-red-700/50 rounded-xl px-4 py-2 text-[10px] text-red-300">
                        ⚠️ <strong>Path incompatible :</strong> le chemin contient <code>WIDE</code>, inutile en HF 300 bauds (pas de digis HF WIDE). Utilisez un path <strong>vide</strong> (direct RF) ou <code>GATE</code>. En mode HF, le patch forcera automatiquement un chemin vide à l'émission.
                    </div>

                    <!-- ── API aprs.fi ── -->
                    <div class="border-t border-slate-800/60 pt-8">
                        <h2 class="text-sm font-black text-white uppercase tracking-widest mb-1">🌐 API aprs.fi</h2>
                        <p class="text-slate-500 text-[10px] mb-5">
                            Optionnel — enrichit le panneau «&nbsp;Mon signal&nbsp;» avec l'historique aprs.fi (24h).
                            Clé gratuite sur
                            <a href="https://aprs.fi/page/api" target="_blank"
                               class="text-blue-400 hover:text-blue-300 underline">aprs.fi/page/api</a>.
                        </p>
                        <div class="mb-2">
                            <label class="block text-[10px] font-black text-slate-500 uppercase mb-2 ml-1">
                                Clé API <span class="text-slate-700 normal-case">(laisser vide si inutilisée)</span>
                            </label>
                            <input type="text" name="aprsfi_key" id="aprsfi_key"
                                value='""" + (config_manager.data.get('aprsfi_key') or '') + """'
                                class="w-full bg-slate-900 border border-slate-800 rounded-xl p-3 text-white text-sm font-mono outline-none focus:border-sky-500 transition-all"
                                placeholder="XXXXXX.XXXXXXXXXXXXXXXXX"
                                autocomplete="off" spellcheck="false">
                            <p class="text-slate-600 text-[9px] mt-2 ml-1">
                                La clé est transmise uniquement depuis le serveur (Raspberry Pi) — jamais exposée au navigateur.
                            </p>
                        </div>
                    </div>

                    <!-- ── Alertes Météo ── WX_ALERT_UI_PATCH_v1 ── -->
                    <div class="border-t border-slate-800/60 pt-8">
                        <h3 class="text-[10px] font-black text-yellow-400 uppercase tracking-widest mb-6 flex items-center gap-2">
                            🌡️ Alertes Météo (Open-Meteo)
                        </h3>
                        <div class="space-y-5">

                            <!-- Toggle principal -->
                            <div class="flex items-center justify-between">
                                <label class="text-slate-300 text-xs font-semibold" for="wx-alert-toggle">Activer les alertes météo</label>
                                <label class="relative inline-flex items-center cursor-pointer">
                                    <input type="checkbox" id="wx-alert-toggle" onchange="wxAlertToggle()" class="sr-only peer">
                                    <div class="w-9 h-5 bg-slate-700 rounded-full peer peer-checked:bg-yellow-500 transition-colors duration-200 after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-4"></div>
                                </label>
                            </div>

                            <!-- Température max -->
                            <div class="flex items-center justify-between gap-4">
                                <label class="text-slate-400 text-[11px] w-48 shrink-0" for="wx-temp-max">🌡️ Alerte chaleur (°C)</label>
                                <div class="flex items-center gap-2">
                                    <input type="number" id="wx-temp-max" min="-10" max="60" step="1"
                                        onchange="wxAlertSave()"
                                        class="w-20 bg-slate-800 border border-slate-600 rounded px-2 py-1 text-white text-xs text-right focus:outline-none focus:border-yellow-500">
                                    <span class="text-slate-500 text-[10px]">°C si temp ≥ seuil</span>
                                </div>
                            </div>

                            <!-- Température min (gel) -->
                            <div class="flex items-center justify-between gap-4">
                                <label class="text-slate-400 text-[11px] w-48 shrink-0" for="wx-temp-min">🧊 Alerte gel (°C)</label>
                                <div class="flex items-center gap-2">
                                    <input type="number" id="wx-temp-min" min="-30" max="15" step="1"
                                        onchange="wxAlertSave()"
                                        class="w-20 bg-slate-800 border border-slate-600 rounded px-2 py-1 text-white text-xs text-right focus:outline-none focus:border-yellow-500">
                                    <span class="text-slate-500 text-[10px]">°C si temp ≤ seuil</span>
                                </div>
                            </div>

                            <!-- Vent moyen -->
                            <div class="flex items-center justify-between gap-4">
                                <label class="text-slate-400 text-[11px] w-48 shrink-0" for="wx-wind-max">💨 Vent fort (km/h)</label>
                                <div class="flex items-center gap-2">
                                    <input type="number" id="wx-wind-max" min="10" max="200" step="5"
                                        onchange="wxAlertSave()"
                                        class="w-20 bg-slate-800 border border-slate-600 rounded px-2 py-1 text-white text-xs text-right focus:outline-none focus:border-yellow-500">
                                    <span class="text-slate-500 text-[10px]">km/h</span>
                                </div>
                            </div>

                            <!-- Rafales -->
                            <div class="flex items-center justify-between gap-4">
                                <label class="text-slate-400 text-[11px] w-48 shrink-0" for="wx-gust-max">🌪️ Rafales (km/h)</label>
                                <div class="flex items-center gap-2">
                                    <input type="number" id="wx-gust-max" min="10" max="250" step="5"
                                        onchange="wxAlertSave()"
                                        class="w-20 bg-slate-800 border border-slate-600 rounded px-2 py-1 text-white text-xs text-right focus:outline-none focus:border-yellow-500">
                                    <span class="text-slate-500 text-[10px]">km/h</span>
                                </div>
                            </div>

                            <!-- Précipitations -->
                            <div class="flex items-center justify-between gap-4">
                                <label class="text-slate-400 text-[11px] w-48 shrink-0" for="wx-rain-mm">🌧️ Pluie intense (mm/h)</label>
                                <div class="flex items-center gap-2">
                                    <input type="number" id="wx-rain-mm" min="1" max="100" step="1"
                                        onchange="wxAlertSave()"
                                        class="w-20 bg-slate-800 border border-slate-600 rounded px-2 py-1 text-white text-xs text-right focus:outline-none focus:border-yellow-500">
                                    <span class="text-slate-500 text-[10px]">mm</span>
                                </div>
                            </div>

                            <!-- WMO sévère -->
                            <div class="flex items-center justify-between">
                                <label class="text-slate-400 text-[11px]" for="wx-wmo-severe">⛈️ Alerte code météo sévère (orage, neige…)</label>
                                <label class="relative inline-flex items-center cursor-pointer">
                                    <input type="checkbox" id="wx-wmo-severe" onchange="wxAlertSave()" class="sr-only peer">
                                    <div class="w-9 h-5 bg-slate-700 rounded-full peer peer-checked:bg-yellow-500 transition-colors duration-200 after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-4"></div>
                                </label>
                            </div>

                            <!-- Intervalle -->
                            <div class="flex items-center justify-between gap-4">
                                <label class="text-slate-400 text-[11px] w-48 shrink-0" for="wx-interval-min">🕐 Intervalle vérification</label>
                                <select id="wx-interval-min" onchange="wxAlertSave()"
                                    class="bg-slate-800 border border-slate-600 rounded px-2 py-1 text-white text-xs focus:outline-none focus:border-yellow-500">
                                    <option value="15">15 min</option>
                                    <option value="30">30 min</option>
                                    <option value="60">1 h</option>
                                    <option value="120">2 h</option>
                                </select>
                            </div>

                            <!-- Checkbox bulletin APRS RF -->
                            <div class="flex items-center justify-between">
                                <label class="text-slate-400 text-[11px]" for="wx-bulletin-aprs">📡 Émettre bulletin APRS RF lors d'une alerte</label>
                                <label class="relative inline-flex items-center cursor-pointer">
                                    <input type="checkbox" id="wx-bulletin-aprs" onchange="wxAlertSave()" class="sr-only peer">
                                    <div class="w-9 h-5 bg-slate-700 rounded-full peer peer-checked:bg-yellow-500 transition-colors duration-200 after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-4"></div>
                                </label>
                            </div>

                            <!-- Bouton test + statut -->
                            <div class="flex items-center gap-3 pt-2">
                                <button onclick="wxAlertTest(this)"
                                    class="px-3 py-1.5 bg-yellow-900/60 hover:bg-yellow-800 border border-yellow-700 text-yellow-300 text-[11px] font-bold rounded transition-colors">
                                    🧪 Tester les alertes maintenant
                                </button>
                                <span id="wx-alert-status" class="text-[10px] text-slate-500"></span>
                            </div>

                        </div><!-- /space-y-5 -->
                    </div><!-- /section wx-alert -->

                    <!-- ── Alertes propagation / blackout ── -->
                    <div class="border-t border-slate-800/60 pt-8">
                        <h3 class="text-[10px] font-black text-orange-400 uppercase tracking-widest mb-6 flex items-center gap-2">
                            📶 Alertes Propagation &amp; Blackout HF
                        </h3>
                        <div class="space-y-5">
                            <!-- Toggle principal -->
                            <div class="flex items-center justify-between">
                                <div>
                                    <div class="text-sm font-bold text-white">Surveillance propagation</div>
                                    <div class="text-[10px] text-slate-500 mt-0.5">Bannière + bip sur tempête Kp, blackout SFI ou éruption solaire X-ray</div>
                                </div>
                                <label class="prox-switch" title="Activer / désactiver les alertes propagation">
                                    <input type="checkbox" id="prop-alert-toggle" onchange="propAlertToggle()">
                                    <span class="prox-slider" style="--sw-on:#c2410c"></span>
                                </label>
                            </div>
                            <!-- Seuil Kp -->
                            <div class="flex items-center gap-4">
                                <label class="text-[10px] text-slate-500 uppercase font-bold shrink-0 w-40">🧲 Kp max (tempête)</label>
                                <select id="prop-kp-max" onchange="propAlertSave()"
                                    class="flex-1 bg-slate-900 border border-slate-800 rounded-xl p-2 text-white text-sm font-mono outline-none focus:border-orange-500 transition-all">
                                    <option value="3">≥ 3 — Agité</option>
                                    <option value="4">≥ 4 — Actif</option>
                                    <option value="5" selected>≥ 5 — Tempête G1</option>
                                    <option value="6">≥ 6 — Tempête G2</option>
                                    <option value="7">≥ 7 — Tempête G3</option>
                                </select>
                            </div>
                            <!-- Seuil SFI -->
                            <div class="flex items-center gap-4">
                                <label class="text-[10px] text-slate-500 uppercase font-bold shrink-0 w-40">📉 SFI min (blackout)</label>
                                <select id="prop-sfi-min" onchange="propAlertSave()"
                                    class="flex-1 bg-slate-900 border border-slate-800 rounded-xl p-2 text-white text-sm font-mono outline-none focus:border-orange-500 transition-all">
                                    <option value="60">< 60 — Très mauvais</option>
                                    <option value="70" selected>< 70 — Mauvais</option>
                                    <option value="80">< 80 — Faible</option>
                                    <option value="90">< 90 — Médiocre</option>
                                </select>
                            </div>
                            <!-- Seuil X-ray -->
                            <div class="flex items-center gap-4">
                                <label class="text-[10px] text-slate-500 uppercase font-bold shrink-0 w-40">☀️ Éruption X-ray min</label>
                                <select id="prop-xray-class" onchange="propAlertSave()"
                                    class="flex-1 bg-slate-900 border border-slate-800 rounded-xl p-2 text-white text-sm font-mono outline-none focus:border-orange-500 transition-all">
                                    <option value="C1">≥ C1 — Faible</option>
                                    <option value="M1" selected>≥ M1 — Modérée</option>
                                    <option value="M5">≥ M5 — Forte</option>
                                    <option value="X1">≥ X1 — Majeure</option>
                                    <option value="X5">≥ X5 — Extrême</option>
                                </select>
                            </div>
                            <!-- Intervalle vérification -->
                            <div class="flex items-center gap-4">
                                <label class="text-[10px] text-slate-500 uppercase font-bold shrink-0 w-40">⏱ Vérification</label>
                                <select id="prop-interval-min" onchange="propAlertSave()"
                                    class="flex-1 bg-slate-900 border border-slate-800 rounded-xl p-2 text-white text-sm font-mono outline-none focus:border-orange-500 transition-all">
                                    <option value="5">Toutes les 5 min</option>
                                    <option value="10">Toutes les 10 min</option>
                                    <option value="15" selected>Toutes les 15 min</option>
                                    <option value="30">Toutes les 30 min</option>
                                    <option value="60">Toutes les heures</option>
                                </select>
                            </div>
                            <!-- Bouton test -->
                            <button onclick="propAlertTest(this)"
                                class="w-full bg-slate-800 hover:bg-orange-900/30 border border-slate-700 py-2 rounded-xl text-[10px] font-bold uppercase tracking-widest transition-all">
                                🔬 Tester les alertes propagation
                            </button>
                        </div>
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
                                <div class="text-[10px] text-slate-500 uppercase font-bold mb-2">📅 Passages du jour</div>
                                <div id="iss-pass-list-cfg" class="bg-slate-900/60 rounded-xl p-3 text-[10px] text-slate-500 italic">
                                    ⏳ Chargement...
                                </div>
                            </div>
                        </div>
                    </div>

                <!-- ── Changement de mot de passe ── -->
                <div class="mt-6 glass rounded-2xl p-6">
                    <h3 class="text-[10px] font-black text-yellow-400 uppercase tracking-widest mb-5 flex items-center gap-2">🔑 Changer le mot de passe</h3>
                    <div class="space-y-4">
                        <div>
                            <label class="block text-xs font-bold text-slate-400 mb-1.5 uppercase tracking-wider">Mot de passe actuel</label>
                            <input id="passwd-current" type="password" autocomplete="current-password"
                                   class="w-full bg-slate-800 border border-slate-700 rounded-xl px-4 py-2.5 text-white text-sm focus:outline-none focus:border-blue-500 transition-colors"
                                   placeholder="••••••••">
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-400 mb-1.5 uppercase tracking-wider">Nouveau mot de passe</label>
                            <input id="passwd-new" type="password" autocomplete="new-password"
                                   class="w-full bg-slate-800 border border-slate-700 rounded-xl px-4 py-2.5 text-white text-sm focus:outline-none focus:border-blue-500 transition-colors"
                                   placeholder="••••••••">
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-400 mb-1.5 uppercase tracking-wider">Confirmer</label>
                            <input id="passwd-confirm" type="password" autocomplete="new-password"
                                   class="w-full bg-slate-800 border border-slate-700 rounded-xl px-4 py-2.5 text-white text-sm focus:outline-none focus:border-blue-500 transition-colors"
                                   placeholder="••••••••"
                                   onkeydown="if(event.key==='Enter')submitPasswd()">
                        </div>
                        <div id="passwd-msg" class="hidden text-xs rounded-xl px-4 py-2.5 font-bold"></div>
                        <button onclick="submitPasswd()"
                                class="w-full bg-yellow-600 hover:bg-yellow-500 active:scale-95 text-white font-black py-3 rounded-xl transition-all text-sm tracking-wide">
                            🔑 Changer le mot de passe
                        </button>
                    </div>
                </div>

                    <!-- ── Bouton appliquer (fin de page) ── -->
                    <button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-500 p-5 rounded-2xl font-black text-white shadow-xl transition-all active:scale-95 mt-2">
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
                                <button onclick="openISS()"
                                        class="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-[10px] font-black transition-all active:scale-95 bg-indigo-500/15 border border-indigo-500/40 text-indigo-300 hover:bg-indigo-500/30 hover:text-white"
                                        title="Contact ISS — path ARISS, dest RS0ISS-4, QRG 145.825 MHz">
                                    🛸 ISS
                                </button>
                                <button id="btn-iss-send" onclick="sendISS()"
                                        title="Envoyer un beacon ARISS — actif uniquement si ISS en vue"
                                        class="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-[10px] font-black transition-all active:scale-95 bg-slate-800/50 border border-slate-700 text-slate-400 hover:bg-indigo-700/40 hover:border-indigo-500 hover:text-indigo-200">
                                    📡 BEACON
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
                            <div class="flex-1 min-w-0">
                                <div id="chat-title" class="font-black text-white text-sm">Selectionner un contact</div>
                                <div id="chat-subtitle" class="text-[10px] text-slate-500">APRS Point-a-Point</div>
                            </div>
                            <button id="chat-delete-btn"
                                    onclick="deleteQso()"
                                    title="Supprimer cette conversation"
                                    disabled
                                    class="w-8 h-8 flex items-center justify-center rounded-lg bg-red-950/40 border border-red-900/50 text-red-400 hover:bg-red-700 hover:text-white hover:border-red-500 transition-all active:scale-95 disabled:opacity-20 disabled:cursor-not-allowed shrink-0">
                                🗑️
                            </button>
                        </div>
                        <!-- ── ISS EN VUE (visible uniquement pendant le passage) ── -->
                        <div id="iss-live-panel" style="display:none;border-top:1px solid #7c3aed55;background:rgba(15,23,42,0.92);animation:issLivePulse 3s ease-in-out infinite">
                            <!-- Header -->
                            <div style="padding:8px 14px;background:rgba(124,58,237,.18);border-bottom:1px solid #7c3aed40;display:flex;align-items:center;justify-content:space-between">
                                <div style="display:flex;align-items:center;gap:7px">
                                    <span id="iss-live-dot" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#a78bfa;box-shadow:0 0 7px #a78bfa;flex-shrink:0"></span>
                                    <span style="font-size:10px;font-weight:900;color:#c4b5fd;text-transform:uppercase;letter-spacing:.1em">🛸 ISS EN VUE</span>
                                </div>
                                <span id="iss-live-timer" style="font-size:10px;font-weight:700;color:#a78bfa;font-family:monospace"></span>
                            </div>
                            <!-- Élévation + Azimut + Boussole -->
                            <div style="padding:8px 14px 4px;display:grid;grid-template-columns:1fr 1fr 90px;gap:8px;align-items:center">
                                <div style="background:rgba(124,58,237,.1);border:1px solid #7c3aed30;border-radius:10px;padding:6px 10px;text-align:center">
                                    <div style="font-size:9px;font-weight:700;color:#7c3aed;text-transform:uppercase;letter-spacing:.08em;margin-bottom:1px">▲ Élévation</div>
                                    <div id="iss-live-el" style="font-size:20px;font-weight:900;color:#c4b5fd;font-family:monospace;line-height:1">--°</div>
                                </div>
                                <div style="background:rgba(124,58,237,.1);border:1px solid #7c3aed30;border-radius:10px;padding:6px 10px;text-align:center">
                                    <div style="font-size:9px;font-weight:700;color:#7c3aed;text-transform:uppercase;letter-spacing:.08em;margin-bottom:1px">➤ Azimut</div>
                                    <div id="iss-live-az" style="font-size:20px;font-weight:900;color:#c4b5fd;font-family:monospace;line-height:1">--°</div>
                                    <div id="iss-live-az-card" style="font-size:10px;font-weight:700;color:#a78bfa;margin-top:1px">--</div>
                                </div>
                                <svg id="iss-compass-svg" width="90" height="90" viewBox="0 0 90 90">
                                    <circle cx="45" cy="45" r="42" fill="rgba(15,23,42,.7)" stroke="#3730a3" stroke-width="1.5"/>
                                    <circle cx="45" cy="45" r="34" fill="none" stroke="#1e1b4b" stroke-width="0.5"/>
                                    <text x="45" y="9"  text-anchor="middle" fill="#a78bfa" font-size="8" font-weight="700">N</text>
                                    <text x="45" y="86" text-anchor="middle" fill="#64748b" font-size="8" font-weight="700">S</text>
                                    <text x="85" y="49" text-anchor="middle" fill="#64748b" font-size="8" font-weight="700">E</text>
                                    <text x="5"  y="49" text-anchor="middle" fill="#64748b" font-size="8" font-weight="700">O</text>
                                    <circle cx="45" cy="45" r="22" fill="none" stroke="#1e293b" stroke-width="0.5" stroke-dasharray="2,3"/>
                                    <circle cx="45" cy="45" r="11" fill="none" stroke="#1e293b" stroke-width="0.5" stroke-dasharray="2,3"/>
                                    <g id="iss-compass-ptr" transform="rotate(0,45,45)">
                                        <polygon points="45,8 42,45 45,42 48,45" fill="#a78bfa" opacity="0.9"/>
                                        <polygon points="45,82 42,45 45,48 48,45" fill="#3730a3" opacity="0.6"/>
                                    </g>
                                    <circle id="iss-compass-dot" cx="45" cy="23" r="4" fill="#a78bfa" stroke="#7c3aed" stroke-width="1.5" style="filter:drop-shadow(0 0 4px #a78bfa)"/>
                                    <text id="iss-compass-icon" x="45" y="26" text-anchor="middle" font-size="6" fill="white">🛸</text>
                                </svg>
                            </div>
                            <!-- Télémétrie compacte -->
                            <div style="padding:2px 14px 6px;display:grid;grid-template-columns:repeat(4,1fr);gap:5px;font-size:10px;font-family:monospace">
                                <div style="background:rgba(15,23,42,.6);border:1px solid #1e293b;border-radius:8px;padding:4px 8px">
                                    <div style="color:#475569;font-size:9px;font-weight:700;text-transform:uppercase;margin-bottom:1px">Dist.</div>
                                    <div id="iss-live-dist" style="color:#94a3b8;font-weight:700">-- km</div>
                                </div>
                                <div style="background:rgba(15,23,42,.6);border:1px solid #1e293b;border-radius:8px;padding:4px 8px">
                                    <div style="color:#475569;font-size:9px;font-weight:700;text-transform:uppercase;margin-bottom:1px">Alt.</div>
                                    <div id="iss-live-alt" style="color:#94a3b8;font-weight:700">-- km</div>
                                </div>
                                <div style="background:rgba(15,23,42,.6);border:1px solid #1e293b;border-radius:8px;padding:4px 8px">
                                    <div style="color:#475569;font-size:9px;font-weight:700;text-transform:uppercase;margin-bottom:1px">Doppler</div>
                                    <div id="iss-live-doppler" style="color:#fbbf24;font-weight:700">-- Hz</div>
                                </div>
                                <div style="background:rgba(15,23,42,.6);border:1px solid #1e293b;border-radius:8px;padding:4px 8px">
                                    <div style="color:#475569;font-size:9px;font-weight:700;text-transform:uppercase;margin-bottom:1px">Fréq.</div>
                                    <div id="iss-live-freq" style="color:#34d399;font-weight:700">-- kHz</div>
                                </div>
                            </div>
                            <!-- Footer APRS params -->
                            <div style="padding:2px 14px 8px">
                                <div id="iss-live-az-footer" style="display:none;margin-bottom:4px;background:rgba(103,232,249,.06);border:1px solid rgba(103,232,249,.18);border-radius:8px;padding:4px 10px;font-size:9px;font-family:monospace;color:#67e8f9;text-align:center;font-weight:700;letter-spacing:.04em"></div>
                                <div style="background:rgba(124,58,237,.08);border:1px solid #7c3aed25;border-radius:8px;padding:5px 10px;font-size:9px;font-family:monospace;color:#7c3aed;text-align:center;font-weight:700;letter-spacing:.03em">
                                    APRS : 145.825 MHz FM · 1200 Bd AFSK
                                    <br><span style="color:#a78bfa">Path : ARISS  ·  Appel : RS0ISS-4</span>
                                </div>
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
                                <button onclick="mapClearTrails()" class="text-[10px] text-slate-600 hover:text-orange-400 font-bold uppercase tracking-widest transition-colors" title="Supprime uniquement les traînées (trajets) des mobiles, conserve les marqueurs">
                                    〰️ Trajets
                                </button>
                                <button onclick="openNearbyModal()" id="btn-nearby" class="text-[10px] text-slate-400 hover:text-cyan-400 font-bold uppercase tracking-widest transition-colors" title="Recherche périmétrique des stations">                                    📡 Périm.                                </button>                                <button onclick="mapFitAll()" class="text-[10px] text-slate-400 hover:text-blue-400 font-bold uppercase tracking-widest transition-colors">
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

                <!-- ── Passages 24h ── -->
                <div class="glass rounded-[2rem] overflow-hidden shadow-2xl">
                    <div class="px-5 py-3 bg-slate-900/50 border-b border-slate-800 flex items-center justify-between">
                        <span class="text-[10px] font-black text-violet-400 uppercase tracking-widest">📅 Passages ISS — 24 heures</span>
                        <button type="button" onclick="iss24hRefresh()" id="iss-24h-btn"
                                class="text-[9px] font-bold uppercase tracking-widest text-slate-500 hover:text-violet-300 transition-colors bg-slate-800/60 rounded-lg px-3 py-1.5">
                            ↺ MAJ
                        </button>
                    </div>
                    <div id="iss-24h-list" data-alias="iss-pass-list" class="p-4 space-y-2">
                        <span class="text-slate-500 text-[10px] italic">Chargement…</span>
                    </div>
                    <div class="px-5 py-2 bg-slate-900/30 border-t border-slate-800">
                        <span id="iss-24h-status" class="text-[9px] font-mono text-slate-600">—</span>
                    </div>
                </div>

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

        <!-- ══════════════════════════════ SSTV ════════════════════════════════ -->
        <div id="tab-sstv" class="hidden">
            <div class="grid grid-cols-1 gap-6">


                <!-- ── Sites en ligne ── -->
                <div class="glass rounded-2xl p-5 space-y-4">
                    <div class="text-[10px] font-black text-yellow-400 uppercase tracking-widest mb-3">🌐 Sites de réception SSTV en ligne</div>

                    <!-- Robot36 Web Decoder -->
                    <div class="bg-slate-900/60 rounded-2xl p-4 flex items-start gap-4">
                        <div class="w-9 h-9 rounded-xl flex items-center justify-center text-xl shrink-0"
                             style="background:rgba(99,102,241,.12);border:1px solid #6366f130">🔊</div>
                        <div class="flex-1 min-w-0">
                            <div class="text-xs font-black text-indigo-300 mb-1">Robot36 — SSTV Image Decoder (WebAssembly)</div>
                            <div class="text-[11px] text-slate-400 mb-3">Décodeur WebAssembly dans le navigateur. Supporte Robot36, Scottie, Martin et PD. Utilise le micro de votre PC directement.</div>
                            <a href="https://robot36.netlify.app" target="_blank" rel="noopener"
                               class="inline-flex items-center gap-2 px-4 py-2 rounded-xl text-[11px] font-bold text-white transition-all"
                               style="background:rgba(99,102,241,.25);border:1px solid #6366f140">
                                🔗 Ouvrir robot36.netlify.app
                            </a>
                        </div>
                    </div>

                    <!-- SSTV Demodulator (colorssstv) -->
                    <div class="bg-slate-900/60 rounded-2xl p-4 flex items-start gap-4">
                        <div class="w-9 h-9 rounded-xl flex items-center justify-center text-xl shrink-0"
                             style="background:rgba(16,185,129,.12);border:1px solid #10b98130">🖼️</div>
                        <div class="flex-1 min-w-0">
                            <div class="text-xs font-black text-emerald-300 mb-1">ColorSSTV / MMSSTV</div>
                            <div class="text-[11px] text-slate-400 mb-3">Logiciels PC classiques, très complets. ColorSSTV est gratuit (Windows). MMSSTV supporte tous les modes courants.</div>
                            <div class="flex flex-wrap gap-2">
                                <a href="https://www.colorsstv.com" target="_blank" rel="noopener"
                                   class="inline-flex items-center gap-2 px-4 py-2 rounded-xl text-[11px] font-bold text-white transition-all"
                                   style="background:rgba(16,185,129,.20);border:1px solid #10b98130">
                                    🔗 colorsstv.com
                                </a>
                                <a href="https://hamsoft.ca/pages/mmsstv.php" target="_blank" rel="noopener"
                                   class="inline-flex items-center gap-2 px-4 py-2 rounded-xl text-[11px] font-bold text-white transition-all"
                                   style="background:rgba(16,185,129,.20);border:1px solid #10b98130">
                                    🔗 MMSSTV
                                </a>
                            </div>
                        </div>
                    </div>

                    <!-- SSTV sur Raspberry Pi -->
                    <div class="bg-slate-900/60 rounded-2xl p-4 flex items-start gap-4">
                        <div class="w-9 h-9 rounded-xl flex items-center justify-center text-xl shrink-0"
                             style="background:rgba(239,68,68,.12);border:1px solid #ef444430">🍓</div>
                        <div class="flex-1 min-w-0">
                            <div class="text-xs font-black text-red-300 mb-1">QSSTV — Linux / Raspberry Pi</div>
                            <div class="text-[11px] text-slate-400 mb-3">Décodeur Linux natif, disponible dans apt. Idéal sur Raspberry Pi. Interface graphique complète.</div>
                            <div class="bg-slate-950/60 rounded-xl px-3 py-2 font-mono text-[11px] text-emerald-400 mb-2">
                                sudo apt install qsstv
                            </div>
                        </div>
                    </div>

                    <!-- ISS SSTV -->
                    <div class="bg-slate-900/60 rounded-2xl p-4 flex items-start gap-4">
                        <div class="w-9 h-9 rounded-xl flex items-center justify-center text-xl shrink-0"
                             style="background:rgba(124,58,237,.12);border:1px solid #7c3aed30">🛸</div>
                        <div class="flex-1 min-w-0">
                            <div class="text-xs font-black text-purple-300 mb-1">ISS SSTV — Galerie des images reçues</div>
                            <div class="text-[11px] text-slate-400 mb-3">La station ISS émet régulièrement des images SSTV sur 145.800 MHz FM (mode PD120). Consultez la galerie communautaire en ligne.</div>
                            <a href="https://ariss-sstv.blogspot.com" target="_blank" rel="noopener"
                               class="inline-flex items-center gap-2 px-4 py-2 rounded-xl text-[11px] font-bold text-white transition-all"
                               style="background:rgba(124,58,237,.20);border:1px solid #7c3aed30">
                                🔗 ariss-sstv.blogspot.com
                            </a>
                        </div>
                    </div>
                </div>

                <!-- ── Info fréquences ── -->
                <div class="glass rounded-2xl p-5">
                    <div class="text-[10px] font-black text-slate-500 uppercase tracking-widest mb-4">📻 Fréquences SSTV usuelles</div>
                    <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
                        <div class="p-3 rounded-xl text-center" style="background:rgba(234,179,8,.08);border:1px solid #eab30830">
                            <div class="text-[8px] font-black text-yellow-400 uppercase tracking-widest mb-1">ISS</div>
                            <div class="font-mono text-sm font-black text-yellow-300">145.800</div>
                            <div class="text-[8px] text-slate-500">FM — mode PD120</div>
                        </div>
                        <div class="p-3 rounded-xl text-center" style="background:rgba(59,130,246,.08);border:1px solid #3b82f630">
                            <div class="text-[8px] font-black text-blue-400 uppercase tracking-widest mb-1">14 MHz HF</div>
                            <div class="font-mono text-sm font-black text-blue-300">14.230</div>
                            <div class="text-[8px] text-slate-500">USB — Scottie / Martin</div>
                        </div>
                        <div class="p-3 rounded-xl text-center" style="background:rgba(16,185,129,.08);border:1px solid #10b98130">
                            <div class="text-[8px] font-black text-emerald-400 uppercase tracking-widest mb-1">20 m DX</div>
                            <div class="font-mono text-sm font-black text-emerald-300">14.233</div>
                            <div class="text-[8px] text-slate-500">USB — PD180</div>
                        </div>
                        <div class="p-3 rounded-xl text-center" style="background:rgba(99,102,241,.08);border:1px solid #6366f130">
                            <div class="text-[8px] font-black text-indigo-400 uppercase tracking-widest mb-1">VIS sync</div>
                            <div class="font-mono text-sm font-black text-indigo-300">1900 Hz</div>
                            <div class="text-[8px] text-slate-500">Leader signal</div>
                        </div>
                    </div>
                </div>

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

                <!-- Best DX + Distance max -->
                <div class="xl:col-span-3 grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <!-- Best DX -->
                    <div class="glass rounded-2xl p-5">
                        <div class="text-[10px] font-black text-rose-400 uppercase tracking-widest mb-3 flex items-center justify-between"><span>🏅 Meilleur DX entendu</span><button onclick="window._resetDx&&window._resetDx()" class="text-[9px] font-mono text-slate-500 hover:text-rose-400 transition-colors border border-slate-700 rounded-lg px-2 py-0.5">🗑️ reset</button></div>
                        <div class="flex items-center justify-between gap-4">
                            <div>
                                <div id="stats-bestdx-cs" class="text-xl font-black text-rose-300 font-mono">–</div>
                                <div class="text-[9px] font-mono text-slate-500 mt-0.5" id="stats-bestdx-date"></div>
                            </div>
                            <div class="text-right">
                                <div id="stats-bestdx-dist" class="text-2xl font-black text-rose-400">–</div>
                                <div id="stats-bestdx-brng" class="text-[10px] font-mono text-slate-400 mt-0.5"></div>
                            </div>
                        </div>
                    </div>
                    <!-- Distance max -->
                    <div class="glass rounded-2xl p-5 text-center flex flex-col items-center justify-center">
                        <div class="text-[10px] font-black text-pink-400 uppercase tracking-widest mb-2 flex items-center justify-between w-full"><span>📡 Distance max atteinte</span><button onclick="window._resetDx&&window._resetDx()" class="text-[9px] font-mono text-slate-500 hover:text-pink-400 transition-colors border border-slate-700 rounded-lg px-2 py-0.5">🗑️ reset</button></div>
                        <div id="stats-bestdx-dist-badge" class="text-4xl font-black text-pink-300">–</div>
                        <div class="text-[9px] font-mono text-slate-500 mt-1">toutes trames de position RX/IS</div>
                    </div>
                </div>

                <!-- Maillage RF (cases Maidenhead entendues) -->
                <div class="xl:col-span-3 glass rounded-[2rem] overflow-hidden shadow-2xl" id="stats-mesh-panel">
                    <div class="px-5 py-3 bg-slate-900/50 border-b border-slate-800 flex items-center justify-between">
                        <div class="flex items-center gap-3">
                            <span class="text-[10px] font-black text-cyan-400 uppercase tracking-widest">🗺️ Maillage RF — cases Maidenhead entendues</span>
                            <span id="stats-mesh-count" class="text-[9px] font-mono text-slate-500 bg-slate-800 px-2 py-0.5 rounded-full">–</span>
                        </div>
                        <button onclick="(function(){_gridCells={};fetch('/stats/reset_mesh',{method:'POST'});_renderMesh();document.getElementById('stats-mesh-count').textContent='0 case';})()" class="text-[9px] font-mono text-slate-500 hover:text-rose-400 transition-colors border border-slate-700 rounded-lg px-2 py-0.5">🗑️ reset</button>
                    </div>
                    <div class="p-4">
                        <svg id="stats-mesh-svg" viewBox="0 0 700 380" style="width:100%;display:block;" xmlns="http://www.w3.org/2000/svg">
                            <text x="350" y="195" text-anchor="middle" font-size="11" fill="#475569">En attente de positions…</text>
                        </svg>
                        <div class="flex flex-wrap gap-4 justify-center mt-2 text-[9px] font-mono text-slate-500">
                            <span class="flex items-center gap-1"><span style="display:inline-block;width:12px;height:12px;background:rgba(30,160,200,0.35);border:1px solid rgba(56,189,248,0.3);border-radius:2px"></span>1–2 trames</span>
                            <span class="flex items-center gap-1"><span style="display:inline-block;width:12px;height:12px;background:rgba(40,200,220,0.6);border:1px solid rgba(56,189,248,0.3);border-radius:2px"></span>fréquent</span>
                            <span class="flex items-center gap-1"><span style="display:inline-block;width:12px;height:12px;background:rgba(50,240,200,0.75);border:1px solid rgba(56,189,248,0.3);border-radius:2px"></span>très actif</span>
                            <span class="flex items-center gap-1"><span style="display:inline-block;width:14px;height:2px;background:#f59e0b;border-radius:1px"></span>Ma station</span>
                            <span class="flex items-center gap-1"><span style="display:inline-block;width:14px;height:1px;border-top:1px dashed #475569"></span>50 / 100 / 200 km</span>
                        </div>
                    </div>
                </div>

                <!-- Historique météo 48h -->
                <div class="xl:col-span-3 glass rounded-[2rem] overflow-hidden shadow-2xl" id="wxhist-panel">
                    <div class="px-5 py-3 bg-slate-900/50 border-b border-slate-800 flex items-center justify-between">
                        <span class="text-[10px] font-black text-sky-400 uppercase tracking-widest">🌤️ Météo locale — 48h</span>
                        <span id="wxhist-status" class="text-[9px] font-mono text-slate-600">chargement…</span>
                    </div>
                    <div class="p-4 space-y-4">
                        <!-- Graphe température -->
                        <div>
                            <div class="text-[9px] font-mono text-orange-400 uppercase tracking-widest mb-1">🌡️ Température (°C)</div>
                            <svg id="wxhist-temp" viewBox="0 0 800 90" style="width:100%;display:block;" xmlns="http://www.w3.org/2000/svg">
                                <text x="400" y="50" text-anchor="middle" font-size="10" fill="#475569">Chargement…</text>
                            </svg>
                        </div>
                        <!-- Graphe vent -->
                        <div>
                            <div class="text-[9px] font-mono text-sky-400 uppercase tracking-widest mb-1">💨 Vent (km/h)</div>
                            <svg id="wxhist-wind" viewBox="0 0 800 90" style="width:100%;display:block;" xmlns="http://www.w3.org/2000/svg">
                                <text x="400" y="50" text-anchor="middle" font-size="10" fill="#475569">Chargement…</text>
                            </svg>
                        </div>
                        <!-- Graphe précipitations -->
                        <div>
                            <div class="text-[9px] font-mono text-blue-400 uppercase tracking-widest mb-1">🌧️ Précipitations (mm)</div>
                            <svg id="wxhist-rain" viewBox="0 0 800 90" style="width:100%;display:block;" xmlns="http://www.w3.org/2000/svg">
                                <text x="400" y="50" text-anchor="middle" font-size="10" fill="#475569">Chargement…</text>
                            </svg>
                        </div>
                        <!-- Graphe orage LPI + CAPE -->
                        <div>
                            <div class="flex items-center justify-between mb-1">
                                <span class="text-[9px] font-mono text-yellow-400 uppercase tracking-widest">⚡ Potentiel orage — LPI (J/kg) &amp; CAPE (J/kg)</span>
                                <span id="wxhist-lightning-max" class="text-[9px] font-mono text-yellow-300 opacity-70"></span>
                            </div>
                            <svg id="wxhist-lightning" viewBox="0 0 800 90" style="width:100%;display:block;" xmlns="http://www.w3.org/2000/svg">
                                <text x="400" y="50" text-anchor="middle" font-size="10" fill="#475569">Chargement…</text>
                            </svg>
                            <div class="flex gap-4 mt-1">
                                <span class="text-[9px] font-mono flex items-center gap-1">
                                    <span style="display:inline-block;width:10px;height:10px;background:#facc15;border-radius:2px"></span>LPI (axe gauche)
                                </span>
                                <span class="text-[9px] font-mono flex items-center gap-1">
                                    <span style="display:inline-block;width:14px;height:2px;background:#f97316"></span>CAPE (axe droit)
                                </span>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Alertes Propagation & Blackout HF -->
                <div class="xl:col-span-3 glass rounded-[2rem] overflow-hidden shadow-2xl" id="propalert-hist-panel">
                    <div class="px-5 py-3 bg-slate-900/50 border-b border-slate-800 flex items-center justify-between">
                        <span class="text-[10px] font-black text-orange-400 uppercase tracking-widest">☀️ Alertes Propagation &amp; Blackout HF</span>
                        <div class="flex items-center gap-3">
                            <button onclick="loadPropAlertHistory()" class="text-[9px] font-mono text-slate-500 hover:text-orange-400 transition-colors px-2 py-0.5 rounded border border-slate-700 hover:border-orange-700">↺ rafraîchir</button>
                            <span id="propalert-hist-status" class="text-[9px] font-mono text-slate-600">–</span>
                        </div>
                    </div>
                    <div class="p-4">
                        <div id="propalert-hist-list" class="space-y-2">
                            <p class="text-slate-600 text-[10px] italic text-center py-4">Aucune alerte enregistrée depuis le démarrage.</p>
                        </div>
                    </div>
                </div>

                <!-- Mon signal sur le réseau APRS -->
                <div class="xl:col-span-3 glass rounded-[2rem] overflow-hidden shadow-2xl" id="sigpath-panel">
                    <div class="px-5 py-3 bg-slate-900/50 border-b border-slate-800 flex items-center justify-between">
                        <span class="text-[10px] font-black text-emerald-400 uppercase tracking-widest">📡 Mon signal — chemins réseau APRS</span>
                        <div class="flex items-center gap-3">
                            <span id="sigpath-callsign" class="text-[9px] font-mono text-slate-500"></span>
                            <button onclick="loadSignalPath(true)" class="text-[9px] font-mono text-slate-500 hover:text-emerald-400 transition-colors px-2 py-0.5 rounded border border-slate-700 hover:border-emerald-700">↺ rafraîchir</button>
                            <span id="sigpath-status" class="text-[9px] font-mono text-slate-600">–</span>
                        </div>
                    </div>
                    <div class="p-4 space-y-4">

                        <!-- Dernier chemin + compteurs digis -->
                        <div class="grid grid-cols-1 xl:grid-cols-2 gap-4">

                            <!-- Dernier chemin complet -->
                            <div>
                                <div class="text-[9px] font-mono text-slate-500 uppercase tracking-widest mb-2">🔗 Dernier chemin observé</div>
                                <div id="sigpath-last" class="flex flex-wrap items-center gap-1 min-h-[32px]">
                                    <span class="text-slate-600 text-[10px] italic">En attente de données…</span>
                                </div>
                            </div>

                            <!-- Top digis -->
                            <div>
                                <div class="text-[9px] font-mono text-slate-500 uppercase tracking-widest mb-2">🏆 Digipeaté par (24h)</div>
                                <div id="sigpath-digis" class="space-y-1.5"></div>
                            </div>
                        </div>

                        <!-- Historique des chemins récents -->
                        <div>
                            <div class="text-[9px] font-mono text-slate-500 uppercase tracking-widest mb-2">📋 Historique des chemins (20 derniers)</div>
                            <div id="sigpath-history" class="space-y-1 max-h-64 overflow-y-auto pr-1"></div>
                        </div>

                    </div>
                </div>


            </div>
        </div>

        <!-- ═══════════════════════════ NOTES ════════════════════════════════════ -->
        <div id="tab-notes" class="hidden">
            <div class="max-w-3xl mx-auto space-y-4">

                <!-- Titre -->
                <div class="flex items-center justify-between mb-2">
                    <h2 class="text-[11px] font-black text-slate-400 uppercase tracking-widest flex items-center gap-2">
                        📝 <span>Notes de l'opérateur</span>
                    </h2>
                    <span id="notes-char-count"
                          style="font-size:9px;font-family:monospace;color:#334155">0 car.</span>
                </div>

                <!-- Éditeur -->
                <div class="glass rounded-2xl p-4" style="border:1px solid rgba(148,163,184,0.1)">
                    <textarea id="notes-editor"
                        placeholder="Notes libres — fréquences, contacts, rappels, infos QSO…"
                        spellcheck="false"
                        style="
                            width:100%; min-height:420px; resize:vertical;
                            background:transparent; border:none; outline:none;
                            color:#e2e8f0; font-family:'JetBrains Mono','Fira Mono',monospace;
                            font-size:13px; line-height:1.7; padding:4px 0;
                            caret-color:#60a5fa;
                        "
                    ></textarea>
                </div>

                <!-- Barre d'actions -->
                <div class="flex items-center gap-3 flex-wrap">
                    <button id="notes-save-btn"
                            onclick="_notesSave()"
                            class="px-4 py-2 rounded-xl font-bold text-[11px] uppercase tracking-wider
                                   bg-blue-600 hover:bg-blue-500 text-white transition-all shadow-lg">
                        💾 Sauvegarder
                    </button>
                    <button onclick="_notesCopy()"
                            class="px-4 py-2 rounded-xl font-bold text-[11px] uppercase tracking-wider
                                   bg-slate-700 hover:bg-slate-600 text-slate-200 transition-all">
                        📋 Copier
                    </button>
                    <button onclick="_notesClear()"
                            class="px-4 py-2 rounded-xl font-bold text-[11px] uppercase tracking-wider
                                   bg-slate-800 hover:bg-red-900 text-slate-400 hover:text-red-300 transition-all">
                        🗑 Effacer
                    </button>
                    <span id="notes-status"
                          style="font-size:10px;font-family:monospace;color:#334155;margin-left:auto"></span>
                </div>

                <!-- Info persistance -->
                <p class="text-[9px] text-slate-600 text-center mt-2">
                    Sauvegarde automatique dans le navigateur (localStorage) · données locales uniquement
                </p>

            </div>
        </div>
        <!-- ═══════════════════════════ LOGBOOK ═══════════════════════════════ -->

</div>

<script>
    var txCount = 0;

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

    // # MONITOR_UI_PATCH_APPLIED
    function badge(label, value, cls, bgCls) {
        cls   = cls   || 'text-slate-300';
        bgCls = bgCls || 'bg-slate-800/80';
        return '<span class="inline-flex items-center gap-1 ' + bgCls + ' rounded-md px-2 py-0.5 text-[10px] font-mono border border-slate-700/40">'
             + '<span class="text-slate-500 text-[9px] uppercase tracking-wide">' + esc(label) + '</span>'
             + '<span class="' + cls + ' font-semibold">' + esc(String(value)) + '</span>'
             + '</span>';
    }

    // ── Coordonnées station locale (depuis la config serveur) ──────────────
    var _stationLat = null, _stationLon = null;
    (function() {
        try {
            var cfg = __CFG_JSON__;
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

    // callsign → distKm et bearing calculés depuis une trame de position propre (pas Objet)
    var _stationPosDistKm  = {};
    var _stationPosBearing = {};   // callsign → azimut (°) vers la station distante

    function haversineKm(lat1, lon1, lat2, lon2) {
        if (lat1 == null || lon1 == null || lat2 == null || lon2 == null) return null;
        if (isNaN(lat1) || isNaN(lon1) || isNaN(lat2) || isNaN(lon2)) return null;
        if (Math.abs(lat1) > 90 || Math.abs(lat2) > 90) return null;
        if (Math.abs(lon1) > 180 || Math.abs(lon2) > 180) return null;
        var R = 6371;
        var dLat = (lat2 - lat1) * Math.PI / 180;
        var dLon = (lon2 - lon1) * Math.PI / 180;
        var a = Math.sin(dLat/2) * Math.sin(dLat/2)
              + Math.cos(lat1 * Math.PI/180) * Math.cos(lat2 * Math.PI/180)
              * Math.sin(dLon/2) * Math.sin(dLon/2);
        return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
    }

    /** Azimut vrai (initial bearing) de (lat1,lon1) vers (lat2,lon2), en degrés 0-360. */
    function bearingDeg(lat1, lon1, lat2, lon2) {
        var toRad = Math.PI / 180;
        var dLon  = (lon2 - lon1) * toRad;
        var φ1    = lat1 * toRad, φ2 = lat2 * toRad;
        var y     = Math.sin(dLon) * Math.cos(φ2);
        var x     = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(dLon);
        return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
    }

    /** Rose des vents 16 secteurs → flèche Unicode pointant vers la cible. */
    function bearingArrow(deg) {
        // flèches pointant dans la direction d'émission (N=↑, E=→, S=↓, O=←)
        var arrows = ['↑','↗','→','↘','↓','↙','←','↖'];
        return arrows[Math.round((deg % 360) / 45) % 8];
    }

    function buildFields(frame) {
        var e = frame.extra || {};
        var parts = [];
        if (e.lat !== undefined && e.lon !== undefined) {
            var _posBadgeId = 'city-' + (frame.src || 'x').replace(/[^a-z0-9]/gi,'_')
                              + '_' + Date.now() + '_' + Math.random().toString(36).slice(2,6);
            parts.push('<span style="display:inline-flex;align-items:center;gap:4px;background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.2);border-radius:6px;padding:2px 7px;font-family:monospace;font-size:10px">'
                     + '<span style="color:#10b981;font-size:8px;font-weight:900;text-transform:uppercase;letter-spacing:.08em">📍pos</span>'
                     + coordLink(e.lat, e.lon) + '</span>');
            // Badge ville (injecté de façon asynchrone après geocoding)
            parts.push('<span id="' + _posBadgeId + '" style="display:inline-flex;align-items:center;gap:4px;background:rgba(14,165,233,0.08);border:1px solid rgba(14,165,233,0.18);border-radius:6px;padding:2px 7px;font-size:10px;color:#7dd3fc;font-style:italic">🏙️&nbsp;<span style="opacity:.5">…</span></span>');
            // Requête asynchrone vers /nearest_city (via proxy serveur → Nominatim)
            (function(badgeId, lat, lon) {
                fetch('/nearest_city?lat=' + lat + '&lon=' + lon)
                    .then(function(r){ return r.ok ? r.json() : null; })
                    .then(function(d) {
                        var el = document.getElementById(badgeId);
                        if (!el) return;
                        if (d && d.display) {
                            el.innerHTML = '🏙️&nbsp;<span style="color:#bae6fd;font-style:normal">' + esc(d.display) + '</span>';
                            el.title = d.city + (d.state ? ', ' + d.state : '') + (d.country ? ' (' + d.country + ')' : '');
                        } else {
                            el.style.display = 'none';
                        }
                    })
                    .catch(function(){ var el=document.getElementById(badgeId); if(el) el.style.display='none'; });
            })(_posBadgeId, e.lat, e.lon);
            if (_stationLat !== null && _stationLon !== null) {
                var _isObj = (frame.aprs_type === 'Objet');
                var _src   = (frame.src || '').toUpperCase();
                var _dist, _brng;
                if (_isObj && _stationPosDistKm[_src] !== undefined) {
                    _dist = _stationPosDistKm[_src];
                    _brng = _stationPosBearing[_src];
                } else {
                    _dist = haversineKm(_stationLat, _stationLon, e.lat, e.lon);
                    _brng = (_dist !== null) ? bearingDeg(_stationLat, _stationLon, e.lat, e.lon) : undefined;
                    if (!_isObj && _dist !== null) {
                        _stationPosDistKm[_src]  = _dist;
                        _stationPosBearing[_src] = _brng;
                    }
                }
                if (_dist !== null && !isNaN(_dist)) {
                    var _distStr = _dist < 10 ? _dist.toFixed(1) + ' km' : Math.round(_dist) + ' km';
                    var _distCls = _dist < 50 ? 'text-emerald-300' : _dist < 150 ? 'text-yellow-300' : 'text-orange-300';
                    var _brngStr = (_brng !== undefined && !isNaN(_brng)) ? bearingArrow(_brng) + ' ' + Math.round(_brng) + '°' : '';
                    var _bearingBadge = _brngStr ? badge('🧭', _distStr + ' · ' + _brngStr, _distCls) : badge('📏', _distStr, _distCls);
                    if (_isObj && _stationPosDistKm[_src] !== undefined) {
                        _bearingBadge = _brngStr
                            ? badge('🧭', _distStr + ' · ' + _brngStr + ' ·sta', _distCls)
                            : badge('📏', _distStr + ' ·sta', _distCls);
                    }
                    parts.push(_bearingBadge);
                }
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
        if (e.msg_dest) parts.push(badge('vers', e.msg_dest, 'text-indigo-300', 'bg-indigo-950/40'));
        if (e.msg_text) parts.push('<span style="display:inline-flex;align-items:center;gap:4px;background:rgba(99,102,241,0.12);border:1px solid rgba(99,102,241,0.25);border-radius:6px;padding:3px 9px;font-size:11px;color:#c7d2fe;font-style:italic">'
            + '💬 ' + esc(e.msg_text) + '</span>');

        // ── Champs météo ──────────────────────────────────────────────────
        var isWx = (frame.aprs_type === 'Meteo' || frame.aprs_type === 'Meteo Peet');
        if (e.temp_c        !== undefined) parts.push(badge('🌡️', e.temp_c.toFixed(1) + ' °C', 'text-orange-300', 'bg-orange-950/30'));
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
                .replace(/\b\d+\.\s{0,}/g, '')   // numéros résiduels type "36."
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
        var now = new Date();
        var timeStr = now.toLocaleTimeString('fr-FR', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
        var isTX = type === 'TX';
        var isIS = frame._source === 'IS';

        // ── Couleurs par direction ────────────────────────────────────────
        var accentColor, dirBg, dirText, dirLabel, dirBorder;
        if (isTX) {
            accentColor = '#3b82f6'; dirBg = 'rgba(59,130,246,0.08)';
            dirText = '#93c5fd'; dirLabel = '📤 TX';
            dirBorder = 'rgba(59,130,246,0.3)';
        } else if (isIS) {
            accentColor = '#8b5cf6'; dirBg = 'rgba(139,92,246,0.08)';
            dirText = '#c4b5fd'; dirLabel = '🌐 IS';
            dirBorder = 'rgba(139,92,246,0.3)';
        } else {
            accentColor = '#10b981'; dirBg = 'rgba(16,185,129,0.06)';
            dirText = '#6ee7b7'; dirLabel = '📥 RX';
            dirBorder = 'rgba(16,185,129,0.25)';
        }

        // ── Type APRS : couleur de fond du badge ─────────────────────────
        var typeColors = {
            'Message':'rgba(99,102,241,0.15)', 'Position':'rgba(16,185,129,0.10)',
            'Meteo':'rgba(14,165,233,0.12)', 'Meteo Peet':'rgba(14,165,233,0.12)',
            'Objet':'rgba(245,158,11,0.12)', 'Mic-E':'rgba(236,72,153,0.10)',
            'Telemetrie':'rgba(168,85,247,0.10)', 'Statut':'rgba(251,191,36,0.08)',
            'Beacon ISS':'rgba(139,92,246,0.15)'
        };
        var typeBg = 'rgba(30,41,59,0.8)';
        for (var tk in typeColors) {
            if (frame.aprs_type && frame.aprs_type.indexOf(tk) !== -1) { typeBg = typeColors[tk]; break; }
        }

        // ── Ligne d'en-tête ───────────────────────────────────────────────
        var destPath = '';
        if (frame.dest) {
            destPath += '<span style="color:#475569;font-size:9px;margin:0 3px">▸</span>'
                     +  '<span style="background:#0f172a;border:1px solid #1e293b;border-radius:5px;padding:1px 6px;font-family:monospace;font-size:10px;color:#64748b">'
                     +  esc(frame.dest) + '</span>';
        }
        if (frame.path) {
            destPath += '<span style="color:#334155;font-size:9px;font-family:monospace;margin-left:3px">' + esc(frame.path) + '</span>';
        }

        var typeBadge = '<span class="aprs-type-badge" style="display:inline-flex;align-items:center;gap:3px;background:' + typeBg + ';border:1px solid rgba(148,163,184,0.1);border-radius:6px;padding:2px 7px;font-size:9px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;white-space:nowrap">'
                      + aprsTypeEmoji(frame.aprs_type) + ' ' + esc(frame.aprs_type || '?') + '</span>';

        var dirBadge = '<span class="aprs-dir-badge" style="display:inline-flex;align-items:center;padding:2px 8px;border-radius:5px;font-size:9px;font-weight:900;letter-spacing:.1em;background:' + dirBg + ';color:' + dirText + ';border:1px solid ' + dirBorder + '">' + dirLabel + '</span>';

        var header = '<div class="aprs-card-hdr" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;justify-content:space-between">'
            + '<div class="aprs-card-hdr-left" style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;min-width:0">'
            + dirBadge
            + qrzLink(frame.src || '?', {cls:'aprs-cs-link text-[11px] font-black font-mono px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-100 hover:text-blue-300 transition-colors border border-slate-700/50'})
            + '<span class="aprs-dest-path" style="display:inline-flex;align-items:center;gap:3px">' + destPath + '</span>'
            + '</div>'
            + '<div class="aprs-card-hdr-right" style="display:flex;align-items:center;gap:6px;flex-shrink:0">'
            + typeBadge
            + '<span class="aprs-time" style="font-size:9px;font-family:monospace;color:#475569;white-space:nowrap">' + timeStr + '</span>'
            + '</div>'
            + '</div>';

        // ── Champs décodés ────────────────────────────────────────────────
        var fieldsHtml = buildFields(frame);

        // ── Trame brute repliable ─────────────────────────────────────────
        var rawHtml = '';
        if (frame.payload && frame.aprs_type !== 'Telemetrie') {
            var rawTrunc = esc(frame.payload.substring(0, 200));
            rawHtml = '<div style="margin-top:6px">'
                + '<button class="raw-toggle" style="background:none;border:none;padding:0;font-size:9px;color:#334155;cursor:pointer;font-family:monospace;font-weight:700;text-transform:uppercase;letter-spacing:.08em;min-height:unset">'
                + '\u22ef trame brute</button>'
                + '<div class="raw-body" style="display:none;margin-top:4px;padding:6px 8px;background:#040d18;border-radius:6px;border:1px solid #1e293b;font-family:monospace;font-size:10px;color:#475569;word-break:break-all;white-space:pre-wrap;line-height:1.5">'
                + rawTrunc + '</div>'
                + '</div>';
        }

        // ── Assemblage ────────────────────────────────────────────────────
        var div = document.createElement('div');
        div.className = 'rx-entry aprs-card';
        div.style.cssText = 'border-left:3px solid ' + accentColor + ';background:rgba(15,23,42,0.55);'
            + 'border-radius:12px;border:1px solid rgba(30,41,59,0.9);border-left-width:3px;border-left-color:' + accentColor + ';'
            + 'padding:10px 14px;animation:aprsCardIn .18s ease-out';
        div.innerHTML = header
            + (fieldsHtml ? '<div class="aprs-fields" style="margin-top:7px">' + fieldsHtml.replace(/^<div[^>]*>/, '').replace(/<\/div>$/, '') + '</div>' : '')
            + rawHtml;
        con.prepend(div);

        // Limiter à 200 entrées pour les perfs
        var children = con.children;
        while (children.length > 202) con.removeChild(children[children.length - 1]);
    }

    // ── Restauration de l'historique après F5 ─────────────────────────────
    var rxFrameCount = 0;
    var _seenFids = new Set();   // fids déjà affichés via rx_history  # PATCH_DISPLAY_DEDUP_v1
    // Déduplication par contenu (src + payload) sur 45 s — évite les doubles
    // affichages quand une même trame arrive via plusieurs chemins (digipeat).
    var _contentDedup = {};  // key → timestamp_ms
    var _CONTENT_TTL  = 45000; // 45 secondes
    function _contentKey(frame) {
        var p = (frame.payload || '').substring(0, 80);
        return (frame.src || '') + '|' + p;
    }
    var _dupPurgeTimer = 0;
    function _isDupContent(frame) {
        var k = _contentKey(frame);
        var now = Date.now();
        // Purger les entrées expirées toutes les 60 s seulement (pas à chaque trame)
        if (now - _dupPurgeTimer > 60000) {
            _dupPurgeTimer = now;
            Object.keys(_contentDedup).forEach(function(ck) {
                if (now - _contentDedup[ck] > _CONTENT_TTL) delete _contentDedup[ck];
            });
            // Purger _seenFids si trop grand
            if (_seenFids.size > 800) {
                var _it = _seenFids.values();
                for (var _i = 0; _i < 400; _i++) { var nx = _it.next(); if (!nx.done) _seenFids.delete(nx.value); }
            }
        }
        if (_contentDedup.hasOwnProperty(k)) return true;
        _contentDedup[k] = now;
        return false;
    }

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
        var _bestDx       = null;   // {cs, distKm, brng, ts}
        var _maxDistKm    = 0;
        var _gridCells    = {};     // grid4 → {count, stations:{cs:1}, last, lat, lon}
    
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
            // ── Best DX (RF uniquement — exclut IS/Internet) ─────────────────
            if (type === 'RX' && _stationLat !== null && _stationLon !== null && frame && frame.extra) {
                var _ex = frame.extra;
                if (_ex.lat !== undefined && _ex.lon !== undefined && frame.aprs_type !== 'Objet') {
                    var _d = haversineKm(_stationLat, _stationLon, _ex.lat, _ex.lon);
                    if (_d !== null && _d > 0) {
                        if (_bestDx === null || _d > _bestDx.distKm) {
                            var _br = bearingDeg(_stationLat, _stationLon, _ex.lat, _ex.lon);
                            _bestDx = {cs: frame.src || '?', distKm: _d, brng: Math.round(_br), ts: Date.now()};
                            _renderBestDx();
                        }
                    }
                }
            }
            // ── Distance max toutes trames (RX + IS) ──────────────────────────
            if ((type === 'RX' || type === 'IS') && _stationLat !== null && _stationLon !== null && frame && frame.extra) {
                var _exm = frame.extra;
                if (_exm.lat !== undefined && _exm.lon !== undefined && frame.aprs_type !== 'Objet') {
                    var _dm = haversineKm(_stationLat, _stationLon, _exm.lat, _exm.lon);
                    if (_dm !== null && _dm > _maxDistKm) {
                        _maxDistKm = _dm;
                        _renderMaxDist();
                    }
                }
            }
            // ── Grille Maidenhead (maillage) ─────────────────────────────────
            if (frame && frame.extra) {
                var _ex2 = frame.extra;
                if (_ex2.lat !== undefined && _ex2.lon !== undefined && frame.aprs_type !== 'Objet') {
                    var _g4 = _latLonToGrid4(_ex2.lat, _ex2.lon);
                    if (_g4) {
                        if (!_gridCells[_g4]) {
                            _gridCells[_g4] = {count:0, stations:{}, last:0,
                                               lat:_ex2.lat, lon:_ex2.lon};
                        }
                        _gridCells[_g4].count++;
                        if (src) _gridCells[_g4].stations[src] = 1;
                        _gridCells[_g4].last = Date.now();
                    }
                }
            }
            // Mise à jour différée (pas à chaque trame pour les perfs)
            _statsDirty = true;
            if (window._statsRenderTimer) clearTimeout(window._statsRenderTimer);
            window._statsRenderTimer = setTimeout(_statsRender, 800);
        };
    
        // ── Maidenhead helpers ────────────────────────────────────────────
        function _latLonToGrid4(lat, lon) {
            try {
                var lo = lon + 180, la = lat + 90;
                var A = String.fromCharCode(65 + Math.floor(lo / 20));
                var B = String.fromCharCode(65 + Math.floor(la / 10));
                var C = String(Math.floor((lo % 20) / 2));
                var D = String(Math.floor(la % 10));
                return A + B + C + D;
            } catch(e) { return null; }
        }

        function _grid4Center(g4) {
            // Retourne {lat, lon} centre de la case Maidenhead 4 car.
            g4 = g4.toUpperCase();
            var lon = (g4.charCodeAt(0) - 65) * 20 - 180 + parseInt(g4[2]) * 2 + 1;
            var lat = (g4.charCodeAt(1) - 65) * 10 - 90  + parseInt(g4[3]) + 0.5;
            return {lat: lat, lon: lon};
        }

        // ── Rendu carte de maillage ───────────────────────────────────────
        function _renderMesh() {
            var svg = document.getElementById('stats-mesh-svg');
            if (!svg) return;
            var keys = Object.keys(_gridCells);

            // Résumé texte
            var elCount = document.getElementById('stats-mesh-count');
            if (elCount) elCount.textContent = keys.length + ' case' + (keys.length > 1 ? 's' : '');

            if (!keys.length || _stationLat === null || _stationLon === null) {
                svg.innerHTML = '<text x="50%" y="50%" text-anchor="middle" dominant-baseline="middle" font-size="11" fill="#475569">En attente de positions…</text>';
                return;
            }

            // Dimensions
            var W = 700, H = 380;
            var PL = 30, PR = 10, PT = 14, PB = 24;
            var cW = W - PL - PR, cH = H - PT - PB;

            // Étendue géographique : centrer sur la station, ±SPAN degrés
            var SPAN_LON = 12, SPAN_LAT = 7;   // fenêtre ±
            var lonMin = _stationLon - SPAN_LON, lonMax = _stationLon + SPAN_LON;
            var latMin = _stationLat - SPAN_LAT, latMax = _stationLat + SPAN_LAT;

            function toX(lon) { return PL + (lon - lonMin) / (lonMax - lonMin) * cW; }
            function toY(lat) { return PT + (1 - (lat - latMin) / (latMax - latMin)) * cH; }

            // Largeur/hauteur d'une case Maidenhead 4 car. en pixels
            var cellW = Math.abs(toX(lonMin + 2) - toX(lonMin));
            var cellH = Math.abs(toY(latMin + 1) - toY(latMin));

            // Densité max pour l'échelle de couleur
            var maxCount = 1;
            keys.forEach(function(k) { if (_gridCells[k].count > maxCount) maxCount = _gridCells[k].count; });

            var out = '';

            // ── Grille de fond (toutes les 2° lon / 1° lat dans la fenêtre) ──
            for (var glon = Math.ceil(lonMin / 2) * 2; glon < lonMax; glon += 2) {
                var x = toX(glon);
                if (x < PL || x > W - PR) continue;
                out += '<line x1="' + x.toFixed(1) + '" y1="' + PT + '" x2="' + x.toFixed(1) + '" y2="' + (PT + cH) + '" stroke="#1e293b" stroke-width="0.5"/>';
                out += '<text x="' + x.toFixed(1) + '" y="' + (PT + cH + 13) + '" text-anchor="middle" font-size="7" fill="#334155">' + glon + '°</text>';
            }
            for (var glat = Math.ceil(latMin); glat < latMax; glat++) {
                var y = toY(glat);
                if (y < PT || y > PT + cH) continue;
                out += '<line x1="' + PL + '" y1="' + y.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + y.toFixed(1) + '" stroke="#1e293b" stroke-width="0.5"/>';
                out += '<text x="' + (PL - 3) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end" font-size="7" fill="#334155">' + glat + '°</text>';
            }

            // ── Cases entendues ───────────────────────────────────────────
            keys.forEach(function(g4) {
                var c   = _grid4Center(g4);
                var cx  = toX(c.lon - 1);   // coin SO : lon centre - 1°
                var cy  = toY(c.lat + 0.5); // coin SO : lat centre + 0.5°
                // Filtrer les cases hors fenêtre
                if (cx + cellW < PL || cx > W - PR || cy + cellH < PT || cy > PT + cH) return;
                var cell = _gridCells[g4];
                var t    = cell.count / maxCount;              // 0..1
                // Couleur : bleu pâle → cyan → vert clair selon densité
                var r = Math.round(30  + t * 20);
                var g = Math.round(140 + t * 100);
                var b = Math.round(200 + (1-t) * 55);
                var alpha = (0.25 + t * 0.55).toFixed(2);
                var fill  = 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
                var nSta  = Object.keys(cell.stations).length;
                var title = g4 + ' · ' + cell.count + ' trame' + (cell.count>1?'s':'')
                          + ' · ' + nSta + ' sta' + (nSta>1?'tions':'tion');
                out += '<rect x="' + cx.toFixed(1) + '" y="' + cy.toFixed(1)
                     + '" width="' + cellW.toFixed(1) + '" height="' + cellH.toFixed(1)
                     + '" fill="' + fill + '" stroke="rgba(56,189,248,0.3)" stroke-width="0.8" rx="1">'
                     + '<title>' + title + '</title></rect>';
                // Label indicatif si case assez grande
                if (cellW > 28) {
                    var lx = (cx + cellW / 2).toFixed(1);
                    var ly = (cy + cellH / 2 + 3).toFixed(1);
                    out += '<text x="' + lx + '" y="' + ly + '" text-anchor="middle" font-size="8" fill="rgba(186,230,253,0.85)" font-family="monospace">' + g4 + '</text>';
                    if (cell.count > 1 && cellH > 14) {
                        out += '<text x="' + lx + '" y="' + (parseFloat(ly)+9).toFixed(1) + '" text-anchor="middle" font-size="7" fill="rgba(148,163,184,0.7)">' + cell.count + '</text>';
                    }
                }
            });

            // ── Station locale (croix) ────────────────────────────────────
            var sx = toX(_stationLon), sy = toY(_stationLat);
            out += '<line x1="' + (sx-7) + '" y1="' + sy + '" x2="' + (sx+7) + '" y2="' + sy + '" stroke="#f59e0b" stroke-width="2"/>';
            out += '<line x1="' + sx + '" y1="' + (sy-7) + '" x2="' + sx + '" y2="' + (sy+7) + '" stroke="#f59e0b" stroke-width="2"/>';
            out += '<circle cx="' + sx + '" cy="' + sy + '" r="4" fill="none" stroke="#f59e0b" stroke-width="1.5"/>';

            // ── Cercles de distance (50 / 100 / 200 km) ──────────────────
            var KM_RINGS = [50, 100, 200];
            KM_RINGS.forEach(function(km) {
                var dLat = km / 111.0;
                var dLon = km / (111.0 * Math.cos(_stationLat * Math.PI / 180));
                var rx   = Math.abs(toX(_stationLon + dLon) - sx);
                var ry   = Math.abs(toY(_stationLat + dLat) - sy);
                if (rx < 3 || ry < 3) return;
                out += '<ellipse cx="' + sx.toFixed(1) + '" cy="' + sy.toFixed(1)
                     + '" rx="' + rx.toFixed(1) + '" ry="' + ry.toFixed(1)
                     + '" fill="none" stroke="rgba(71,85,105,0.5)" stroke-width="0.8" stroke-dasharray="3,4"/>';
                out += '<text x="' + (sx + rx + 2).toFixed(1) + '" y="' + (sy - 2).toFixed(1)
                     + '" font-size="7" fill="#475569">' + km + ' km</text>';
            });

            svg.innerHTML = out;
        }

        // ── Rendu ─────────────────────────────────────────────────────────
        function _renderBestDx() {
            var elCs   = document.getElementById('stats-bestdx-cs');
            var elDist = document.getElementById('stats-bestdx-dist');
            var elBrng = document.getElementById('stats-bestdx-brng');
            var elDate = document.getElementById('stats-bestdx-date');
            if (!elCs) return;
            if (!_bestDx) {
                elCs.textContent   = '–';
                elDist.textContent = '–';
                elBrng.textContent = '';
                if (elDate) elDate.textContent = '';
                return;
            }
            elCs.innerHTML = qrzLink(_bestDx.cs, {cls:'font-mono font-black text-rose-300 hover:text-rose-200 transition-colors'});
            elDist.textContent = _bestDx.distKm < 10 ? _bestDx.distKm.toFixed(1) + ' km' : Math.round(_bestDx.distKm) + ' km';
            elBrng.textContent = bearingArrow(_bestDx.brng) + ' ' + _bestDx.brng + '°';
            if (elDate) {
                var d = new Date(_bestDx.ts);
                elDate.textContent = d.toLocaleDateString() + ' ' + d.toLocaleTimeString();
            }
        }

        function _renderMaxDist() {
            var elBadge = document.getElementById('stats-bestdx-dist-badge');
            if (!elBadge) return;
            if (_maxDistKm <= 0) { elBadge.textContent = '–'; return; }
            elBadge.textContent = _maxDistKm < 10 ? _maxDistKm.toFixed(1) + ' km' : Math.round(_maxDistKm) + ' km';
        }

        function _statsRender() {
            _renderChart();
            _renderTop10();
            _renderBestDx();
            _renderMaxDist();
            _renderMesh();
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
                    bestDx: _bestDx, maxDistKm: _maxDistKm,
                    gridCells: _gridCells,
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
                if (d.maxDistKm) { _maxDistKm = d.maxDistKm; _renderMaxDist(); }
                if (d.bestDx)    { _bestDx    = d.bestDx; _renderBestDx(); }
                if (d.gridCells) { _gridCells = d.gridCells; }
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
            if (e.detail === 'stats') {
                setTimeout(_statsRender, 50);
                _loadWeatherHistory();
            }
        });

        // ── Exports publics (utilisés par les boutons reset inline) ──────────
        window._renderBestDx = _renderBestDx;
        window._resetDx = function() {
            _bestDx = null;
            _maxDistKm = 0;
            fetch('/stats/reset_dx', {method: 'POST'}).catch(function(){});
            _renderBestDx();
            _renderMaxDist();
        };
    })();

    // ── Historique météo 48h ──────────────────────────────────────────────────
    (function() {
        var _wxHistLoaded = false;

        function _wxLinePath(values, minV, maxV, W, H, PL, PR, PT, PB) {
            var cW = W - PL - PR, cH = H - PT - PB;
            var n = values.length;
            var pts = [];
            for (var i = 0; i < n; i++) {
                var v = values[i];
                var x = PL + (i / (n - 1)) * cW;
                var y = (maxV === minV)
                    ? PT + cH / 2
                    : PT + cH - ((v - minV) / (maxV - minV)) * cH;
                pts.push(x.toFixed(1) + ',' + y.toFixed(1));
            }
            return pts.join(' ');
        }

        function _wxAreaPath(values, minV, maxV, W, H, PL, PR, PT, PB) {
            var cW = W - PL - PR, cH = H - PT - PB;
            var n = values.length;
            var top = [];
            for (var i = 0; i < n; i++) {
                var v = values[i];
                var x = PL + (i / (n - 1)) * cW;
                var y = (maxV === minV)
                    ? PT + cH / 2
                    : PT + cH - ((v - minV) / (maxV - minV)) * cH;
                top.push(x.toFixed(1) + ',' + y.toFixed(1));
            }
            var xFirst = (PL).toFixed(1), xLast = (PL + cW).toFixed(1), yBase = (PT + cH).toFixed(1);
            return 'M ' + xFirst + ',' + yBase + ' L ' + top.join(' L ') + ' L ' + xLast + ',' + yBase + ' Z';
        }

        function _wxRenderSvg(svgId, values, color, areaColor, unit, decimals) {
            var svg = document.getElementById(svgId);
            if (!svg || !values || !values.length) return;
            var W = 800, H = 90, PL = 38, PR = 6, PT = 8, PB = 22;
            var cW = W - PL - PR, cH = H - PT - PB;
            var n = values.length;

            var minV = Math.min.apply(null, values);
            var maxV = Math.max.apply(null, values);
            if (maxV === minV) { minV -= 1; maxV += 1; }
            // Léger padding vertical
            var pad = (maxV - minV) * 0.12;
            minV -= pad; maxV += pad;

            var out = '';

            // Grille Y (3 niveaux)
            for (var step = 0; step <= 2; step++) {
                var yv  = minV + (step / 2) * (maxV - minV);
                var ypy = PT + cH - (step / 2) * cH;
                out += '<line x1="' + PL + '" y1="' + ypy.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + ypy.toFixed(1)
                     + '" stroke="#1e293b" stroke-width="1" stroke-dasharray="2,4"/>';
                out += '<text x="' + (PL - 3) + '" y="' + (ypy + 3).toFixed(1)
                     + '" text-anchor="end" font-size="8" fill="#475569">'
                     + yv.toFixed(decimals) + '</text>';
            }

            // Axe X : étiquettes toutes les 6h
            // n = 48 points = 48h; point i = (47-i)h avant maintenant
            var now = new Date();
            for (var i = 0; i < n; i += 6) {
                var xTick = PL + (i / (n - 1)) * cW;
                var msBack = (n - 1 - i) * 3600 * 1000;
                var d = new Date(now.getTime() - msBack);
                var hLabel = String(d.getHours()).padStart(2, '0') + 'h';
                var dayLabel = (d.getDate() !== now.getDate()) ? (d.getDate() + '/' + (d.getMonth()+1) + ' ') : '';
                out += '<line x1="' + xTick.toFixed(1) + '" y1="' + PT + '" x2="' + xTick.toFixed(1)
                     + '" y2="' + (PT + cH) + '" stroke="#1e293b" stroke-width="1"/>';
                out += '<text x="' + xTick.toFixed(1) + '" y="' + (PT + cH + 14)
                     + '" text-anchor="middle" font-size="7" fill="#475569">' + dayLabel + hLabel + '</text>';
            }

            // Aire de remplissage
            out += '<path d="' + _wxAreaPath(values, minV, maxV, W, H, PL, PR, PT, PB)
                 + '" fill="' + areaColor + '" />';

            // Ligne principale
            out += '<polyline points="' + _wxLinePath(values, minV, maxV, W, H, PL, PR, PT, PB)
                 + '" fill="none" stroke="' + color + '" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>';

            // Ligne de base
            out += '<line x1="' + PL + '" y1="' + (PT + cH) + '" x2="' + (W - PR) + '" y2="' + (PT + cH)
                 + '" stroke="#334155" stroke-width="1"/>';

            svg.innerHTML = out;
        }

        function _wxRenderBars(svgId, values, color, areaColor) {
            // Barres verticales pour les précipitations
            var svg = document.getElementById(svgId);
            if (!svg || !values || !values.length) return;
            var W = 800, H = 90, PL = 38, PR = 6, PT = 8, PB = 22;
            var cW = W - PL - PR, cH = H - PT - PB;
            var n = values.length;
            var maxV = Math.max.apply(null, values);
            if (maxV <= 0) maxV = 1;
            var barW = Math.max(1.5, cW / n - 1);
            var out = '';

            // Grille Y
            for (var step = 0; step <= 2; step++) {
                var yv  = (step / 2) * maxV;
                var ypy = PT + cH - (step / 2) * cH;
                out += '<line x1="' + PL + '" y1="' + ypy.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + ypy.toFixed(1)
                     + '" stroke="#1e293b" stroke-width="1" stroke-dasharray="2,4"/>';
                out += '<text x="' + (PL - 3) + '" y="' + (ypy + 3).toFixed(1)
                     + '" text-anchor="end" font-size="8" fill="#475569">' + yv.toFixed(1) + '</text>';
            }

            // Axe X
            var now = new Date();
            for (var i = 0; i < n; i += 6) {
                var xTick = PL + (i / (n - 1)) * cW;
                var msBack = (n - 1 - i) * 3600 * 1000;
                var d = new Date(now.getTime() - msBack);
                var hLabel = String(d.getHours()).padStart(2, '0') + 'h';
                var dayLabel = (d.getDate() !== now.getDate()) ? (d.getDate() + '/' + (d.getMonth()+1) + ' ') : '';
                out += '<line x1="' + xTick.toFixed(1) + '" y1="' + PT + '" x2="' + xTick.toFixed(1)
                     + '" y2="' + (PT + cH) + '" stroke="#1e293b" stroke-width="1"/>';
                out += '<text x="' + xTick.toFixed(1) + '" y="' + (PT + cH + 14)
                     + '" text-anchor="middle" font-size="7" fill="#475569">' + dayLabel + hLabel + '</text>';
            }

            // Barres
            for (var i = 0; i < n; i++) {
                var v = values[i];
                if (v <= 0) continue;
                var x   = PL + (i / (n - 1)) * cW - barW / 2;
                var bH  = Math.max(1, (v / maxV) * cH);
                var y   = PT + cH - bH;
                out += '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1)
                     + '" width="' + barW.toFixed(1) + '" height="' + bH.toFixed(1)
                     + '" fill="' + color + '" rx="1"/>';
            }

            // Ligne de base
            out += '<line x1="' + PL + '" y1="' + (PT + cH) + '" x2="' + (W - PR) + '" y2="' + (PT + cH)
                 + '" stroke="#334155" stroke-width="1"/>';

            svg.innerHTML = out;
        }

        // ── Graphe LPI + CAPE dual-axis ────────────────────────────────────────
        function _wxRenderLightning(svgId, lpiArr, capeArr) {
            var svg = document.getElementById(svgId);
            if (!svg) return;
            var W = 800, H = 90, PL = 38, PR = 48, PT = 8, PB = 22;
            var cW = W - PL - PR, cH = H - PT - PB;
            var n = lpiArr.length;
            if (n === 0) return;

            // Valeurs LPI (filtrer null)
            var lpiClean = lpiArr.map(function(v){ return (v === null || v === undefined) ? 0 : v; });
            var maxLpi = Math.max.apply(null, lpiClean); if (maxLpi <= 0) maxLpi = 1;

            // Valeurs CAPE
            var capeClean = (capeArr && capeArr.length === n)
                ? capeArr.map(function(v){ return (v === null || v === undefined) ? 0 : v; })
                : lpiArr.map(function(){ return 0; });
            var maxCape = Math.max.apply(null, capeClean); if (maxCape <= 0) maxCape = 1;

            var out = '';

            // Grille Y LPI (gauche) — 3 niveaux
            for (var step = 0; step <= 2; step++) {
                var yv  = (step / 2) * maxLpi;
                var ypy = PT + cH - (step / 2) * cH;
                out += '<line x1="' + PL + '" y1="' + ypy.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + ypy.toFixed(1)
                     + '" stroke="#1e293b" stroke-width="1" stroke-dasharray="2,4"/>';
                out += '<text x="' + (PL - 3) + '" y="' + (ypy + 3).toFixed(1)
                     + '" text-anchor="end" font-size="8" fill="#ca8a04">' + yv.toFixed(1) + '</text>';
            }
            // Axe Y CAPE (droite)
            for (var step = 0; step <= 2; step++) {
                var yv  = (step / 2) * maxCape;
                var ypy = PT + cH - (step / 2) * cH;
                out += '<text x="' + (W - PR + 3) + '" y="' + (ypy + 3).toFixed(1)
                     + '" text-anchor="start" font-size="8" fill="#f97316">' + Math.round(yv) + '</text>';
            }

            // Axe X
            var now = new Date();
            for (var i = 0; i < n; i += 6) {
                var xTick = PL + (i / (n - 1)) * cW;
                var msBack = (n - 1 - i) * 3600 * 1000;
                var d2 = new Date(now.getTime() - msBack);
                var hLabel = String(d2.getHours()).padStart(2, '0') + 'h';
                var dayLabel = (d2.getDate() !== now.getDate()) ? (d2.getDate() + '/' + (d2.getMonth()+1) + ' ') : '';
                out += '<line x1="' + xTick.toFixed(1) + '" y1="' + PT + '" x2="' + xTick.toFixed(1)
                     + '" y2="' + (PT + cH) + '" stroke="#1e293b" stroke-width="1"/>';
                out += '<text x="' + xTick.toFixed(1) + '" y="' + (PT + cH + 14)
                     + '" text-anchor="middle" font-size="7" fill="#475569">' + dayLabel + hLabel + '</text>';
            }

            // Barres LPI — couleur par intensité
            var barW = Math.max(2, cW / n - 1);
            for (var i = 0; i < n; i++) {
                var v = lpiClean[i];
                if (v <= 0) continue;
                var x  = PL + (i / (n - 1)) * cW - barW / 2;
                var bH = Math.max(1, (v / maxLpi) * cH);
                var y  = PT + cH - bH;
                // Rouge > 5, orange 1-5, jaune < 1
                var col = v > 5 ? '#ef4444' : v > 1 ? '#f97316' : '#facc15';
                out += '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1)
                     + '" width="' + barW.toFixed(1) + '" height="' + bH.toFixed(1)
                     + '" fill="' + col + '" rx="1" opacity="0.85"/>';
            }

            // Ligne CAPE (orange, axe droit)
            if (maxCape > 0) {
                var capePts = [];
                for (var i = 0; i < n; i++) {
                    var cx = PL + (i / (n - 1)) * cW;
                    var cy = PT + cH - (capeClean[i] / maxCape) * cH;
                    capePts.push(cx.toFixed(1) + ',' + cy.toFixed(1));
                }
                out += '<polyline points="' + capePts.join(' ')
                     + '" fill="none" stroke="#f97316" stroke-width="1.5"'
                     + ' stroke-linejoin="round" stroke-linecap="round" stroke-dasharray="3,2" opacity="0.7"/>';
            }

            // Ligne de base
            out += '<line x1="' + PL + '" y1="' + (PT + cH) + '" x2="' + (W - PR) + '" y2="' + (PT + cH)
                 + '" stroke="#334155" stroke-width="1"/>';

            svg.innerHTML = out;
        }

        window._loadWeatherHistory = function() {
            if (_wxHistLoaded) return;   // déjà chargé dans cette session
            var st = document.getElementById('wxhist-status');
            fetch('/weather_history')
                .then(function(r) { return r.ok ? r.json() : Promise.reject(r.status); })
                .then(function(d) {
                    if (!d || !d.temperature_2m || !d.temperature_2m.length) {
                        if (st) st.textContent = 'données indisponibles';
                        return;
                    }
                    _wxHistLoaded = true;

                    // température
                    _wxRenderSvg('wxhist-temp', d.temperature_2m, '#fb923c', 'rgba(251,146,60,0.10)', '°C', 1);

                    // vent en km/h (API retourne m/s → ×3.6)
                    var windKmh = d.wind_speed_10m.map(function(v){ return +(v * 3.6).toFixed(1); });
                    _wxRenderSvg('wxhist-wind', windKmh, '#38bdf8', 'rgba(56,189,248,0.10)', 'km/h', 0);

                    // précipitations barres
                    _wxRenderBars('wxhist-rain', d.precipitation, '#60a5fa', 'rgba(96,165,250,0.15)');

                    // orage : LPI + CAPE dual-axis
                    if (d.lightning_potential && d.lightning_potential.length) {
                        _wxRenderLightning('wxhist-lightning', d.lightning_potential, d.cape || []);
                        // badge max LPI
                        var maxLpi = Math.max.apply(null, d.lightning_potential.filter(function(v){ return v !== null; }));
                        var el = document.getElementById('wxhist-lightning-max');
                        if (el && maxLpi > 0) {
                            var sev = maxLpi > 5 ? 'text-red-400' : maxLpi > 1 ? 'text-orange-400' : 'text-yellow-500';
                            el.innerHTML = '<span class="' + sev + '">max LPI ' + maxLpi.toFixed(2) + ' J/kg</span>';
                        }
                    }

                    // statut
                    if (st) {
                        var loc = d.location ? ' · ' + d.location : '';
                        st.textContent = 'màj ' + new Date().toLocaleTimeString() + loc;
                    }
                })
                .catch(function(err) {
                    if (st) st.textContent = 'erreur (' + err + ')';
                });
        };

        // Chargement automatique si l'onglet stats est déjà actif à l'ouverture
        window.addEventListener('load', function() {
            var tab = document.getElementById('tab-stats');
            if (tab && !tab.classList.contains('hidden')) {
                _loadWeatherHistory();
                setTimeout(function() { if (window.loadSignalPath) loadSignalPath(false); }, 0);
                if (window.loadPropAlertHistory) loadPropAlertHistory();
            }
        });
    })();

    // ── Signal Path — chemins réseau APRS ────────────────────────────────────
    (function() {
        var _spTimer = null;

        // Couleur par source
        function _srcCls(src) {
            if (src === 'RF')     return 'text-emerald-300';
            if (src === 'IS')     return 'text-violet-300';
            if (src === 'aprsfi') return 'text-sky-300';
            return 'text-slate-400';
        }
        function _srcBadge(src) {
            var map = { RF: ['RF', 'bg-emerald-900/40 border-emerald-700/40'],
                        IS: ['IS', 'bg-violet-900/40 border-violet-700/40'],
                        aprsfi: ['aprs.fi', 'bg-sky-900/40 border-sky-700/40'] };
            var info = map[src] || [src, 'bg-slate-800/80 border-slate-700/40'];
            return '<span class="inline-flex items-center ' + info[1]
                 + ' border rounded px-1.5 py-0.5 text-[8px] font-bold font-mono uppercase tracking-wide '
                 + _srcCls(src) + '">' + info[0] + '</span>';
        }

        // Nœud chaîné (callsign pill)
        function _nodePill(call, role) {
            var roleStyle = {
                'me':    'background:rgba(16,185,129,0.2);border-color:rgba(16,185,129,0.5);color:#6ee7b7',
                'digi':  'background:rgba(245,158,11,0.15);border-color:rgba(245,158,11,0.4);color:#fcd34d',
                'igate': 'background:rgba(139,92,246,0.15);border-color:rgba(139,92,246,0.4);color:#c4b5fd',
                'wide':  'background:rgba(71,85,105,0.3);border-color:rgba(71,85,105,0.5);color:#94a3b8',
            };
            var st = roleStyle[role] || roleStyle['wide'];
            var link = (role !== 'wide')
                ? '<a href="https://qrz.com/db/' + call.replace(/[^A-Z0-9]/gi,'') + '" target="_blank"'
                  + ' style="' + st + ';font-family:monospace;font-size:11px;font-weight:700;'
                  + 'padding:2px 8px;border-radius:6px;border:1px solid;display:inline-block;'
                  + 'text-decoration:none">' + esc(call) + '</a>'
                : '<span style="' + st + ';font-family:monospace;font-size:11px;font-weight:500;'
                  + 'padding:2px 8px;border-radius:6px;border:1px solid;display:inline-block">'
                  + esc(call) + '</span>';
            return link;
        }

        function _arrow() {
            return '<span style="color:#475569;margin:0 2px;font-size:12px">→</span>';
        }

        // Rendu d'un chemin complet sous forme de nœuds chaînés
        function _renderChain(pathObj, myCall) {
            // pathObj = { src, nodes: ['F1RIQ-9', 'F5ZKB*', 'WIDE2-1', 'qAR', 'F5XAJ-10'], source }
            var nodes = pathObj.nodes || [];
            var parts = [];
            // Moi en premier
            parts.push(_nodePill(myCall, 'me'));
            for (var i = 0; i < nodes.length; i++) {
                var n = nodes[i];
                if (!n) continue;
                var upper = n.toUpperCase();
                parts.push(_arrow());
                if (upper === 'QAR' || upper === 'QAC' || upper === 'QAS') {
                    parts.push('<span style="color:#64748b;font-size:10px;font-family:monospace">' + esc(n) + '</span>');
                } else if (/^WIDE\d/.test(upper)) {
                    parts.push(_nodePill(n, 'wide'));
                } else if (n.endsWith('*') || i === 0) {
                    parts.push(_nodePill(n.replace('*','') + (n.endsWith('*') ? '*' : ''), 'digi'));
                } else {
                    // iGate (après qAR) ou digi non marqué
                    var role = (i > 0 && (nodes[i-1] || '').toUpperCase().startsWith('Q')) ? 'igate' : 'digi';
                    parts.push(_nodePill(n, role));
                }
            }
            return parts.join('');
        }

        window.loadSignalPath = function(force) {
            var st = document.getElementById('sigpath-status');
            if (st) st.textContent = 'chargement…';
            fetch('/signal_path' + (force ? '?refresh=1' : ''))
                .then(function(r) { return r.ok ? r.json() : Promise.reject(r.status); })
                .then(function(d) {
                    if (!d) return;

                    // Callsign
                    var csEl = document.getElementById('sigpath-callsign');
                    if (csEl) csEl.textContent = d.callsign || '';

                    // ── Dernier chemin ──────────────────────────────────────
                    var lastEl = document.getElementById('sigpath-last');
                    if (lastEl) {
                        if (d.last_path) {
                            lastEl.innerHTML = _renderChain(d.last_path, d.callsign)
                                + '&ensp;' + _srcBadge(d.last_path.source)
                                + '<span class="text-[9px] font-mono text-slate-600 ml-2">'
                                + esc(d.last_path.time_str || '') + '</span>';
                        } else {
                            lastEl.innerHTML = '<span class="text-slate-600 text-[10px] italic">Aucune trame de votre station observée</span>';
                        }
                    }

                    // ── Top digis ───────────────────────────────────────────
                    var digiEl = document.getElementById('sigpath-digis');
                    if (digiEl) {
                        if (d.digi_counts && d.digi_counts.length) {
                            var maxC = d.digi_counts[0].count || 1;
                            digiEl.innerHTML = d.digi_counts.slice(0, 8).map(function(e) {
                                var pct = Math.round((e.count / maxC) * 100);
                                var srcB = _srcBadge(e.source || 'RF');
                                return '<div class="space-y-0.5">'
                                     + '<div class="flex items-center justify-between gap-2">'
                                     + '<span class="text-[10px] font-mono font-bold text-amber-300">'
                                     + _nodePill(e.digi, 'digi') + '</span>'
                                     + '<span class="flex items-center gap-1">'
                                     + srcB
                                     + '<span class="text-[9px] font-mono text-slate-500">' + e.count + '×</span>'
                                     + '</span>'
                                     + '</div>'
                                     + '<div style="height:3px;background:#1e293b;border-radius:2px;overflow:hidden">'
                                     + '<div style="height:100%;width:' + pct + '%;'
                                     + 'background:linear-gradient(90deg,#f59e0b,#fbbf24);border-radius:2px;transition:width .4s"></div>'
                                     + '</div></div>';
                            }).join('');
                        } else {
                            digiEl.innerHTML = '<p class="text-slate-600 text-[10px] italic">Aucun digipeat observé</p>';
                        }
                    }

                    // ── Historique ──────────────────────────────────────────
                    var histEl = document.getElementById('sigpath-history');
                    if (histEl) {
                        if (d.history && d.history.length) {
                            histEl.innerHTML = d.history.map(function(h) {
                                return '<div class="flex items-center gap-2 py-1 border-b border-slate-800/60">'
                                     + '<span class="text-[9px] font-mono text-slate-600 w-14 shrink-0">'
                                     + esc(h.time_str) + '</span>'
                                     + _srcBadge(h.source)
                                     + '<span class="flex-1 flex flex-wrap items-center gap-0.5 text-[10px]">'
                                     + _renderChain(h, d.callsign)
                                     + '</span>'
                                     + '</div>';
                            }).join('');
                        } else {
                            histEl.innerHTML = '<p class="text-slate-600 text-[10px] italic py-2">Aucun historique disponible</p>';
                        }
                    }

                    if (st) {
                        var src_info = [];
                        if (d.rf_count)     src_info.push(d.rf_count + ' RF');
                        if (d.is_count)     src_info.push(d.is_count + ' IS');
                        if (d.aprsfi_count) src_info.push(d.aprsfi_count + ' aprs.fi');
                        st.textContent = 'màj ' + new Date().toLocaleTimeString()
                            + (src_info.length ? ' · ' + src_info.join(', ') : '');
                    }
                })
                .catch(function(err) {
                    if (st) st.textContent = 'erreur (' + err + ')';
                });
        };

        // Brancher sur ouverture onglet stats + polling 60 s
        document.addEventListener('aprs-switchtab', function(e) {
            if (e.detail === 'stats') {
                loadSignalPath(false);
                if (_spTimer) clearInterval(_spTimer);
                _spTimer = setInterval(function(){ loadSignalPath(false); }, 60000);
                loadPropAlertHistory();
            }
        });
    })();


    // ── Onglet Notes ─────────────────────────────────────────────────────────
    (function() {
        var STORAGE_KEY = 'aprs_operator_notes';
        var _autoSaveTimer = null;

        function _notesLoad() {
            var ed = document.getElementById('notes-editor');
            if (!ed) return;
            try {
                ed.value = localStorage.getItem(STORAGE_KEY) || '';
            } catch(e) { ed.value = ''; }
            _notesUpdateCount();
        }

        function _notesUpdateCount() {
            var ed  = document.getElementById('notes-editor');
            var el  = document.getElementById('notes-char-count');
            if (!ed || !el) return;
            var n = ed.value.length;
            el.textContent = n.toLocaleString('fr-FR') + ' car.';
            el.style.color = n > 8000 ? '#f87171' : n > 4000 ? '#fcd34d' : '#334155';
        }

        function _notesSetStatus(msg, color) {
            var el = document.getElementById('notes-status');
            if (!el) return;
            el.textContent = msg;
            el.style.color  = color || '#475569';
            clearTimeout(el._clr);
            el._clr = setTimeout(function(){ el.textContent = ''; }, 3000);
        }

        window._notesSave = function() {
            var ed = document.getElementById('notes-editor');
            if (!ed) return;
            try {
                localStorage.setItem(STORAGE_KEY, ed.value);
                _notesSetStatus('✓ Sauvegardé', '#34d399');
            } catch(e) {
                _notesSetStatus('✗ Erreur localStorage', '#f87171');
            }
        };

        window._notesCopy = function() {
            var ed = document.getElementById('notes-editor');
            if (!ed || !ed.value) return;
            navigator.clipboard ? navigator.clipboard.writeText(ed.value)
                                      .then(function(){ _notesSetStatus('✓ Copié', '#60a5fa'); })
                                      .catch(function(){ _notesSetStatus('✗ Erreur copie', '#f87171'); })
                                : (function(){
                                    ed.select();
                                    document.execCommand('copy');
                                    _notesSetStatus('✓ Copié', '#60a5fa');
                                  })();
        };

        window._notesClear = function() {
            if (!confirm('Effacer toutes les notes ?')) return;
            var ed = document.getElementById('notes-editor');
            if (ed) ed.value = '';
            try { localStorage.removeItem(STORAGE_KEY); } catch(e) {}
            _notesUpdateCount();
            _notesSetStatus('Notes effacées', '#94a3b8');
        };

        // Charger au démarrage
        _notesLoad();

        // Attacher les événements quand le DOM est prêt
        document.addEventListener('DOMContentLoaded', function() {
            var ed = document.getElementById('notes-editor');
            if (!ed) return;
            ed.addEventListener('input', function() {
                _notesUpdateCount();
                // Autosave après 2 s d'inactivité
                clearTimeout(_autoSaveTimer);
                _autoSaveTimer = setTimeout(function() {
                    try {
                        localStorage.setItem(STORAGE_KEY, ed.value);
                        _notesSetStatus('✓ Auto-sauvegardé', '#475569');
                    } catch(e) {}
                }, 2000);
            });
        });

        // Charger aussi lors du switch vers l'onglet notes
        document.addEventListener('aprs-switchtab', function(e) {
            if (e.detail === 'notes') _notesLoad();
        });
    })();

    // ── Historique alertes propagation & blackout HF ──────────────────────────
    window.loadPropAlertHistory = function() {
        var st   = document.getElementById('propalert-hist-status');
        var list = document.getElementById('propalert-hist-list');
        if (st) st.textContent = 'chargement…';
        fetch('/prop_alert_history')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                var alerts = d.alerts || [];
                if (st) st.textContent = alerts.length ? alerts.length + ' alerte(s)' : '–';
                if (!list) return;
                if (!alerts.length) {
                    list.innerHTML = '<p class="text-slate-600 text-[10px] italic text-center py-4">Aucune alerte depuis le démarrage du serveur.</p>';
                    return;
                }
                list.innerHTML = alerts.map(function(a) {
                    var ago = _fmtAgo(a.ago_s);
                    var extras = [];
                    if (a.kp  !== null && a.kp  !== undefined) extras.push('Kp ' + parseFloat(a.kp).toFixed(1));
                    if (a.sfi !== null && a.sfi !== undefined) extras.push('SFI ' + Math.round(a.sfi));
                    if (a.xray_class) extras.push(a.xray_class);
                    var extraHtml = extras.length
                        ? '<span class="text-[9px] font-mono text-slate-500 ml-2">' + extras.join('· ') + '</span>'
                        : '';
                    return '<div class="flex items-start gap-3 px-3 py-2 rounded-xl border" style="border-color:' + a.color_border + '33;background:' + a.color_border + '11">'
                        + '<span class="text-base leading-none mt-0.5">' + a.icon + '</span>'
                        + '<div class="flex-1 min-w-0">'
                        +   '<div class="flex items-center gap-2 flex-wrap">'
                        +     '<span class="text-[11px] font-bold" style="color:' + a.color_text + '">' + esc(a.title) + '</span>'
                        +     extraHtml
                        +   '</div>'
                        +   '<div class="text-[10px] text-slate-400 mt-0.5">' + esc(a.detail) + '</div>'
                        + '</div>'
                        + '<span class="text-[9px] font-mono text-slate-600 shrink-0 mt-0.5">' + ago + '</span>'
                        + '</div>';
                }).join('');
            })
            .catch(function(err) {
                if (st) st.textContent = 'erreur';
                if (list) list.innerHTML = '<p class="text-red-500 text-[10px] italic text-center py-4">Erreur chargement historique : ' + esc(String(err)) + '</p>';
            });
    };

    function _fmtAgo(s) {
        if (s < 60)   return s + ' s';
        if (s < 3600) return Math.floor(s/60) + ' min';
        if (s < 86400) return Math.floor(s/3600) + ' h ' + Math.floor((s%3600)/60) + ' min';
        return Math.floor(s/86400) + ' j';
    }


    fetch('/rx_history')
        .then(function(r) { return r.json(); })
        .then(function(frames) {
            frames.forEach(function(data) {
                if (data._fid) {
                _seenFids.add(data._fid);
                if (_seenFids.size > 500) {
                    var _it = _seenFids.values();
                    for (var _i=0;_i<100;_i++) _seenFids.delete(_it.next().value);
                }
            }
                if (data.type === 'tx_event') {
                    txCount++;
                    if (window._statsRecord) _statsRecord('TX', data);
                    addLog('TX', data);
                } else if (data.type !== 'rx_level' && data.type !== 'connected') {
                    rxFrameCount++;
                    if (window._statsRecord) _statsRecord(data._source === 'IS' ? 'IS' : 'RX', data);
                    // Déduplication contenu : ne pas afficher deux fois la même trame
                    if (!_isDupContent(data)) {
                        addLog('RX', data);
                        document.dispatchEvent(new CustomEvent('aprs-frame', {detail: data}));
                    }
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
    evtSource.onmessage = _sseOnMessage;
    evtSource.onerror   = _sseOnError;

    function _handleSseFrame(data) {
        try {
            // Ignorer les trames déjà affichées via rx_history (dédoublonnage par _fid)
            if (data._fid && _seenFids.has(data._fid)) return;
            if (data._fid) _seenFids.add(data._fid);
            // Ignorer les doublons de contenu (même trame via chemins multiples)
            if (data.type !== 'rx_level' && data.type !== 'tx_event' && _isDupContent(data)) return;
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
            } else if (data.type === 'weather_alert') {
                _wxAlertShow(data);
            } else if (data.type === 'prop_alert') {
                _propAlertShow(data);
            } else if (data.type === 'iss_pass_alert') {
                _issPassAlertShow(data);
            } else if (data.type === 'msg_ack') {
                // ── ACK reçu : mettre à jour la bulle TX en temps réel ────────
                var badge = document.getElementById('ack-badge-' + data.msgno);
                if (badge) {
                    badge.outerHTML = '<span style="color:#34d399;font-size:9px;margin-left:4px" title="ACK reçu">✓ ACK</span>';
                }
                // Rafraîchir si c'est la conversation active
                if (activeContact && activeContact === data.dest) {
                    // Pas de rechargement complet — le badge vient d'être mis à jour inline
                }
            } else if (data.type === 'msg_retry') {
                // ── Retry en cours ou timeout ─────────────────────────────────
                var badge2 = document.getElementById('ack-badge-' + data.msgno);
                if (badge2) {
                    if (data.failed) {
                        badge2.outerHTML = '<span style="color:#f87171;font-size:9px;margin-left:4px" title="Pas de réponse après ' + data.max + ' essais">✗ TIMEOUT</span>';
                    } else {
                        badge2.title = 'Retry ' + data.retry + '/' + data.max + '…';
                        badge2.textContent = '⟳';
                        badge2.style.color = '#f59e0b';
                    }
                }
                if (data.failed && activeContact && activeContact === data.dest) {
                    refreshContacts();
                }
            } else if (data.type !== 'connected') {
                rxFrameCount++;
                document.getElementById('rx-count').textContent = rxFrameCount;
                document.getElementById('rx-led').style.background = '#60a5fa';
                setTimeout(function() { document.getElementById('rx-led').style.background = '#34d399'; }, 400);
                if (window._statsRecord) _statsRecord(data._source === 'IS' ? 'IS' : 'RX', data);
                if (data._source === 'IS') _incIsCount();
                addLog(data._source === 'IS' ? 'IS' : 'RX', data);
                // Dispatcher vers la carte Leaflet
                document.dispatchEvent(new CustomEvent('aprs-frame', {detail: data}));
                // Notification chat si message prive
                if (data._chat) {
                    refreshContacts();
                    var _chatSrc = (data.src || '').trim().toUpperCase();
                    if (activeContact && activeContact.trim().toUpperCase() === _chatSrc) {
                        loadHistory(activeContact);
                    }
                    document.getElementById('qso-badge').classList.remove('hidden');
                    var _mb=document.getElementById('mnav-qso-badge');if(_mb){_mb.textContent='●';_mb.style.display='block';}
                }
            }
        } catch(err) {}
    }  // end _handleSseFrame

    var _sseReconnDelay = 3000;
    var _sseReconnTimer = null;

    function _sseOnMessage(e) {
        try {
            var data = JSON.parse(e.data);
            _sseReconnDelay = 3000; // reset backoff on success
            // rétablir indicateur si on était en erreur
            var txt = document.getElementById('rx-status-text');
            // traitement délégué à la fonction principale
            _handleSseFrame(data);
        } catch(err) {}
    }

    function _sseOnError() {
        var txt = document.getElementById('rx-status-text');
        var led = document.getElementById('rx-led');
        if (txt) { txt.textContent = '🔄 Reconnexion SSE…'; txt.className = 'text-amber-400'; }
        if (led) led.style.background = '#f59e0b';
        try { evtSource.close(); } catch(e) {}
        if (_sseReconnTimer) clearTimeout(_sseReconnTimer);
        _sseReconnTimer = setTimeout(function() {
            _sseReconnDelay = Math.min(_sseReconnDelay * 1.5, 30000);
            evtSource = new EventSource('/rx_stream');
            evtSource.onmessage = _sseOnMessage;
            evtSource.onerror   = _sseOnError;
        }, _sseReconnDelay);
    }

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

    function checkHfPathWarn() {
        var hfOn  = document.getElementById('hf_mode') && document.getElementById('hf_mode').checked;
        var pval  = (document.getElementById('pathCustom') || {}).value || '';
        var warn  = document.getElementById('hf_wide_warn');
        if (warn) warn.style.display = (hfOn && /WIDE/i.test(pval)) ? 'block' : 'none';
    }
    // Vérifier aussi quand on modifie manuellement le path
    document.addEventListener('DOMContentLoaded', function() {
        var pc = document.getElementById('pathCustom');
        if (pc) pc.addEventListener('input', checkHfPathWarn);
        checkHfPathWarn();
    });

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

    // ── Countdown ticker ISS ─────────────────────────────────────────────────
    var _issNextRisetime  = 0;
    var _issPassDuration  = 0;   // durée du prochain passage (secondes)
    var _issNextRiseAz    = null; // azimut AOS du prochain passage
    var _issCountdownTimer = null;
    function _issTickCountdown() {
        var el = document.getElementById('iss-next-countdown');
        if (!el || !_issNextRisetime) return;
        var now  = Date.now() / 1000;
        var diff = Math.round(_issNextRisetime - now);
        var inPass = (diff <= 0 && _issPassDuration > 0
                      && now < _issNextRisetime + _issPassDuration);
        if (diff <= 0) {
            el.textContent = '🛸 EN COURS';
            el.style.color = '#4ade80';
            el.style.borderColor = '#22c55e44';
            el.style.background  = 'rgba(74,222,128,.12)';
        } else {
            var h = Math.floor(diff / 3600);
            var m = Math.floor((diff % 3600) / 60);
            var s = diff % 60;
            var txt = h > 0
                ? h + 'h' + String(m).padStart(2,'0') + 'm'
                : m + 'min ' + String(s).padStart(2,'0') + 's';
            el.textContent = '⏱ ' + txt;
            el.style.display = 'inline-block';
        }
        // Voyant ISS : orange/doré pendant le passage, violet sinon
        _issUpdateDot(undefined, inPass);
    }

    function issPassRefresh() {
        // issPassRefresh écrit dans iss-pass-list (onglet Réglages/alertes widget)
        // ET dans iss-pass-list-cfg (miroir condensé dans Réglages ISS).
        // L'onglet ISS principal utilise iss-24h-list, géré exclusivement par iss24hRefresh().
        var list    = document.getElementById('iss-pass-list');
        var cfgList = document.getElementById('iss-pass-list-cfg');
        var btn     = document.getElementById('iss-refresh-btn');
        if (!list && !cfgList && !btn) return;   // rien à mettre à jour
        if (list) list.innerHTML = '<span style="color:#475569;font-style:italic;font-size:10px">⏳ Calcul SGP4...</span>';
        if (cfgList) cfgList.innerHTML = '<span style="color:#475569;font-style:italic;font-size:10px">⏳ Calcul SGP4...</span>';
        if (btn)  btn.textContent = '…';
        fetch('/iss_passes').then(function(r){ return r.json(); }).then(function(d){
            if (btn) btn.textContent = '↺ MAJ';
            if (d.error) {
                if (list)    list.innerHTML    = '<span style="color:#ef4444;font-size:10px">' + d.error + '</span>';
                if (cfgList) cfgList.innerHTML = '<span style="color:#ef4444;font-size:10px">❌ ' + d.error + '</span>';
                return;
            }
            if (!d.passes || !d.passes.length) {
                if (list)    list.innerHTML    = '<span style="color:#475569;font-style:italic;font-size:10px">Aucun passage prévu aujourd&rsquo;hui</span>';
                if (cfgList) cfgList.innerHTML = '<span style="color:#475569;font-style:italic;font-size:10px">Aucun passage prévu aujourd&rsquo;hui</span>';
                return;
            }

            // Mettre à jour le countdown sur le 1er passage FUTUR
            var _nowSec = Date.now() / 1000;
            var _firstFuture = d.passes.find(function(p){ return p.risetime > _nowSec; });
            _issNextRisetime = _firstFuture ? _firstFuture.risetime   : 0;
            _issPassDuration = _firstFuture ? (_firstFuture.duration  || _firstFuture.duration_min * 60 || 0) : 0;
            _issNextRiseAz   = _firstFuture ? (_firstFuture.rise_az   !== undefined && _firstFuture.rise_az !== null ? Math.round(_firstFuture.rise_az) : null) : null;
            // Badge azimut AOS dans le dashboard principal
            (function() {
                var _AZ_C = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSO','SO','OSO','O','ONO','NO','NNO'];
                var elAz = document.getElementById('iss-next-az-badge');
                if (elAz) {
                    if (_issNextRiseAz !== null) {
                        var card = _AZ_C[Math.round(_issNextRiseAz / 22.5) % 16];
                        elAz.textContent = '🧭 AOS ' + _issNextRiseAz + '° ' + card;
                        elAz.style.display = 'inline';
                    } else {
                        elAz.style.display = 'none';
                    }
                }
                // Footer live panel : AOS → LOS pour le passage le plus proche (futur ou en cours)
                var elFoot = document.getElementById('iss-live-az-footer');
                if (elFoot && d.passes && d.passes.length) {
                    var _nowSec3 = Date.now() / 1000;
                    var _cur = d.passes.find(function(p){ return p.risetime <= _nowSec3 && _nowSec3 < p.risetime + (p.duration || 0); })
                            || d.passes.find(function(p){ return p.risetime > _nowSec3; });
                    if (_cur && _cur.rise_az !== undefined && _cur.rise_az !== null) {
                        var rAz = Math.round(_cur.rise_az);
                        var rCd = _AZ_C[Math.round(rAz / 22.5) % 16];
                        var txt = '🧭 AOS ' + rAz + '° ' + rCd;
                        if (_cur.set_az !== undefined && _cur.set_az !== null) {
                            var sAz = Math.round(_cur.set_az);
                            var sCd = _AZ_C[Math.round(sAz / 22.5) % 16];
                            txt += '  →  LOS ' + sAz + '° ' + sCd;
                        }
                        elFoot.textContent = txt;
                        elFoot.style.display = 'block';
                    } else {
                        elFoot.style.display = 'none';
                    }
                }
            })();
            _issTickCountdown();
            if (_issCountdownTimer) clearInterval(_issCountdownTimer);
            _issCountdownTimer = setInterval(_issTickCountdown, 1000);

            // Onglet Réglages : list peut être null, cfgList prend le relais
            if (!list && !cfgList) return;

            var _nowSec2 = Date.now() / 1000;
            var _nextIdx = d.passes.findIndex(function(p){ return p.risetime > _nowSec2; });
            if (_nextIdx < 0) _nextIdx = d.passes.length - 1; // tous passés : highlight dernier
            if (list) { list.innerHTML = d.passes.map(function(p, i) {
                var inMin   = Math.round(p.in_min);
                var maxEl   = (p.max_el !== undefined && p.max_el !== null) ? Math.round(p.max_el) : null;
                var dur     = p.duration_min;
                var isNext  = (i === _nextIdx);

                // Couleur selon imminence (inMin négatif = déjà passé)
                var urgColor = inMin < -5   ? '#334155'   // gris foncé : déjà passé
                             : inMin <= 0   ? '#f472b6'   // rose : en cours
                             : inMin < 15   ? '#f472b6'   // rose : très proche
                             : inMin < 60   ? '#a78bfa'   // violet : < 1h
                             : inMin < 240  ? '#67e8f9'   // cyan : < 4h
                             :                '#475569'; // gris : lointain

                // Qualité du passage selon élévation max
                var elLabel = '', elColor = '#475569', elPct = 0;
                if (maxEl !== null) {
                    elPct = Math.min(100, Math.round(maxEl / 90 * 100));
                    if      (maxEl >= 60) { elLabel = '⭐ Excellent'; elColor = '#4ade80'; }
                    else if (maxEl >= 30) { elLabel = '✦ Bon';        elColor = '#a78bfa'; }
                    else if (maxEl >= 10) { elLabel = '· Passable';   elColor = '#67e8f9'; }
                    else                  { elLabel = '↙ Rasant';     elColor = '#475569'; }
                }

                // Probabilité contact APRS VHF 145.825 MHz selon élévation max
                var aprsProb = 0, aprsColor = '#475569', aprsQuality = '';
                if (maxEl !== null) {
                    if      (maxEl < 5)  { aprsProb = 5;  aprsColor = '#475569'; aprsQuality = 'Très faible'; }
                    else if (maxEl < 10) { aprsProb = 12; aprsColor = '#ef4444'; aprsQuality = 'Faible'; }
                    else if (maxEl < 20) { aprsProb = 30; aprsColor = '#f97316'; aprsQuality = 'Possible'; }
                    else if (maxEl < 35) { aprsProb = 52; aprsColor = '#eab308'; aprsQuality = 'Probable'; }
                    else if (maxEl < 55) { aprsProb = 72; aprsColor = '#22c55e'; aprsQuality = 'Bonne'; }
                    else                 { aprsProb = 90; aprsColor = '#10b981'; aprsQuality = 'Excellente'; }
                    // Interpolation linéaire par segment
                    if (maxEl >= 5  && maxEl < 10) aprsProb = Math.round(5  + (maxEl-5)  / 5  * 15);
                    if (maxEl >= 10 && maxEl < 20) aprsProb = Math.round(20 + (maxEl-10) / 10 * 20);
                    if (maxEl >= 20 && maxEl < 35) aprsProb = Math.round(40 + (maxEl-20) / 15 * 25);
                    if (maxEl >= 35 && maxEl < 55) aprsProb = Math.round(65 + (maxEl-35) / 20 * 15);
                    if (maxEl >= 55 && maxEl < 80) aprsProb = Math.round(80 + (maxEl-55) / 25 * 15);
                    if (maxEl >= 80)               aprsProb = 95;
                }

                // Fond de carte
                var cardBg  = isNext
                    ? 'background:linear-gradient(135deg,rgba(124,58,237,.18),rgba(59,130,246,.10));border:1px solid #7c3aed55'
                    : 'background:rgba(15,23,42,0.5);border:1px solid #1e293b';

                var html = '<div style="border-radius:10px;padding:8px 10px;' + cardBg + ';margin-bottom:4px">';

                // Ligne 1 : heure début + badge "dans Xmin"
                var setFmt = p.set_fmt || '';
                html += '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:3px">';
                html +=   '<div>';
                html +=     '<div style="font-family:monospace;font-size:' + (isNext ? '13' : '11') + 'px;font-weight:' + (isNext ? '800' : '600') + ';color:' + (isNext ? '#e2e8f0' : '#94a3b8') + '">';
                html +=       (isNext ? '🛸 ' : '') + p.risetime_fmt;
                html +=     '</div>';
                if (setFmt) {
                    html += '<div style="font-family:monospace;font-size:9px;color:#475569;margin-top:1px">⬇ fin&nbsp;&nbsp;' + setFmt + '</div>';
                }
                html +=   '</div>';
                html +=   '<span style="font-size:10px;font-weight:700;color:' + urgColor + ';background:' + urgColor + '18;border-radius:6px;padding:2px 7px;white-space:nowrap">';
                html +=     (inMin < -5 ? 'PASSÉ' : inMin <= 0 ? 'EN COURS' : 'dans ' + inMin + ' min');
                html +=   '</span>';
                html += '</div>';

                // Ligne 2 : durée + élévation max + label qualité
                html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:' + (maxEl !== null ? '6' : '0') + 'px">';
                html +=   '<span style="font-size:9px;color:#64748b">⏱ ' + dur + ' min</span>';
                if (maxEl !== null) {
                    html += '<span style="font-size:9px;color:#64748b">·</span>';
                    html += '<span style="font-size:9px;color:' + elColor + ';font-weight:700">▲ ' + maxEl + '°</span>';
                    html += '<span style="font-size:9px;color:' + elColor + '">' + elLabel + '</span>';
                }
                // Fréquences (seulement si passage notable ou 1er)
                if (isNext || (maxEl !== null && maxEl >= 20)) {
                    html += '<span style="margin-left:auto;font-size:8px;color:#334155;font-family:monospace">145.825 · 437.550</span>';
                }
                html += '</div>';

                // Barre d'élévation
                if (maxEl !== null) {
                    var barColor = maxEl >= 60 ? '#4ade80' : maxEl >= 30 ? '#a78bfa' : '#67e8f9';
                    html += '<div style="height:3px;background:#0f172a;border-radius:2px;overflow:hidden;margin-bottom:6px">';
                    html +=   '<div style="width:' + elPct + '%;height:100%;background:' + barColor + ';border-radius:2px;transition:width .6s ease"></div>';
                    html += '</div>';
                }

                // Probabilité contact APRS
                if (maxEl !== null) {
                    html += '<div style="display:flex;align-items:center;gap:10px;background:rgba(0,0,0,.25);border-radius:8px;padding:6px 8px;margin-top:2px">';
                    // Colonne gauche : label + barre
                    html +=   '<div style="flex:1;min-width:0">';
                    html +=     '<div style="font-size:9px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px">📡 Contact APRS</div>';
                    html +=     '<div style="height:5px;background:#0f172a;border-radius:3px;overflow:hidden">';
                    html +=       '<div style="width:' + aprsProb + '%;height:100%;background:' + aprsColor + ';border-radius:3px;transition:width .8s ease;box-shadow:0 0 6px ' + aprsColor + '88"></div>';
                    html +=     '</div>';
                    html +=     '<div style="font-size:9px;color:' + aprsColor + ';opacity:.75;margin-top:3px">' + aprsQuality + '</div>';
                    html +=   '</div>';
                    // Colonne droite : pourcentage en grand
                    html +=   '<div style="text-align:center;min-width:52px">';
                    html +=     '<div style="font-family:monospace;font-size:22px;font-weight:900;line-height:1;color:' + aprsColor + ';text-shadow:0 0 12px ' + aprsColor + '66">' + aprsProb + '%</div>';
                    html +=   '</div>';
                    html += '</div>';
                }

                html += '</div>';
                return html;
            }).join(''); } // fin if (list)

            // Statut position
            var st = document.getElementById('iss-pass-status');
            if (st && d.lat !== undefined) {
                st.textContent = '📍 ' + d.lat.toFixed(2) + '° / ' + d.lon.toFixed(2) + '°';
            }

            // Miroir Réglages (version condensée)
            if (cfgList) {
                cfgList.innerHTML = d.passes.map(function(p, i) {
                    var inMin  = Math.round(p.in_min);
                    var maxEl  = (p.max_el !== undefined && p.max_el !== null) ? Math.round(p.max_el) : null;
                    var riseAz = (p.rise_az !== undefined && p.rise_az !== null) ? Math.round(p.rise_az) : null;
                    var col    = inMin < 60 ? '#a78bfa' : '#475569';
                    var times  = p.risetime_fmt + (p.set_fmt ? ' → ' + p.set_fmt : '');
                    var _AZ_C  = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSO','SO','OSO','O','ONO','NO','NNO'];
                    var azTxt  = riseAz !== null ? ' · 🧭 ' + riseAz + '° ' + _AZ_C[Math.round(riseAz/22.5)%16] : '';
                    // Probabilité APRS (calcul inline)
                    var aprsProb = 0, aprsColor = '#475569';
                    if (maxEl !== null) {
                        if      (maxEl < 5)  { aprsProb = 5;  aprsColor = '#475569'; }
                        else if (maxEl < 10) { aprsProb = 12; aprsColor = '#ef4444'; }
                        else if (maxEl < 20) { aprsProb = 30; aprsColor = '#f97316'; }
                        else if (maxEl < 35) { aprsProb = 52; aprsColor = '#eab308'; }
                        else if (maxEl < 55) { aprsProb = 72; aprsColor = '#22c55e'; }
                        else                 { aprsProb = 90; aprsColor = '#10b981'; }
                        if (maxEl >= 5  && maxEl < 10) aprsProb = Math.round(5  + (maxEl-5)  / 5  * 15);
                        if (maxEl >= 10 && maxEl < 20) aprsProb = Math.round(20 + (maxEl-10) / 10 * 20);
                        if (maxEl >= 20 && maxEl < 35) aprsProb = Math.round(40 + (maxEl-20) / 15 * 25);
                        if (maxEl >= 35 && maxEl < 55) aprsProb = Math.round(65 + (maxEl-35) / 20 * 15);
                        if (maxEl >= 55 && maxEl < 80) aprsProb = Math.round(80 + (maxEl-55) / 25 * 15);
                        if (maxEl >= 80)               aprsProb = 95;
                    }
                    return '<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0;border-bottom:1px solid #1e293b33">'
                        + '<span style="font-family:monospace;font-size:10px;color:' + (i===0?'#e2e8f0':'#64748b') + '">'
                        + (i===0?'🛸 ':'') + times + '</span>'
                        + '<span style="display:flex;align-items:center;gap:5px">'
                        + '<span style="font-size:9px;color:' + col + '">'
                        + 'dans ' + inMin + 'min' + (maxEl!==null ? ' · ▲'+maxEl+'°' : '') + ' · ' + p.duration_min + 'min' + azTxt
                        + '</span>'
                        + (maxEl !== null ? '<span style="font-family:monospace;font-size:9px;font-weight:700;color:' + aprsColor + ';background:' + aprsColor + '1a;border-radius:4px;padding:1px 5px">📡 ' + aprsProb + '%</span>' : '')
                        + '</span>'
                        + '</div>';
                }).join('');
            }
        }).catch(function(e){
            if (btn) btn.textContent = '↺ MAJ';
            if (list) list.innerHTML = '<span style="color:#ef4444;font-size:10px">❌ Calcul SGP4 indisponible</span>';
            if (cfgList) cfgList.innerHTML = '<span style="color:#ef4444;font-size:10px">❌ Erreur SGP4</span>';
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
            issPassRefresh();
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
        setTimeout(function(){ if (banner.parentNode) banner.parentNode.removeChild(banner); }, 15000);
    }

    // ── Alertes météo — popup ────────────────────────────────────────────────
    function _wxAlertShow(data) {
        // Eviter les doublons visuels sur la même clé
        var existId = 'wx-banner-' + (data.key || 'gen');
        if (document.getElementById(existId)) return;

        // ── Mettre à jour le badge header ────────────────────────────────────
        _wxBadgeSet(data.icon || '⚠️', data.title || 'Alerte météo', data);

        var banner = document.createElement('div');
        banner.id  = existId;
        banner.style.cssText = [
            'position:fixed',
            'top:60px',
            'left:50%',
            'transform:translateX(-50%)',
            'z-index:9998',
            'background:' + (data.color_bg    || '#1c0a00'),
            'border:2px solid ' + (data.color_border || '#f59e0b'),
            'border-radius:14px',
            'padding:12px 26px',
            'font-size:13px',
            'font-weight:700',
            'color:' + (data.color_text  || '#fcd34d'),
            'box-shadow:0 8px 32px ' + (data.color_border || '#f59e0b') + '40',
            'max-width:92vw',
            'text-align:center',
            'cursor:pointer',
            'transition:opacity .3s'
        ].join(';');

        var icon   = data.icon   || '⚠️';
        var title  = data.title  || 'Alerte météo';
        var detail = data.detail || '';
        var extra  = '';
        if (data.temp_c   !== null && data.temp_c   !== undefined) extra += ' · ' + data.temp_c.toFixed(1) + ' °C';
        if (data.wind_kmh !== null && data.wind_kmh !== undefined) extra += ' · vent ' + data.wind_kmh.toFixed(0) + ' km/h';
        if (data.description) extra += ' · ' + data.description;

        banner.innerHTML = icon + ' <b>' + title + '</b>'
            + (detail ? '<br><span style="font-size:11px;font-weight:400;opacity:.85">' + detail + extra + '</span>' : '');

        banner.onclick = function() {
            banner.style.opacity = '0';
            setTimeout(function(){
                banner.parentNode && banner.parentNode.removeChild(banner);
                if (!document.querySelector('[id^="wx-banner-"]')) _wxBadgeClear();
            }, 300);
        };
        document.body.appendChild(banner);

        // Bip double (440 Hz + 550 Hz) — distinct du triple ISS
        try {
            var ctx = new (window.AudioContext || window.webkitAudioContext)();
            [440, 550].forEach(function(freq, i) {
                var osc = ctx.createOscillator();
                var g   = ctx.createGain();
                osc.frequency.value = freq;
                osc.type = 'triangle';
                g.gain.value = 0.2;
                osc.connect(g); g.connect(ctx.destination);
                osc.start(ctx.currentTime + i * 0.22);
                osc.stop (ctx.currentTime + i * 0.22 + 0.18);
            });
            setTimeout(function(){ try { ctx.close(); } catch(_){} }, 1200);
        } catch(_) {}

        // Auto-fermeture après 20 s
        setTimeout(function() {
            if (banner.parentNode) {
                banner.style.opacity = '0';
                setTimeout(function(){
                    banner.parentNode && banner.parentNode.removeChild(banner);
                    if (!document.querySelector('[id^="wx-banner-"]')) _wxBadgeClear();
                }, 300);
            }
        }, 20000);
    }

    function _wxBadgeSet(icon, txt, data) {
        var b  = document.getElementById('wx-alert-badge');
        var bi = document.getElementById('wx-alert-badge-icon');
        var bt = document.getElementById('wx-alert-badge-txt');
        if (!b) return;
        if (bi) bi.textContent = icon;
        if (bt) bt.textContent = txt.length > 22 ? txt.slice(0, 20) + '\u2026' : txt;
        var border = (data && data.color_border) ? data.color_border : '#f59e0b';
        var bg     = (data && data.color_bg)     ? data.color_bg     : 'rgba(120,53,15,0.55)';
        b.style.borderColor = border + '88';
        b.style.background  = bg;
        b.title = txt + (data && data.detail ? ' \u2014 ' + data.detail : '');
        b.style.display   = 'flex';
        b.style.animation = 'wxBadgePulse 2s ease-in-out infinite';
    }

    function _wxBadgeClear() {
        var b = document.getElementById('wx-alert-badge');
        if (b) b.style.display = 'none';
    }

    window._wxAlertBadgeClick = function() {
        document.querySelectorAll('[id^="wx-banner-"]').forEach(function(el) {
            el.style.opacity = '0';
            setTimeout(function(){ el.parentNode && el.parentNode.removeChild(el); }, 300);
        });
        setTimeout(_wxBadgeClear, 350);
    };

    // ── Alertes propagation / blackout ────────────────────────────────────────

    function _propBadgeSet(icon, txt, data) {
        var b  = document.getElementById('prop-alert-badge');
        var bi = document.getElementById('prop-alert-badge-icon');
        var bt = document.getElementById('prop-alert-badge-txt');
        if (!b) return;
        if (bi) bi.textContent = icon;
        // Texte court : supprimer le prefixe bavard, garder l'essentiel
        var short = txt;
        // Kp : "Tempete geomagnetique - Kp 6.7" → "TEMPETE Kp 6.7"
        short = short.replace(/[Tt]emp.te\s+g.omagn.tique\s*[—-]\s*/i, 'TEMPETE ');
        // SFI : "Conditions HF degradees - SFI 65" → "HF DEG SFI 65"
        short = short.replace(/[Cc]onditions\s+HF\s+d.grad.es\s*[—-]\s*/i, 'HF DEG ');
        // Xray : "Eruption solaire M2.5" → "FLARE M2.5"
        short = short.replace(/[Ee]ruption\s+solaire\s*/i, 'FLARE ');
        if (bt) bt.textContent = short.length > 20 ? short.slice(0, 18) + '…' : short;
        var border = (data && data.color_border) ? data.color_border : '#ef4444';
        var bg     = (data && data.color_bg)     ? data.color_bg     : 'rgba(120,20,20,0.6)';
        var txtCol = (data && data.color_text)   ? data.color_text   : '#fca5a5';
        b.style.borderColor  = border + '88';
        b.style.background   = bg;
        if (bt) bt.style.color = txtCol;
        b.title = txt + (data && data.detail ? ' — ' + data.detail : '');
        b.style.display   = 'flex';
        b.style.animation = 'propBadgePulse 2s ease-in-out infinite';
    }

    function _propBadgeClear() {
        var b = document.getElementById('prop-alert-badge');
        if (b) { b.style.display = 'none'; b.style.animation = ''; }
    }

    window._propAlertBadgeClick = function() {
        // Fermer toutes les bannières prop ouvertes
        document.querySelectorAll('[id^="prop-banner-"]').forEach(function(el) {
            el.style.opacity = '0';
            setTimeout(function(){ el.parentNode && el.parentNode.removeChild(el); }, 300);
        });
        setTimeout(_propBadgeClear, 350);
    };

    // ── Pill propagation discrète — mise à jour indices courants ────────────
    function _propPillUpdate() {
        fetch('/prop_status')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                var pill  = document.getElementById('prop-status-pill');
                var elKp  = document.getElementById('prop-pill-kp');
                var elSfi = document.getElementById('prop-pill-sfi');
                var elXr  = document.getElementById('prop-pill-xray');
                if (!pill) return;

                // ── Kp ────────────────────────────────────────────────────
                if (elKp) {
                    var kpTxt = d.kp !== null && d.kp !== undefined
                        ? '🧲 Kp ' + d.kp.toFixed(1) : '🧲 –';
                    elKp.textContent = kpTxt;
                    elKp.style.color = d.kp_alert ? '#fcd34d' : '#475569';
                    elKp.title = d.kp_alert ? ('⚠ Kp ≥ ' + d.kp_max) : '';
                }

                // ── SFI ───────────────────────────────────────────────────
                if (elSfi) {
                    var sfiTxt = d.sfi !== null && d.sfi !== undefined
                        ? '📻 ' + Math.round(d.sfi) : '📻 –';
                    elSfi.textContent = sfiTxt;
                    elSfi.style.color = d.sfi_alert ? '#fca5a5' : '#475569';
                    elSfi.title = d.sfi_alert ? ('⚠ SFI < ' + d.sfi_min) : '';
                }

                // ── X-ray ─────────────────────────────────────────────────
                if (elXr) {
                    var xrTxt = d.xray_class ? '☀️ ' + d.xray_class : '☀️ –';
                    elXr.textContent = xrTxt;
                    elXr.style.color = d.xray_alert ? '#fdba74' : '#475569';
                    elXr.title = d.xray_alert ? ('⚠ Éruption ≥ ' + d.xray_thr) : '';
                }

                // ── Bordure globale ───────────────────────────────────────
                var anyAlert = d.kp_alert || d.sfi_alert || d.xray_alert;
                if (anyAlert) {
                    pill.style.borderColor  = '#ef4444aa';
                    pill.style.background   = 'rgba(69,10,10,0.55)';
                    pill.style.animation    = 'propPillPulse 2.5s ease-in-out infinite';
                } else {
                    pill.style.borderColor  = '#1e293b';
                    pill.style.background   = 'rgba(15,23,42,0.7)';
                    pill.style.animation    = '';
                }
            })
            .catch(function() { /* silencieux si hors-ligne */ });
    }

    // Charger au démarrage puis toutes les 10 minutes
    _propPillUpdate();
    setInterval(_propPillUpdate, 600000);

    function _propAlertShow(data) {
        var existId = 'prop-banner-' + (data.key || 'gen');
        if (document.getElementById(existId)) return;

        // Mettre a jour le badge header (visible sur tous les onglets)
        _propBadgeSet(data.icon || '📶', data.title || 'Alerte propagation', data);

        var banner = document.createElement('div');
        banner.id  = existId;
        banner.style.cssText = [
            'position:fixed',
            'top:60px',
            'left:50%',
            'transform:translateX(-50%)',
            'z-index:9999',
            'background:' + (data.color_bg    || '#1c0a00'),
            'border:2px solid ' + (data.color_border || '#f97316'),
            'border-radius:14px',
            'padding:12px 26px',
            'font-size:13px',
            'font-weight:700',
            'color:' + (data.color_text  || '#fdba74'),
            'box-shadow:0 8px 32px ' + (data.color_border || '#f97316') + '40',
            'max-width:92vw',
            'text-align:center',
            'cursor:pointer',
            'transition:opacity .3s'
        ].join(';');

        var icon   = data.icon   || '📶';
        var title  = data.title  || 'Alerte propagation';
        var detail = data.detail || '';
        var extra  = '';
        if (data.sfi !== null && data.sfi !== undefined) extra += ' · SFI ' + Math.round(data.sfi);
        if (data.kp  !== null && data.kp  !== undefined) extra += ' · Kp '  + data.kp.toFixed(1);
        if (data.xray_class) extra += ' · ' + data.xray_class;

        banner.innerHTML = icon + ' <b>' + title + '</b>'
            + (detail ? '<br><span style="font-size:11px;font-weight:400;opacity:.85">' + detail + extra + '</span>' : '');

        banner.onclick = function() {
            banner.style.opacity = '0';
            setTimeout(function(){
                banner.parentNode && banner.parentNode.removeChild(banner);
                if (!document.querySelector('[id^="prop-banner-"]')) _propBadgeClear();
            }, 300);
        };
        document.body.appendChild(banner);

        // Bip triple montant (distinct météo et ISS)
        try {
            var ctx = new (window.AudioContext || window.webkitAudioContext)();
            [520, 660, 820].forEach(function(freq, i) {
                var osc = ctx.createOscillator();
                var g   = ctx.createGain();
                osc.frequency.value = freq;
                osc.type = 'sine';
                g.gain.value = 0.18;
                osc.connect(g); g.connect(ctx.destination);
                osc.start(ctx.currentTime + i * 0.18);
                osc.stop (ctx.currentTime + i * 0.18 + 0.15);
            });
            setTimeout(function(){ try { ctx.close(); } catch(_){} }, 1200);
        } catch(_) {}

        // Auto-fermeture après 25 s
        setTimeout(function() {
            if (banner.parentNode) {
                banner.style.opacity = '0';
                setTimeout(function(){
                    banner.parentNode && banner.parentNode.removeChild(banner);
                    if (!document.querySelector('[id^="prop-banner-"]')) _propBadgeClear();
                }, 300);
            }
        }, 25000);
    }

    function propAlertToggle() {
        var tog = document.getElementById('prop-alert-toggle');
        if (!tog) return;
        fetch('/prop_alert_config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ enabled: tog.checked })
        }).catch(function(){});
    }

    function propAlertSave() {
        var kp   = document.getElementById('prop-kp-max');
        var sfi  = document.getElementById('prop-sfi-min');
        var xray = document.getElementById('prop-xray-class');
        var itvl = document.getElementById('prop-interval-min');
        fetch('/prop_alert_config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                kp_max:       kp   ? parseFloat(kp.value)   : 5.0,
                sfi_min:      sfi  ? parseFloat(sfi.value)  : 70.0,
                xray_class:   xray ? xray.value              : 'M1',
                interval_min: itvl ? parseInt(itvl.value)   : 15,
            })
        }).catch(function(){});
    }

    function propAlertTest(btn) {
        if (btn) btn.textContent = '⏳ Test en cours…';
        fetch('/prop_alert_test', { method: 'POST' })
            .then(function(r){ return r.json(); })
            .then(function(d){
                if (btn) btn.textContent = d.status === 'ok'
                    ? '✅ Test lancé — vérifiez les seuils'
                    : '❌ Erreur : ' + (d.error || '?');
                setTimeout(function(){ if (btn) btn.textContent = '🔬 Tester les alertes propagation'; }, 4000);
            })
            .catch(function(){ if (btn) { btn.textContent = '❌ Erreur réseau'; setTimeout(function(){ btn.textContent = '🔬 Tester les alertes propagation'; }, 4000); } });
    }

    // Charger config propagation au démarrage
    (function propAlertInit(){
        fetch('/prop_alert_config').then(function(r){ return r.json(); }).then(function(d){
            var tog  = document.getElementById('prop-alert-toggle');
            var kp   = document.getElementById('prop-kp-max');
            var sfi  = document.getElementById('prop-sfi-min');
            var xray = document.getElementById('prop-xray-class');
            var itvl = document.getElementById('prop-interval-min');
            if (tog)  tog.checked  = !!d.enabled;
            if (kp   && d.kp_max       !== undefined) kp.value   = d.kp_max;
            if (sfi  && d.sfi_min      !== undefined) sfi.value  = d.sfi_min;
            if (xray && d.xray_class   !== undefined) xray.value = d.xray_class;
            if (itvl && d.interval_min !== undefined) itvl.value = d.interval_min;
        }).catch(function(){});
    })();

    // ── Alertes Météo — WX_ALERT_UI_PATCH_v1 ─────────────────────────────────
    function wxAlertToggle() {
        var tog = document.getElementById('wx-alert-toggle');
        if (!tog) return;
        fetch('/weather_alert_config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ enabled: tog.checked })
        }).then(function(r){ return r.json(); }).then(function(){
            var st = document.getElementById('wx-alert-status');
            if (st) { st.textContent = tog.checked ? '✅ Alertes activées' : '⏹ Alertes désactivées'; setTimeout(function(){ st.textContent=''; }, 3000); }
        }).catch(function(){});
    }

    function wxAlertSave() {
        var v = function(id) { var e = document.getElementById(id); return e ? e.value : null; };
        var b = function(id) { var e = document.getElementById(id); return e ? e.checked : false; };
        var payload = {
            enabled:      b('wx-alert-toggle'),
            temp_max:     parseFloat(v('wx-temp-max'))  || 38,
            temp_min:     parseFloat(v('wx-temp-min'))  || -5,
            wind_max:     parseFloat(v('wx-wind-max'))  || 60,
            gust_max:     parseFloat(v('wx-gust-max'))  || 80,
            rain_mm:      parseFloat(v('wx-rain-mm'))   || 10,
            wmo_severe:   b('wx-wmo-severe'),
            interval_min:   parseInt(v('wx-interval-min')) || 30,
            bulletin_aprs:  b('wx-bulletin-aprs')
        };
        fetch('/weather_alert_config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        }).then(function(r){ return r.json(); }).then(function(){
            var st = document.getElementById('wx-alert-status');
            if (st) { st.textContent = '✅ Sauvegardé'; setTimeout(function(){ st.textContent=''; }, 2500); }
        }).catch(function(){});
    }

    function wxAlertTest(btn) {
        if (btn) btn.disabled = true;
        var st = document.getElementById('wx-alert-status');
        if (st) st.textContent = '⏳ Interrogation Open-Meteo…';
        fetch('/weather_alert_test', { method: 'POST' })
            .then(function(r){ return r.json(); })
            .then(function(d){
                if (btn) btn.disabled = false;
                if (st) { st.textContent = d.status === 'ok' ? '✅ Test déclenché — vérifiez les notifications' : '⚠️ ' + (d.error || 'Erreur'); setTimeout(function(){ st.textContent=''; }, 4000); }
            })
            .catch(function(){ if (btn) btn.disabled = false; if (st) { st.textContent = '❌ Erreur réseau'; setTimeout(function(){ st.textContent=''; }, 4000); } });
    }

    (function wxAlertInit(){
        fetch('/weather_alert_config').then(function(r){ return r.json(); }).then(function(d){
            var tog  = document.getElementById('wx-alert-toggle');
            var tmax = document.getElementById('wx-temp-max');
            var tmin = document.getElementById('wx-temp-min');
            var wmax = document.getElementById('wx-wind-max');
            var gmax = document.getElementById('wx-gust-max');
            var rmm  = document.getElementById('wx-rain-mm');
            var wmo  = document.getElementById('wx-wmo-severe');
            var itvl = document.getElementById('wx-interval-min');
            if (tog)  tog.checked  = !!d.enabled;
            if (tmax && d.temp_max     !== undefined) tmax.value = d.temp_max;
            if (tmin && d.temp_min     !== undefined) tmin.value = d.temp_min;
            if (wmax && d.wind_max     !== undefined) wmax.value = d.wind_max;
            if (gmax && d.gust_max     !== undefined) gmax.value = d.gust_max;
            if (rmm  && d.rain_mm      !== undefined) rmm.value  = d.rain_mm;
            if (wmo)  wmo.checked = (d.wmo_severe !== false);
            if (itvl && d.interval_min !== undefined) itvl.value = d.interval_min;
            var blnaprs = document.getElementById('wx-bulletin-aprs');
            if (blnaprs) blnaprs.checked = (d.bulletin_aprs !== false);
        }).catch(function(){});
    })();
    // ── FIN WX_ALERT_UI_PATCH_v1 ─────────────────────────────────────────────

    function _issUpdateDot(enabled, inPass) {
        var dot = document.getElementById('iss-active-dot');
        if (!dot) return;
        // Si enabled explicitement fourni, mémoriser l'état alerte
        if (enabled !== undefined) _issAlertEnabled = !!enabled;
        if (!_issAlertEnabled) {
            dot.style.display = 'none';
            return;
        }
        dot.style.display = 'inline-flex';
        var inner = dot.querySelector('span');
        if (inPass) {
            // 🟠 Passage en cours : orange/doré vif
            dot.style.color     = '#fbbf24';
            dot.style.animation = 'issDotPulse .8s ease-in-out infinite';
            if (inner) {
                inner.style.background = '#f59e0b';
                inner.style.boxShadow  = '0 0 8px #f59e0b, 0 0 16px #f59e0b88';
            }
            dot.title = 'ISS EN PASSAGE';
        } else {
            // 🟣 Alerte active, pas de passage : violet standard
            dot.style.color     = '#a78bfa';
            dot.style.animation = 'issDotPulse 2.5s ease-in-out infinite';
            if (inner) {
                inner.style.background = '#7c3aed';
                inner.style.boxShadow  = '0 0 6px #7c3aed';
            }
            dot.title = 'Alerte passage ISS activée';
        }
    }
    var _issAlertEnabled = false;

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

    // Rafraîchissement auto toutes les 15 min (rafraîchissement automatique toutes les 15 min)
    setInterval(issPassRefresh, 15 * 60 * 1000);

    // ── Passages ISS 24h glissantes ────────────────────────────────────────
    function iss24hRefresh() {
        var list = document.getElementById('iss-24h-list');
        var btn  = document.getElementById('iss-24h-btn');
        var st   = document.getElementById('iss-24h-status');
        if (list) list.innerHTML = '<span style="color:#475569;font-style:italic;font-size:10px">⏳ Calcul SGP4…</span>';
        if (btn)  btn.textContent = '…';
        fetch('/iss_passes?range=24h').then(function(r){ return r.json(); }).then(function(d){
            if (btn) btn.textContent = '↺ MAJ';
            if (d.error) {
                if (list) list.innerHTML = '<span style="color:#ef4444;font-size:10px">' + d.error + '</span>';
                return;
            }
            if (!d.passes || !d.passes.length) {
                if (list) list.innerHTML = '<span style="color:#475569;font-style:italic;font-size:10px">Aucun passage dans les 24 prochaines heures</span>';
                if (st)   st.textContent = 'Aucun passage';
                return;
            }
            var nowSec = Date.now() / 1000;
            var passes = d.passes.slice().sort(function(a,b){ return a.risetime - b.risetime; });
            var nextIdx = passes.findIndex(function(p){ return p.risetime > nowSec; });
            if (nextIdx < 0) nextIdx = passes.length - 1;

            if (st) st.textContent = passes.length + ' passage(s) · '
                + (d.lat !== undefined ? d.lat.toFixed(2) + '° / ' + d.lon.toFixed(2) + '°' : '');

            list.innerHTML = passes.map(function(p, i) {
                var inMin  = Math.round((p.risetime - nowSec) / 60);
                var maxEl  = (p.max_el !== undefined && p.max_el !== null) ? Math.round(p.max_el) : null;
                var dur    = p.duration_min;
                var riseAz = (p.rise_az !== undefined && p.rise_az !== null) ? Math.round(p.rise_az) : null;
                var setAz  = (p.set_az  !== undefined && p.set_az  !== null) ? Math.round(p.set_az)  : null;
                var isNext = (i === nextIdx);
                var isPast = inMin < -Math.round(dur || 5);

                // ── Couleurs selon statut temporel ──────────────────────────
                var badgeCol = isPast     ? '#334155'
                             : inMin <= 0 ? '#f472b6'
                             : inMin < 15 ? '#f472b6'
                             : inMin < 60 ? '#a78bfa'
                             : inMin < 240? '#67e8f9'
                             :              '#475569';

                // ── Qualité élévation ───────────────────────────────────────
                var elLabel = '—', elColor = '#475569', elBg = 'rgba(71,85,105,.12)';
                if (maxEl !== null) {
                    if      (maxEl >= 60) { elLabel = 'Excellent'; elColor = '#4ade80'; elBg = 'rgba(74,222,128,.10)'; }
                    else if (maxEl >= 30) { elLabel = 'Bon';       elColor = '#a78bfa'; elBg = 'rgba(167,139,250,.10)'; }
                    else if (maxEl >= 10) { elLabel = 'Passable';  elColor = '#67e8f9'; elBg = 'rgba(103,232,249,.10)'; }
                    else                  { elLabel = 'Rasant';    elColor = '#475569'; elBg = 'rgba(71,85,105,.12)'; }
                }

                // ── Probabilité contact APRS ────────────────────────────────
                var aprsProb = 0, aprsColor = '#475569', aprsLabel = 'Très faible';
                if (maxEl !== null) {
                    if      (maxEl < 5)  { aprsProb = 5;  aprsColor = '#475569'; aprsLabel = 'Très faible'; }
                    else if (maxEl < 10) { aprsProb = 12; aprsColor = '#ef4444'; aprsLabel = 'Faible'; }
                    else if (maxEl < 20) { aprsProb = 30; aprsColor = '#f97316'; aprsLabel = 'Possible'; }
                    else if (maxEl < 35) { aprsProb = 52; aprsColor = '#eab308'; aprsLabel = 'Probable'; }
                    else if (maxEl < 55) { aprsProb = 72; aprsColor = '#22c55e'; aprsLabel = 'Bonne'; }
                    else                 { aprsProb = 90; aprsColor = '#10b981'; aprsLabel = 'Excellente'; }
                    if (maxEl >= 5  && maxEl < 10) aprsProb = Math.round(5  + (maxEl-5)  / 5  * 15);
                    if (maxEl >= 10 && maxEl < 20) aprsProb = Math.round(20 + (maxEl-10) / 10 * 20);
                    if (maxEl >= 20 && maxEl < 35) aprsProb = Math.round(40 + (maxEl-20) / 15 * 25);
                    if (maxEl >= 35 && maxEl < 55) aprsProb = Math.round(65 + (maxEl-35) / 20 * 15);
                    if (maxEl >= 55 && maxEl < 80) aprsProb = Math.round(80 + (maxEl-55) / 25 * 15);
                    if (maxEl >= 80)               aprsProb = 95;
                }

                // ── Libellé jour ─────────────────────────────────────────────
                var rdt  = new Date(p.risetime * 1000);
                var tod  = new Date(); tod.setHours(0,0,0,0);
                var rdtd = new Date(rdt); rdtd.setHours(0,0,0,0);
                var isToday    = (rdtd.getTime() === tod.getTime());
                var tomorrow   = new Date(tod); tomorrow.setDate(tod.getDate()+1);
                var isTomorrow = (rdtd.getTime() === tomorrow.getTime());
                var dayTag = isToday ? ''
                    : isTomorrow
                        ? '<span style="display:inline-block;background:rgba(245,158,11,.15);color:#f59e0b;font-size:9px;font-weight:700;border-radius:4px;padding:0 5px;margin-right:5px;text-transform:uppercase;letter-spacing:.05em">demain</span>'
                        : '<span style="display:inline-block;background:rgba(245,158,11,.12);color:#f59e0b;font-size:9px;font-weight:700;border-radius:4px;padding:0 5px;margin-right:5px">' + rdt.toLocaleDateString("fr-FR",{day:"2-digit",month:"2-digit"}) + '</span>';

                // ── Libellé statut / countdown ────────────────────────────────
                var statusTxt = isPast     ? 'PASSÉ'
                    : inMin <= 0           ? 'EN COURS'
                    : inMin < 60           ? 'dans ' + inMin + ' min'
                    : Math.round(inMin/60) + 'h' + (inMin%60 ? String(inMin%60).padStart(2,'0') : '');

                // ── Fond de carte ─────────────────────────────────────────────
                var cardStyle = isNext
                    ? 'background:rgba(124,58,237,.12);border:1px solid rgba(124,58,237,.35)'
                    : isPast
                        ? 'background:rgba(10,15,25,.35);border:1px solid rgba(30,41,59,.3);opacity:.5'
                        : 'background:rgba(15,23,42,.55);border:1px solid rgba(30,41,59,.7)';

                var h = '<div style="border-radius:12px;padding:10px 12px;' + cardStyle + ';margin-bottom:2px">';

                // ══ LIGNE 1 : heure + badge statut ═══════════════════════════
                h += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:7px">';

                // Heure AOS → LOS
                h += '<div style="display:flex;align-items:baseline;gap:6px">';
                if (isNext) h += '<span style="font-size:14px;line-height:1">🛸</span>';
                h += dayTag;
                h += '<span style="font-family:monospace;font-size:' + (isNext?'14':'12') + 'px;font-weight:' + (isNext?'800':'600') + ';color:' + (isPast?'#475569':isNext?'#e2e8f0':'#94a3b8') + ';letter-spacing:.02em">';
                h +=   p.risetime_fmt;
                h += '</span>';
                if (p.set_fmt && !isPast) {
                    h += '<span style="font-family:monospace;font-size:10px;color:#475569;font-weight:400">→ ' + p.set_fmt + '</span>';
                }
                h += '</div>';

                // Badge statut
                h += '<span style="font-size:9px;font-weight:700;color:' + badgeCol + ';background:' + badgeCol + '1a;border:1px solid ' + badgeCol + '30;border-radius:6px;padding:2px 8px;white-space:nowrap;letter-spacing:.04em;text-transform:uppercase">';
                h +=   statusTxt;
                h += '</span>';
                h += '</div>';

                if (!isPast) {
                    // ══ LIGNE 2 : métriques orbitales ════════════════════════
                    h += '<div style="display:flex;align-items:center;gap:10px;padding:6px 8px;background:rgba(0,0,0,.2);border-radius:8px;margin-bottom:6px">';

                    // Durée
                    h += '<div style="display:flex;flex-direction:column;align-items:center;min-width:36px">';
                    h +=   '<span style="font-family:monospace;font-size:13px;font-weight:700;color:#94a3b8;line-height:1">' + dur + '</span>';
                    h +=   '<span style="font-size:8px;color:#475569;text-transform:uppercase;letter-spacing:.06em;margin-top:2px">min</span>';
                    h += '</div>';

                    h += '<div style="width:1px;height:28px;background:rgba(71,85,105,.3)"></div>';

                    // Élévation max
                    if (maxEl !== null) {
                        h += '<div style="display:flex;flex-direction:column;align-items:center;min-width:38px">';
                        h +=   '<span style="font-family:monospace;font-size:13px;font-weight:700;color:' + elColor + ';line-height:1">' + maxEl + '°</span>';
                        h +=   '<span style="font-size:8px;color:#475569;text-transform:uppercase;letter-spacing:.06em;margin-top:2px">élév. max</span>';
                        h += '</div>';

                        h += '<div style="width:1px;height:28px;background:rgba(71,85,105,.3)"></div>';

                        // Badge qualité passage
                        h += '<div style="flex:1;display:flex;align-items:center">';
                        h +=   '<span style="font-size:10px;font-weight:700;color:' + elColor + ';background:' + elBg + ';border-radius:6px;padding:3px 8px">';
                        h +=     (maxEl >= 60 ? '⭐ ' : maxEl >= 30 ? '✦ ' : maxEl >= 10 ? '· ' : '↙ ') + elLabel;
                        h +=   '</span>';
                        h += '</div>';

                        // Azimut AOS → LOS (boussole compacte)
                        if (riseAz !== null) {
                            var _AZ_CARDS2 = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSO','SO','OSO','O','ONO','NO','NNO'];
                            var rCard = _AZ_CARDS2[Math.round(riseAz / 22.5) % 16];
                            var sCard = setAz !== null ? _AZ_CARDS2[Math.round(setAz / 22.5) % 16] : null;
                            // Aiguille sur cercle SVG (r=9, centre 11,11)
                            var r9 = 8;
                            var rxN = (11 + r9 * Math.cos((riseAz - 90) * Math.PI / 180)).toFixed(1);
                            var ryN = (11 + r9 * Math.sin((riseAz - 90) * Math.PI / 180)).toFixed(1);
                            h += '<div style="width:1px;height:28px;background:rgba(71,85,105,.3)"></div>';
                            h += '<div style="display:flex;align-items:center;gap:5px">';
                            // Mini boussole SVG
                            h += '<svg width="22" height="22" viewBox="0 0 22 22" style="flex-shrink:0">';
                            h +=   '<circle cx="11" cy="11" r="10" fill="rgba(15,23,42,.6)" stroke="rgba(71,85,105,.5)" stroke-width="1"/>';
                            h +=   '<text x="11" y="5"  text-anchor="middle" font-size="4" fill="#475569" font-family="monospace">N</text>';
                            h +=   '<text x="11" y="20" text-anchor="middle" font-size="4" fill="#475569" font-family="monospace">S</text>';
                            h +=   '<text x="3"  y="13" text-anchor="middle" font-size="4" fill="#475569" font-family="monospace">O</text>';
                            h +=   '<text x="19" y="13" text-anchor="middle" font-size="4" fill="#475569" font-family="monospace">E</text>';
                            // Aiguille AOS (cyan)
                            h +=   '<line x1="11" y1="11" x2="' + rxN + '" y2="' + ryN + '" stroke="#67e8f9" stroke-width="2" stroke-linecap="round"/>';
                            h +=   '<circle cx="11" cy="11" r="1.5" fill="#67e8f9"/>';
                            // Aiguille LOS (si dispo, violet atténué)
                            if (setAz !== null) {
                                var sxN = (11 + r9 * Math.cos((setAz - 90) * Math.PI / 180)).toFixed(1);
                                var syN = (11 + r9 * Math.sin((setAz - 90) * Math.PI / 180)).toFixed(1);
                                h +=   '<line x1="11" y1="11" x2="' + sxN + '" y2="' + syN + '" stroke="#a78bfa" stroke-width="1.5" stroke-linecap="round" opacity=".5" stroke-dasharray="2,2"/>';
                            }
                            h += '</svg>';
                            // Texte AOS → LOS
                            h += '<div style="display:flex;flex-direction:column;gap:1px">';
                            h +=   '<span style="font-family:monospace;font-size:9px;font-weight:700;color:#67e8f9;letter-spacing:.03em">' + riseAz + '° ' + rCard + '</span>';
                            if (sCard) {
                                h += '<span style="font-family:monospace;font-size:8px;color:#a78bfa;opacity:.7">' + setAz + '° ' + sCard + '</span>';
                            }
                            h +=   '<span style="font-size:7px;color:#334155;text-transform:uppercase;letter-spacing:.06em">AOS→LOS</span>';
                            h += '</div>';
                            h += '</div>';
                        }
                    }

                    // Fréquences (compact, à droite)
                    h += '<div style="margin-left:auto;text-align:right">';
                    h +=   '<div style="font-family:monospace;font-size:8px;color:#334155;line-height:1.5">145.825</div>';
                    h +=   '<div style="font-family:monospace;font-size:8px;color:#2d3748;line-height:1.5">437.550</div>';
                    h += '</div>';
                    h += '</div>';

                    // ══ LIGNE 3 : probabilité contact APRS ═══════════════════
                    if (maxEl !== null) {
                        var elPct = Math.min(100, Math.round(maxEl / 90 * 100));
                        var barCol = maxEl >= 60 ? '#4ade80' : maxEl >= 30 ? '#a78bfa' : maxEl >= 10 ? '#67e8f9' : '#475569';
                        h += '<div style="display:flex;align-items:center;gap:10px;padding:7px 8px;background:rgba(0,0,0,.25);border-radius:8px">';

                        // Colonne gauche : label + double barre (élév + APRS)
                        h +=   '<div style="flex:1;min-width:0">';
                        h +=     '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">';
                        h +=       '<span style="font-size:8px;font-weight:700;color:#475569;text-transform:uppercase;letter-spacing:.07em">Élév.</span>';
                        h +=       '<span style="font-size:8px;color:' + elColor + ';font-weight:600">' + maxEl + '°</span>';
                        h +=     '</div>';
                        h +=     '<div style="height:3px;background:#0f172a;border-radius:2px;overflow:hidden;margin-bottom:6px">';
                        h +=       '<div style="width:' + elPct + '%;height:100%;background:' + barCol + ';border-radius:2px"></div>';
                        h +=     '</div>';
                        h +=     '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">';
                        h +=       '<span style="font-size:8px;font-weight:700;color:#475569;text-transform:uppercase;letter-spacing:.07em">📡 Contact APRS</span>';
                        h +=       '<span style="font-size:8px;color:' + aprsColor + ';font-weight:600">' + aprsLabel + '</span>';
                        h +=     '</div>';
                        h +=     '<div style="height:4px;background:#0f172a;border-radius:3px;overflow:hidden">';
                        h +=       '<div style="width:' + aprsProb + '%;height:100%;background:' + aprsColor + ';border-radius:3px;box-shadow:0 0 5px ' + aprsColor + '77"></div>';
                        h +=     '</div>';
                        h +=   '</div>';

                        // Colonne droite : % en grand
                        h +=   '<div style="text-align:center;min-width:46px;border-left:1px solid rgba(71,85,105,.25);padding-left:10px">';
                        h +=     '<div style="font-family:monospace;font-size:20px;font-weight:900;line-height:1;color:' + aprsColor + '">' + aprsProb + '%</div>';
                        h +=     '<div style="font-size:8px;color:#475569;margin-top:2px;text-transform:uppercase;letter-spacing:.05em">liaison</div>';
                        h +=   '</div>';
                        h += '</div>';
                    }
                }

                h += '</div>';
                return h;
            }).join('');
        }).catch(function(err){
            if (btn)  btn.textContent = '↺ MAJ';
            if (list) list.innerHTML = '<span style="color:#ef4444;font-size:10px">Erreur chargement</span>';
        });
    }
    // Lancer au chargement de l'onglet ISS
    iss24hRefresh();
    setInterval(iss24hRefresh, 15 * 60 * 1000);

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
        // Vérifier que l'ISS est bien en vue avant d'émettre
        var btn = document.getElementById("btn-iss-send");
        try {
            var r = await fetch("/iss_now");
            var d = await r.json();
            if (!d.visible) {
                if (btn) { var old = btn.textContent; btn.textContent = "❌ ISS hors de portée"; setTimeout(function(){ btn.textContent = old; }, 3000); }
                return;
            }
        } catch(e) { /* on tente quand même si /iss_now échoue */ }

        if (btn) { btn.textContent = "⏳ Émission…"; btn.disabled = true; }
        try {
            await fetch('/send_raw', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ message: "Contact via ARISS / ISS", is_iss: true })
            });
            if (btn) { btn.textContent = "✅ Envoyé !"; }
        } catch(e) {
            if (btn) { btn.textContent = "❌ Erreur"; }
        }
        setTimeout(function(){
            if (btn) { btn.textContent = "📡 BEACON"; btn.disabled = false; }
        }, 3000);
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

    async function sendText1(btn) {
        var orig = btn.textContent;
        btn.disabled = true;
        btn.textContent = '⏳ Envoi...';
        try {
            var r = await fetch('/send_text1', { method: 'POST' });
            var d = await r.json();
            btn.textContent = d.error ? ('❌ ' + d.error) : '✅ Envoyé';
        } catch(e) { btn.textContent = '❌ Erreur réseau'; }
        setTimeout(function(){ btn.textContent = orig; btn.disabled = false; }, 3000);
    }

    async function sendText2(btn) {
        var orig = btn.textContent;
        btn.disabled = true;
        btn.textContent = '⏳ Envoi...';
        try {
            var r = await fetch('/send_text2', { method: 'POST' });
            var d = await r.json();
            btn.textContent = d.error ? ('❌ ' + d.error) : '✅ Envoyé';
        } catch(e) { btn.textContent = '❌ Erreur réseau'; }
        setTimeout(function(){ btn.textContent = orig; btn.disabled = false; }, 3000);
    }

    function clearConsole() { document.getElementById('console').innerHTML = ''; }

    // ── Handler délégué : bouton "trame brute" ───────────────────────────────
    document.addEventListener('click', function(ev) {
        if (ev.target && ev.target.classList.contains('raw-toggle')) {
            var body = ev.target.parentNode.querySelector('.raw-body');
            if (body) body.style.display = body.style.display === 'none' ? 'block' : 'none';
        }
    });

    // ── Compteur IS ─────────────────────────────────────────────────────────
    var _isCount = 0;
    function _incIsCount() {
        _isCount++;
        var el = document.getElementById('is-count');
        if (el) el.textContent = _isCount;
    }

    // ── Filtre moniteur ─────────────────────────────────────────────────────
    function applyMonitorFilter() {
        var val = (document.getElementById('monitor-filter') || {}).value || '';
        var cards = document.querySelectorAll('#console .aprs-card');
        cards.forEach(function(card) {
            if (!val) { card.style.display = ''; return; }
            var html = card.innerHTML.toLowerCase();
            var show = false;
            if (val === 'RX') show = html.indexOf('📥 rx') !== -1;
            else if (val === 'TX') show = html.indexOf('📤 tx') !== -1;
            else if (val === 'IS') show = html.indexOf('🌐 is') !== -1;
            else if (val === 'pos') show = html.indexOf('📍pos') !== -1;
            else if (val === 'msg') show = html.indexOf('💬') !== -1;
            else if (val === 'wx') show = html.indexOf('🌡️') !== -1 || html.indexOf('💧') !== -1 || html.indexOf('💨') !== -1;
            card.style.display = show ? '' : 'none';
        });
    }

    // ── Beacon auto ────────────────────────────────────────────────────────
    var _beaconRemaining = null;
    var _beaconInterval  = 0;

    async function pollBeaconStatus() {
        try {
            var r = await fetch('/beacon_status');
            var d = await r.json();
            // d.schedules = {station:{interval,next_in}, meteo:{...}, ...}
            var schedules = d.schedules || {};

            var TYPES = ['station', 'iss', 'meteo', 'propagation', 'text1', 'text2'];
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
        ['station','iss','meteo','propagation','text1','text2'].forEach(function(t) {
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
        // Digipeater
        data.digi_enabled   = document.getElementById('digi_enabled') ? document.getElementById('digi_enabled').checked : false;
        data.digi_aliases   = (data.digi_aliases || '').split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
        data.digi_limit     = parseInt(data.digi_limit) || 2;
        // Mode HF 300 bauds
        data.hf_mode        = document.getElementById('hf_mode') ? document.getElementById('hf_mode').checked : false;
        data.hf_mark_hz     = parseInt(data.hf_mark_hz)  || 1600;
        data.hf_space_hz    = parseInt(data.hf_space_hz) || 1800;
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

    // ── Constante destination ISS ─────────────────────────────────────────────
    var _ISS_DEST    = 'RS0ISS-4';   // Digipeater ISS principal (ARISS)
    var _ISS_PATH    = 'ARISS';      // Path requis pour passer via l'ISS
    var _issMode     = false;        // true quand le contact actif est ISS

    function openCQGeneral() {
        activeContact = 'APRS';
        _issMode = false;
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
        document.getElementById('chat-delete-btn').disabled = false;
        document.getElementById('chat-input').placeholder = 'CQ CQ DE F1RIQ — 67 car. max...';
        document.getElementById('chat-input').focus();
        _renderMacroBar();
        _macroSetEnabled(true);
        loadHistory('APRS');
        switchTab('qso');
    }

    function openISS() {
        activeContact = _ISS_DEST;
        _issMode = true;
        document.getElementById('chat-title').innerHTML =
            '<span style="color:#818cf8;font-weight:900;letter-spacing:.04em">🛸 ISS / ARISS</span>'
            + ' <span style="font-size:9px;color:#6366f1;font-family:monospace">→ ' + _ISS_DEST + '</span>';
        document.getElementById('chat-subtitle').innerHTML =
            '<span style="color:#6366f1">PATH : <b>' + _ISS_PATH + '</b></span>'
            + ' &nbsp;·&nbsp; QRG 145.825 MHz FM';
        document.getElementById('chat-avatar').innerHTML =
            '<span style="font-size:16px">🛸</span>';
        document.getElementById('chat-avatar').style.background =
            'linear-gradient(135deg,#1e1b4b,#3730a3)';
        document.getElementById('chat-avatar').style.border = '1px solid #818cf8';
        document.getElementById('chat-input').disabled    = false;
        document.getElementById('chat-send-btn').disabled = false;
        document.getElementById('chat-delete-btn').disabled = false;
        document.getElementById('chat-input').placeholder =
            'Message ISS via ARISS — 67 car. max...';
        document.getElementById('chat-input').focus();
        _renderMacroBar();
        _macroSetEnabled(true);
        loadHistory(_ISS_DEST);
        switchTab('qso');
    }

    function openContact(cs) {
        cs = cs.trim().toUpperCase();
        if (!cs) return;
        activeContact = cs;
        _issMode = false;
        document.getElementById('new-contact-input').value = '';
        document.getElementById('chat-title').innerHTML = cs + ' ' + qrzLink(cs, {cls:'text-[10px] text-slate-500 hover:text-blue-400 font-mono transition-colors align-middle'});
        document.getElementById('chat-subtitle').textContent = 'APRS - 144.800 MHz';
        document.getElementById('chat-avatar').textContent = cs.substring(0, 2);
        document.getElementById('chat-input').disabled    = false;
        document.getElementById('chat-send-btn').disabled = false;
        document.getElementById('chat-delete-btn').disabled = false;
        document.getElementById('chat-input').placeholder = 'Message APRS 67 car. max...';
        document.getElementById('chat-avatar').style.background = '';
        document.getElementById('chat-avatar').style.border = '';
        document.getElementById('chat-input').focus();
        _renderMacroBar();
        _macroSetEnabled(true);
        loadHistory(cs);
        switchTab('qso');
    }

    async function deleteQso() {
        if (!activeContact) return;
        if (!confirm('Supprimer toute la conversation avec ' + activeContact + ' ?')) return;
        var r = await fetch('/chat/delete/' + encodeURIComponent(activeContact), { method: 'DELETE' });
        var d = await r.json();
        if (d.ok) {
            // Réinitialiser le panneau chat
            activeContact = null;
            _issMode = false;
            document.getElementById('chat-title').textContent = 'Selectionner un contact';
            document.getElementById('chat-subtitle').textContent = 'APRS Point-a-Point';
            document.getElementById('chat-avatar').textContent = '?';
            document.getElementById('chat-avatar').style.background = '';
            document.getElementById('chat-avatar').style.border = '';
            document.getElementById('chat-input').disabled    = true;
            document.getElementById('chat-send-btn').disabled = true;
            document.getElementById('chat-delete-btn').disabled = true;
            document.getElementById('chat-messages').innerHTML =
                '<div class="text-center text-slate-600 text-xs italic mt-20">👆 Choisissez un contact pour demarrer</div>';
            _macroSetEnabled(false);
            refreshContacts();
        } else {
            alert('Erreur : ' + (d.error || 'impossible de supprimer'));
        }
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
            var ackState = m.acked
                ? '<span style="color:#34d399;font-size:9px;margin-left:4px" title="ACK reçu">✓ ACK</span>'
                : m.acked_failed
                    ? '<span style="color:#f87171;font-size:9px;margin-left:4px" title="Pas de réponse après 3 essais">✗ TIMEOUT</span>'
                    : m.msgno
                        ? '<span id="ack-badge-'+esc(m.msgno)+'" style="color:#94a3b8;font-size:9px;margin-left:4px" title="En attente de confirmation">⏳</span>'
                        : '';
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
                      +     esc(m.text) + ackState
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
    var _CALLSIGN_CFG = __CALLSIGN_JSON__;
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

    // Groupe 3 : macros ISS / ARISS — envoi avec path forcé ARISS
    var _macrosISS = [
        { label:'CQ ARISS', text:'CQ CQ ARISS DE {MY} LOC {LOC} QRZ?' },
        { label:'QTH',      text:'DE {MY} QTH LOC {LOC} APRS VIA ISS' },
        { label:'73 ISS',   text:'73 DE {MY} TNX ARISS QSO GL' },
        { label:'PSE QSL',  text:'PSE QSL DE {MY} VIA ISS ARISS' },
        { label:'Test',     text:'TEST DE {MY} PSE QSL VIA ARISS' },
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
        var isISS   = (group === 'iss');
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
        } else if (isISS) {
            // Bouton ISS : remplit le champ (envoi manuel pour relecture)
            btn.textContent = m.label;
            btn.title = _macroExpand(m.text);
            btn.className = baseClass
                + 'bg-indigo-950/60 border border-indigo-600/60 text-indigo-300 '
                + 'hover:bg-indigo-600 hover:border-indigo-400 hover:text-white';
            btn.onclick = function() {
                var inp2 = document.getElementById('chat-input');
                if (!inp2 || inp2.disabled) return;
                var txt = _macroExpand(_macrosISS[parseInt(this.getAttribute('data-idx'))].text).substring(0, 67);
                inp2.value = txt;
                inp2.focus();
                var cc = document.getElementById('chat-charcount');
                if (cc) cc.textContent = txt.length + '/67';
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

        if (_issMode) {
            // ── Mode ISS : groupe ISS uniquement ──────────────────────────
            var lblIss = document.createElement('span');
            lblIss.innerHTML = '🛸 ARISS';
            lblIss.style.cssText = 'font-size:8px;font-weight:900;color:#6366f1;'
                                 + 'text-transform:uppercase;letter-spacing:.08em;'
                                 + 'align-self:center;white-space:nowrap;margin-right:4px';
            bar.appendChild(lblIss);

            _macrosISS.forEach(function(m, idx) {
                bar.appendChild(_makeMacroBtn(m, idx, 'iss'));
            });

            // Badge path ARISS
            var badge = document.createElement('span');
            badge.textContent = 'path: ARISS';
            badge.style.cssText = 'font-size:9px;font-weight:700;color:#818cf8;'
                                + 'background:#1e1b4b;border:1px solid #4338ca;'
                                + 'border-radius:6px;padding:1px 6px;'
                                + 'align-self:center;margin-left:auto;white-space:nowrap';
            bar.appendChild(badge);
        } else {
            // ── Mode normal : QSO + APRS ──────────────────────────────────
            var lblQso = document.createElement('span');
            lblQso.textContent = 'QSO';
            lblQso.style.cssText = 'font-size:8px;font-weight:900;color:#475569;'
                                 + 'text-transform:uppercase;letter-spacing:.08em;'
                                 + 'align-self:center;white-space:nowrap;margin-right:2px';
            bar.appendChild(lblQso);

            _macrosQso.forEach(function(m, idx) {
                bar.appendChild(_makeMacroBtn(m, idx, 'qso'));
            });

            // ── Séparateur ────────────────────────────────────────────────
            var sep = document.createElement('span');
            sep.style.cssText = 'width:1px;height:18px;background:#334155;'
                              + 'align-self:center;flex-shrink:0;margin:0 4px';
            bar.appendChild(sep);

            // ── Groupe APRS (envoi direct) ────────────────────────────────
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
        var payload = { dest: activeContact, text: text };
        if (_issMode) payload.custom_path = _ISS_PATH;
        await fetch('/chat/send', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
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
    var _mapMarkers = {};   // callsign → { marker, polyline, emoji, trail:[[lat,lon],…] }
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
        var _tileUrl = (typeof _isDayMode === 'function' && _isDayMode())
            ? 'https://{s}.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png'
            : 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png';
        L.tileLayer(_tileUrl, { maxZoom: 20, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributeurs' }).addTo(_map);
        // Rejouer les stations déjà en mémoire
        Object.keys(_mapMarkers).forEach(function(cs) {
            var m = _mapMarkers[cs];
            if (m.lat && m.lon) _placeMarker(cs, m.lat, m.lon, m.emoji, m.popup);
        });
    }

    // Symboles APRS mobiles — doit correspondre à _TRAIL_SYMBOLS Python
    var _TRAIL_SYM = {'/j':1,'/k':1,'/u':1,'/v':1,'/o':1,'/O':1,'/f':1,'/g':1,
        '/[':1,'/X':1,'/Y':1,'/a':1,'/s':1,'/>':1,'/<':1,'/^':1,'/n':1,'/r':1,
        '\\\\j':1,'\\\\k':1,'\\\\u':1,'\\\\v':1,'\\\\a':1,'\\\\s':1,'\\\\>':1,'\\\\<':1,'\\\\^':1};
    function _isMobileSym(sym) {
        if (!sym) return false;
        var s = sym.trim().toLowerCase();
        if (_TRAIL_SYM[s]) return true;
        return false;
    }

    // Palette de couleurs par station (hachage de l'indicatif)
    function _stationColor(cs) {
        var palette = ['#f97316','#3b82f6','#10b981','#ec4899','#f59e0b','#8b5cf6','#06b6d4','#84cc16'];
        var h = 0;
        for (var i = 0; i < cs.length; i++) h = (h * 31 + cs.charCodeAt(i)) & 0xffff;
        return palette[h % palette.length];
    }

    function _placeMarker(cs, lat, lon, emoji, popupHtml) {
        var m = _mapMarkers[cs];
        var trail = m.trail || [];
        var course = m.lastCourse || -1;

        // ── Icône avec rotation selon le cap ───────────────────────────────
        var rotStyle = (course >= 0) ? 'transform:rotate(' + course + 'deg);' : '';
        var icon = L.divIcon({
            className: '',
            html: '<div style="display:flex;flex-direction:column;align-items:center;gap:1px">'
                + '<div style="font-size:22px;line-height:1;filter:drop-shadow(0 1px 2px #000a);' + rotStyle + '">' + emoji + '</div>'
                + '<div style="background:rgba(15,23,42,0.9);color:#60a5fa;border:1px solid #334155;'
                + 'border-radius:4px;padding:1px 5px;font-size:9px;font-weight:700;font-family:monospace;white-space:nowrap;letter-spacing:.04em">' + cs + '</div>'
                + '</div>',
            iconAnchor: [13, 32]
        });
        if (m.marker) {
            m.marker.setLatLng([lat, lon]).setIcon(icon).setPopupContent(popupHtml);
        } else {
            m.marker = L.marker([lat, lon], {icon: icon}).bindPopup(popupHtml, {maxWidth: 260}).addTo(_map);
        }

        // ── Trail : segments avec dégradé temporel ────────────────────────
        var stColor = _stationColor(cs);

        // Supprimer les anciens segments et waypoints
        if (m.trailLayers) {
            m.trailLayers.forEach(function(l){ _map.removeLayer(l); });
        }
        m.trailLayers = [];

        if (trail.length >= 2) {
            var now = Date.now() / 1000;
            var oldest = trail[0][2] || (now - 3600);
            var span   = Math.max(now - oldest, 1);

            for (var i = 0; i < trail.length - 1; i++) {
                var p1 = trail[i], p2 = trail[i+1];
                var age = (now - (p1[2] || now)) / span; // 0=récent, 1=ancien
                age = Math.min(Math.max(age, 0), 1);
                // Opacité : 0.15 (vieux) → 0.9 (récent)
                var opacity = 0.15 + (1 - age) * 0.75;
                // Largeur : 1.5 (vieux) → 3.5 (récent)
                var weight  = 1.5 + (1 - age) * 2;
                var seg = L.polyline([[p1[0],p1[1]], [p2[0],p2[1]]], {
                    color: stColor, weight: weight, opacity: opacity,
                    lineJoin: 'round', lineCap: 'round'
                }).addTo(_map);
                m.trailLayers.push(seg);
            }

            // Waypoints : petits cercles sur chaque position intermédiaire
            for (var j = 0; j < trail.length - 1; j++) {
                var pt = trail[j];
                var ptAge = (now - (pt[2] || now)) / span;
                ptAge = Math.min(Math.max(ptAge, 0), 1);
                var ptOpacity = 0.2 + (1 - ptAge) * 0.6;
                var ptRadius  = 1.5 + (1 - ptAge) * 2;

                // Tooltip heure + vitesse
                var ptTime = pt[2] ? new Date(pt[2]*1000).toLocaleTimeString('fr-FR',{hour:'2-digit',minute:'2-digit'}) : '';
                var ptSpd  = (pt[3] !== undefined && pt[3] >= 0) ? pt[3] + ' km/h' : '';
                var ptTip  = ptTime + (ptSpd ? ' · ' + ptSpd : '');

                var dot = L.circleMarker([pt[0], pt[1]], {
                    radius: ptRadius, color: stColor, weight: 1,
                    fillColor: stColor, fillOpacity: ptOpacity, opacity: ptOpacity
                }).addTo(_map);
                if (ptTip) dot.bindTooltip(ptTip, {permanent: false, direction: 'top', className: 'aprs-trail-tip'});
                m.trailLayers.push(dot);
            }
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
        var _rawBrng    = (_stationLat !== null && _stationLon !== null)
            ? bearingDeg(_stationLat, _stationLon, lat, lon) : null;
        // Pour un objet, préférer la distance/azimut de position propre de la station
        var _distKm = (_isObjFrame && _stationPosDistKm[cs] !== undefined)
            ? _stationPosDistKm[cs] : _rawDistKm;
        var _brngMap = (_isObjFrame && _stationPosBearing[cs] !== undefined)
            ? _stationPosBearing[cs] : _rawBrng;
        // Mémoriser si c'est une trame de position propre
        if (!_isObjFrame && _rawDistKm !== null) {
            _stationPosDistKm[cs]  = _rawDistKm;
            _stationPosBearing[cs] = _rawBrng;
        }
        var _distLabel = _distKm !== null
            ? (_distKm < 10 ? _distKm.toFixed(1) : Math.round(_distKm)) + ' km' : null;
        var _brngLabel = _brngMap !== null
            ? bearingArrow(_brngMap) + ' ' + Math.round(_brngMap) + '°' : null;
        var _distColor = _distKm === null ? '' : _distKm < 50 ? '#34d399' : _distKm < 150 ? '#fbbf24' : '#fb923c';
        // ── Popup station mobile enrichi ─────────────────────────────────
        var _stClr = _stationColor(cs);
        var popup =
            '<div style="font-family:system-ui;min-width:180px">'
            + '<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">'
            + '<span style="font-size:20px">' + emoji + '</span>'
            + '<div>'
            + '<div style="font-size:13px;font-weight:900;font-family:monospace;color:#f1f5f9">' + cs + '</div>'
            + '<div style="font-size:10px;color:#64748b">' + (frame.aprs_type || '') + '</div>'
            + '</div>'
            + (_distLabel ? '<span style="margin-left:auto;font-size:11px;color:' + _distColor + ';font-weight:700;white-space:nowrap">🧭 ' + _distLabel + '</span>' : '')
            + '</div>'
            + (e.speed_kmh !== undefined ? '<div style="font-size:11px;color:#94a3b8;margin-bottom:2px">🏎️ <b style="color:#f1f5f9">' + e.speed_kmh + ' km/h</b>&ensp;🧭 ' + (e.course !== undefined ? e.course + '°' : '?') + (_brngLabel ? '&ensp;' + _brngLabel : '') + '</div>' : '')
            + (e.alt_m     !== undefined ? '<div style="font-size:11px;color:#94a3b8;margin-bottom:2px">⬆️ <b style="color:#f1f5f9">' + e.alt_m + ' m</b></div>' : '')
            + (e.comment && e.comment.length ? '<div style="font-size:11px;color:#cbd5e1;margin-bottom:4px;font-style:italic">💬 ' + esc(e.comment) + '</div>' : '');

        // Dernières positions du trail
        var _trailNow = (_mapMarkers[cs] && _mapMarkers[cs].trail) ? _mapMarkers[cs].trail : [];
        if (_trailNow.length > 1) {
            popup += '<div style="margin-top:6px;border-top:1px solid #1e293b;padding-top:5px">'
                   + '<div style="font-size:9px;color:#475569;text-transform:uppercase;letter-spacing:.08em;margin-bottom:3px">Historique (' + _trailNow.length + ' pts)</div>';
            var _showPts = _trailNow.slice(-5).reverse();
            _showPts.forEach(function(pt, idx) {
                var _ptT = pt[2] ? new Date(pt[2]*1000).toLocaleTimeString('fr-FR',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '';
                var _ptS = (pt[3] !== undefined && pt[3] >= 0) ? pt[3] + ' km/h' : '';
                var _ptC = (pt[4] !== undefined && pt[4] >= 0) ? pt[4] + '°' : '';
                var _ptLl = pt[0].toFixed(4) + ', ' + pt[1].toFixed(4);
                popup += '<div style="font-size:10px;color:' + (idx===0?'#cbd5e1':'#64748b') + ';font-family:monospace;padding:1px 0">'
                       + (idx===0?'▶ ':'  ')
                       + _ptT
                       + (_ptS ? ' <span style="color:' + _stClr + '">' + _ptS + '</span>' : '')
                       + (_ptC ? ' <span style="color:#94a3b8">' + _ptC + '</span>' : '')
                       + '</div>';
            });
            popup += '</div>';
        }

        popup += '<div style="font-size:9px;color:#475569;margin-top:5px">'
               + new Date().toLocaleTimeString('fr-FR') + '</div>'
               + '<div id="mapcity-' + cs.replace(/[^a-z0-9]/gi,'_') + '" style="font-size:10px;color:#7dd3fc;margin-top:3px;font-style:italic">🏙️ …</div>'
               + '</div>';

        // Injection asynchrone de la ville dans le popup Leaflet
        (function(_cs, _lat, _lon) {
            fetch('/nearest_city?lat=' + _lat + '&lon=' + _lon)
                .then(function(r){ return r.ok ? r.json() : null; })
                .then(function(d) {
                    var el = document.getElementById('mapcity-' + _cs.replace(/[^a-z0-9]/gi,'_'));
                    if (el && d && d.display) {
                        el.innerHTML = '🏙️ <b style="color:#bae6fd">' + esc(d.display) + '</b>';
                        el.style.fontStyle = 'normal';
                    } else if (el) { el.style.display = 'none'; }
                })
                .catch(function(){ var el=document.getElementById('mapcity-' + _cs.replace(/[^a-z0-9]/gi,'_')); if(el) el.style.display='none'; });
        })(cs, lat, lon);

        if (!_mapMarkers[cs]) {
            _mapMarkers[cs] = { lat: lat, lon: lon, emoji: emoji, popup: popup, distKm: _distKm, trail: [], city: null };
            _mapCount++;
            document.getElementById('map-station-count').textContent = _mapCount;
            var badge = document.getElementById('map-badge');
            badge.textContent = _mapCount;
            badge.classList.remove('hidden');
            var _mmb = document.getElementById('mnav-map-badge');
            if (_mmb) { _mmb.textContent = _mapCount; _mmb.style.display = 'block'; }
            // Première apparition : fetcher la ville
            _fetchCityForStation(cs, lat, lon);
        } else {
            var _prevLat = _mapMarkers[cs].lat, _prevLon = _mapMarkers[cs].lon;
            _mapMarkers[cs].lat    = lat;
            _mapMarkers[cs].lon    = lon;
            _mapMarkers[cs].emoji  = emoji;
            _mapMarkers[cs].popup  = popup;
            _mapMarkers[cs].distKm = _distKm;
            // Re-fetcher la ville si la position a significativement changé (~10 km)
            if (!_mapMarkers[cs].city || Math.abs(_prevLat - lat) > 0.1 || Math.abs(_prevLon - lon) > 0.1) {
                _fetchCityForStation(cs, lat, lon);
            }
        }

        // ── Accumulation de traînée pour stations mobiles ─────────────────
        var _rawSym = (e.symbol || '').trim().toLowerCase();
        var _mobile = _isMobileSym(_rawSym)
                   || (e.speed_kmh !== undefined && e.speed_kmh > 0)
                   || (frame.aprs_type === 'Mic-E');

        // Stocker le cap pour la rotation de l'icône
        if (e.course !== undefined && e.course >= 0) {
            _mapMarkers[cs].lastCourse = e.course;
        }

        if (_mobile) {
            var _tr = _mapMarkers[cs].trail;
            var _now_s = Date.now() / 1000;
            var _addPt = true;
            if (_tr.length) {
                var _lp = _tr[_tr.length-1];
                // Filtre doublon exact (< ~1 m)
                if (Math.abs(_lp[0]-lat) < 1e-5 && Math.abs(_lp[1]-lon) < 1e-5) {
                    _addPt = false;
                } else {
                    // Filtre tracés parasites : distance entre deux points consécutifs
                    var _dLat = (lat - _lp[0]) * Math.PI / 180;
                    var _dLon = (lon - _lp[1]) * Math.PI / 180;
                    var _a = Math.sin(_dLat/2)*Math.sin(_dLat/2)
                           + Math.cos(_lp[0]*Math.PI/180)*Math.cos(lat*Math.PI/180)
                           * Math.sin(_dLon/2)*Math.sin(_dLon/2);
                    var _distKmSeg = 6371 * 2 * Math.atan2(Math.sqrt(_a), Math.sqrt(1-_a));
                    // Rejeter si > 300 km entre deux positions (saut géographique impossible)
                    if (_distKmSeg > 300) {
                        _addPt = false;
                        // Rompre la traînée pour ne pas relier l'ancienne position
                        _mapMarkers[cs].trail = [];
                        _tr = _mapMarkers[cs].trail;
                    } else if (_lp[2]) {
                        // Filtre vitesse implicite : > 500 km/h → parasite
                        var _dtSeg = _now_s - _lp[2];
                        if (_dtSeg > 0 && (_distKmSeg / (_dtSeg / 3600)) > 500) {
                            _addPt = false;
                        }
                    }
                }
            }
            if (_addPt) {
                var _spd = (e.speed_kmh !== undefined) ? e.speed_kmh : -1;
                var _crs = (e.course    !== undefined) ? e.course    : -1;
                _tr.push([lat, lon, _now_s, _spd, _crs]);
                if (_tr.length > 100) _tr.shift();
            }
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
                distHtml = '<span style="font-size:9px;font-family:monospace;color:' + dCol + ';white-space:nowrap">📏 ' + dStr + ' km</span>';
            }
            var cityId   = 'stlist-city-' + cs.replace(/[^a-z0-9]/gi,'_');
            var cityHtml = m.city
                ? '<span style="font-size:9px;color:#7dd3fc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100px" title="' + esc(m.city) + '">🏙️ ' + esc(m.city) + '</span>'
                : '<span id="' + cityId + '" style="font-size:9px;color:#475569;font-style:italic">🏙️…</span>';
            return '<div class="px-4 py-2.5 flex flex-col gap-0.5 cursor-pointer hover:bg-slate-800/40 transition-colors"'
                 + ' data-cs="' + cs + '" onclick="(function(el){var c=el.dataset.cs;if(_map&&_mapMarkers[c]&&_mapMarkers[c].marker){_map.setView(_mapMarkers[c].marker.getLatLng(),13);_mapMarkers[c].marker.openPopup();switchTab(&quot;map&quot;)}})(this)">'
                 + '<div style="display:flex;align-items:center;gap:6px">'
                 + '<span style="font-size:18px;line-height:1;flex-shrink:0">' + (m.emoji||'📍') + '</span>'
                 + qrzLink(cs, {cls:'font-mono font-bold text-white text-xs hover:text-blue-300 transition-colors'})
                 + '<span style="margin-left:auto;display:flex;gap:4px;align-items:center">' + distHtml + '</span>'
                 + '</div>'
                 + '<div style="padding-left:26px">' + cityHtml + '</div>'
                 + '</div>';
        }).join('');
    }

    // Fetch city for a station and store it in _mapMarkers, then refresh list
    function _fetchCityForStation(cs, lat, lon) {
        if (!cs || !lat || !lon) return;
        fetch('/nearest_city?lat=' + lat + '&lon=' + lon)
            .then(function(r){ return r.ok ? r.json() : null; })
            .then(function(d) {
                if (!d || !d.display) return;
                if (_mapMarkers[cs]) {
                    _mapMarkers[cs].city = d.display;
                    // Mise à jour directe du span si la liste est déjà rendue
                    var el = document.getElementById('stlist-city-' + cs.replace(/[^a-z0-9]/gi,'_'));
                    if (el) {
                        el.removeAttribute('id');
                        el.style.color = '#7dd3fc';
                        el.style.fontStyle = 'normal';
                        el.title = d.display;
                        el.innerHTML = '🏙️ ' + esc(d.display);
                    }
                }
            })
            .catch(function(){});
    }

    function mapClearAll() {
        if (_map) Object.values(_mapMarkers).forEach(function(m) {
            if (m.marker)   _map.removeLayer(m.marker);
            if (m.polyline) _map.removeLayer(m.polyline);
        });
        _mapMarkers = {}; _mapCount = 0;
        document.getElementById('map-station-count').textContent = '0';
        document.getElementById('map-badge').classList.add('hidden');
        var _mmbc = document.getElementById('mnav-map-badge'); if (_mmbc) _mmbc.style.display = 'none';
        _updateStationList();
    }

    function mapClearTrails() {
        // Efface les traînées côté frontend (polylines + tableau trail)
        if (_map) {
            Object.values(_mapMarkers).forEach(function(m) {
                if (m.trailLayers) {
                    m.trailLayers.forEach(function(l){ _map.removeLayer(l); });
                    m.trailLayers = [];
                }
                if (m.polyline) { _map.removeLayer(m.polyline); m.polyline = null; }
                m.trail = [];
            });
        }
        // Efface les traînées côté serveur (stations_positions)
        fetch('/map_clear_trails', { method: 'DELETE' })
            .then(function(r){ return r.json(); })
            .then(function(d){ console.log('[MAP] Traînées effacées :', d.cleared, 'station(s)'); })
            .catch(function(e){ console.error('[MAP] Erreur effacement traînées :', e); });
    }

    function mapFitAll() {
        if (!_map) return;
        var pts = Object.values(_mapMarkers).filter(function(m){return m.lat;}).map(function(m){return [m.lat,m.lon];});
        if (pts.length) _map.fitBounds(pts, {padding:[40,40]});
    }

    // ── Recherche périmétrique ─────────────────────────────────────────────
    var _nearbyCircle = null;

    function openNearbyModal() {
        var m = document.getElementById('nearby-modal');
        m.style.display = 'flex';
        setTimeout(runNearbySearch, 80);
    }
    function closeNearbyModal() {
        document.getElementById('nearby-modal').style.display = 'none';
        if (_nearbyCircle && _map) { _map.removeLayer(_nearbyCircle); _nearbyCircle = null; }
    }
    function runNearbySearch() {
        var radius  = parseInt(document.getElementById('nearby-radius').value, 10);
        var age     = parseInt(document.getElementById('nearby-age').value, 10);
        var mobOnly = document.getElementById('nearby-mobile-only').checked ? 1 : 0;
        var resDiv  = document.getElementById('nearby-results');
        resDiv.innerHTML = '<span style="color:#94a3b8;font-size:12px;">Recherche en cours…</span>';

        // Construire URL : si la station locale a des coordonnées, pas besoin de lat/lon
        var url = '/nearby_stations?radius_km=' + radius + '&max_age_s=' + age + '&mobile_only=' + mobOnly;
        if (typeof _stationLat !== 'undefined' && _stationLat !== null) {
            url += '&lat=' + _stationLat + '&lon=' + _stationLon;
        }

        fetch(url)
            .then(function(r){ return r.json(); })
            .then(function(data) {
                if (data.error) {
                    resDiv.innerHTML = '<span style="color:#f87171;font-size:12px;">⚠ ' + data.error + '</span>';
                    return;
                }
                // Cercle sur la carte
                if (_nearbyCircle && _map) _map.removeLayer(_nearbyCircle);
                if (_map && data.center) {
                    _nearbyCircle = L.circle([data.center.lat, data.center.lon], {
                        radius: data.radius_km * 1000,
                        color: '#38bdf8', weight: 1.5,
                        fillColor: '#38bdf8', fillOpacity: 0.04,
                        dashArray: '6,4'
                    }).addTo(_map);
                }
                // Tableau résultats
                if (!data.stations || data.stations.length === 0) {
                    resDiv.innerHTML = '<span style="color:#64748b;font-size:12px;">Aucune station dans ce périmètre.</span>';
                    return;
                }
                var html = '<table style="width:100%;border-collapse:collapse;font-size:11px;">'
                    + '<thead><tr style="color:#64748b;border-bottom:1px solid #1e293b;">'
                    + '<th style="text-align:left;padding:4px 6px;">Indicatif</th>'
                    + '<th style="text-align:right;padding:4px 6px;">Dist.</th>'
                    + '<th style="text-align:right;padding:4px 6px;">Âge</th>'
                    + '<th style="text-align:center;padding:4px 6px;">Type</th>'
                    + '</tr></thead><tbody>';
                data.stations.forEach(function(s) {
                    var ageStr = s.age_s < 60 ? s.age_s + ' s'
                               : s.age_s < 3600 ? Math.round(s.age_s/60) + ' min'
                               : Math.round(s.age_s/3600) + ' h';
                    var typBadge = s.mobile
                        ? '<span style="color:#fb923c;">🚗 mobile</span>'
                        : '<span style="color:#94a3b8;">📍 fixe</span>';
                    var rowColor = s.distance_km < 10 ? '#dcfce7' : s.distance_km < 50 ? '#e2e8f0' : '#94a3b8';
                    html += '<tr style="border-bottom:1px solid #1e293b;cursor:pointer;" '
                          + 'onclick="closeNearbyModal();if(_map)_map.setView([' + s.lat + ',' + s.lon + '],12);"'
                          + 'title="Cliquer pour centrer la carte">'
                          + '<td style="padding:4px 6px;color:' + rowColor + ';font-weight:700;font-family:monospace;">' + s.callsign + '</td>'
                          + '<td style="text-align:right;padding:4px 6px;color:#38bdf8;">' + s.distance_km.toFixed(1) + ' km</td>'
                          + '<td style="text-align:right;padding:4px 6px;color:#64748b;">' + ageStr + '</td>'
                          + '<td style="text-align:center;padding:4px 6px;">' + typBadge + '</td>'
                          + '</tr>';
                });
                html += '</tbody></table>';
                html += '<div style="margin-top:8px;font-size:10px;color:#475569;">'
                      + data.count + ' station(s) dans un rayon de ' + data.radius_km + ' km</div>';
                resDiv.innerHTML = html;
            })
            .catch(function(e) {
                resDiv.innerHTML = '<span style="color:#f87171;font-size:12px;">Erreur réseau : ' + e + '</span>';
            });
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

    // ══════════════════════════════════════════════════════════════════════
    // 📡 ALERTES PROPAGATION VHF
    // ══════════════════════════════════════════════════════════════════════
    (function() {
        var _alertTimer = null;
        var _alertQueue = [];
        var _alertShowing = false;

        // ── Bip audio doux ─────────────────────────────────────────────────
        function _alertBeep(level) {
            try {
                var ctx = new (window.AudioContext || window.webkitAudioContext)();
                var freqs = level === 'special' ? [660, 880, 1100] : [440, 550];
                freqs.forEach(function(freq, i) {
                    var osc = ctx.createOscillator();
                    var g   = ctx.createGain();
                    osc.frequency.value = freq;
                    osc.type = 'sine';
                    g.gain.setValueAtTime(0, ctx.currentTime + i * 0.18);
                    g.gain.linearRampToValueAtTime(0.18, ctx.currentTime + i * 0.18 + 0.05);
                    g.gain.linearRampToValueAtTime(0, ctx.currentTime + i * 0.18 + 0.22);
                    osc.connect(g); g.connect(ctx.destination);
                    osc.start(ctx.currentTime + i * 0.18);
                    osc.stop(ctx.currentTime + i * 0.18 + 0.3);
                });
                setTimeout(function(){ try { ctx.close(); } catch(_){} }, 1200);
            } catch(_) {}
        }

        // ── Bannière flash ─────────────────────────────────────────────────
        function _showBanner(alert) {
            _alertShowing = true;
            var colors = alert.level === 'special'
                ? { bg: '#1e1b4b', border: '#818cf8', text: '#a5b4fc', icon: '🌠' }
                : { bg: '#052e16', border: '#22c55e', text: '#86efac', icon: '📡' };

            var banner = document.createElement('div');
            banner.id = 'vhf-alert-banner';
            banner.style.cssText = [
                'position:fixed', 'top:18px', 'left:50%', 'transform:translateX(-50%)',
                'z-index:9999', 'min-width:320px', 'max-width:90vw',
                'background:' + colors.bg,
                'border:1.5px solid ' + colors.border,
                'border-radius:16px', 'padding:14px 20px',
                'display:flex', 'align-items:center', 'gap:14px',
                'box-shadow:0 8px 32px rgba(0,0,0,.6)',
                'animation:vhfAlertIn .35s cubic-bezier(.22,1,.36,1)',
                'cursor:pointer',
            ].join(';');

            // Injecter le keyframe si absent
            if (!document.getElementById('vhf-alert-style')) {
                var st = document.createElement('style');
                st.id  = 'vhf-alert-style';
                st.textContent = [
                    '@keyframes vhfAlertIn{from{opacity:0;transform:translateX(-50%) translateY(-20px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}',
                    '@keyframes vhfAlertOut{from{opacity:1}to{opacity:0;transform:translateX(-50%) translateY(-10px)}}',
                    '@keyframes vhfPulse{0%,100%{box-shadow:0 8px 32px rgba(0,0,0,.6)}50%{box-shadow:0 8px 40px ' + colors.border + '66}}',
                ].join('');
                document.head.appendChild(st);
            }
            if (alert.level === 'special') banner.style.animation += ',vhfPulse 1.2s ease infinite';

            banner.innerHTML =
                '<span style="font-size:26px">' + colors.icon + '</span>'
                + '<div style="flex:1">'
                +   '<div style="font-size:12px;font-weight:800;color:' + colors.text + ';letter-spacing:.04em">' + alert.label + '</div>'
                +   '<div style="font-size:10px;color:#94a3b8;margin-top:2px">' + alert.detail + '</div>'
                + '</div>'
                + '<button onclick="this.parentNode.remove()" style="background:none;border:none;color:#64748b;font-size:18px;cursor:pointer;padding:0 4px;line-height:1">×</button>';

            // Clic → aller sur MAP/Propagation
            banner.addEventListener('click', function(e) {
                if (e.target.tagName !== 'BUTTON') {
                    if (typeof switchTab === 'function') switchTab('map');
                }
            });

            document.body.appendChild(banner);
            _alertBeep(alert.level);

            // Auto-dismiss après 12 s
            setTimeout(function() {
                if (!banner.parentNode) { _alertShowing = false; _nextAlert(); return; }
                banner.style.animation = 'vhfAlertOut .4s ease forwards';
                setTimeout(function() {
                    if (banner.parentNode) banner.parentNode.removeChild(banner);
                    _alertShowing = false;
                    _nextAlert();
                }, 400);
            }, 12000);
        }

        function _nextAlert() {
            if (_alertQueue.length && !_alertShowing) {
                var a = _alertQueue.shift();
                setTimeout(function() { _showBanner(a); }, 600);
            }
        }

        // ── Mise à jour du badge dans le widget propagation ────────────────
        function _updatePropBadge(data) {
            var badge = document.getElementById('vhf-alert-badge');
            if (!badge) {
                // Créer le badge et l'insérer dans le widget propagation
                var box = document.getElementById('vhf-prop-content');
                if (!box) return;
                badge = document.createElement('div');
                badge.id = 'vhf-alert-badge';
                badge.style.cssText = 'margin-top:8px;border-radius:10px;padding:6px 10px;font-size:10px;font-family:monospace;text-align:center;display:none';
                box.parentNode.insertBefore(badge, box.nextSibling);
            }
            if (data.score >= 70) {
                var color = data.es ? '#818cf8' : '#22c55e';
                var txt   = data.es
                    ? '🌠 Es actif — ouvertures 6m/2m possibles'
                    : '📡 Conditions VHF favorables — Kp ' + (data.kp !== null ? data.kp.toFixed(1) : '?');
                badge.style.cssText = badge.style.cssText.replace('display:none','').replace(/display:[^;]+/,'display:block');
                badge.style.background = '#0f1629';
                badge.style.border = '1px solid ' + color;
                badge.style.color  = color;
                badge.textContent  = txt;
            } else if (data.aurora) {
                badge.style.background = '#1e1b4b';
                badge.style.border = '1px solid #818cf8';
                badge.style.color  = '#a5b4fc';
                badge.style.display = 'block';
                badge.textContent  = '🌠 Aurora possible — Kp ' + (data.kp !== null ? data.kp.toFixed(1) : '?');
            } else {
                badge.style.display = 'none';
            }
        }

        // ── Polling /vhf_alert_check toutes les 5 min ──────────────────────
        function _pollAlerts() {
            fetch('/vhf_alert_check', {cache: 'no-store'})
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    _updatePropBadge(d);
                    if (d.alerts && d.alerts.length) {
                        d.alerts.forEach(function(a) {
                            _alertQueue.push(a);
                        });
                        _nextAlert();
                    }
                })
                .catch(function() {});
        }

        // Premier poll après 30 s (laisser la page s'initialiser)
        setTimeout(_pollAlerts, 30000);
        // Puis toutes les 5 min
        _alertTimer = setInterval(_pollAlerts, 5 * 60 * 1000);

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

    function _updateDigiStatus() {
        fetch('/digi/status').then(function(r){ return r.json(); }).then(function(d) {
            var dot  = document.getElementById('digi-dot');
            var txt  = document.getElementById('digi-count-txt');
            var big  = document.getElementById('digi-count-big');
            if (!dot) return;
            if (d.enabled) {
                dot.style.background = '#10b981';
                dot.style.boxShadow  = '0 0 6px #10b98188';
            } else {
                dot.style.background = '#334155';
                dot.style.boxShadow  = 'none';
            }
            var n = d.digipeated || 0;
            if (txt) txt.textContent = n + ' retransmission' + (n !== 1 ? 's' : '');
            if (big) big.textContent = n;
        }).catch(function(){});
    }
    setInterval(_updateDigiStatus, 5000);
    _updateDigiStatus();

    // ── Répéteur toggle warning ────────────────────────────────────────────
    var _repToggle  = document.getElementById('repeater_toggle');

    // ══════════════════════════════════════════════════════════════════════
    //  MÉTÉO  —  Open-Meteo (gratuit, sans clé API)  WEATHER_PATCH_APPLIED
    // ══════════════════════════════════════════════════════════════════════
    (function() {
        // ── Correspondance code WMO → emoji + description FR ──────────────
        var WMO = {
            0:  ['☀️',  'Ciel dégagé'],
            1:  ['🌤️',  'Peu nuageux'],
            2:  ['⛅',   'Partiellement nuageux'],
            3:  ['☁️',  'Couvert'],
            45: ['🌫️',  'Brouillard'],
            48: ['🌫️',  'Brouillard givrant'],
            51: ['🌦️',  'Bruine légère'],
            53: ['🌦️',  'Bruine modérée'],
            55: ['🌧️',  'Bruine forte'],
            61: ['🌧️',  'Pluie légère'],
            63: ['🌧️',  'Pluie modérée'],
            65: ['🌧️',  'Pluie forte'],
            71: ['🌨️',  'Neige légère'],
            73: ['🌨️',  'Neige modérée'],
            75: ['❄️',   'Neige forte'],
            77: ['🌨️',  'Grains de neige'],
            80: ['🌦️',  'Averses légères'],
            81: ['🌧️',  'Averses modérées'],
            82: ['⛈️',   'Averses violentes'],
            85: ['🌨️',  'Averses de neige'],
            86: ['❄️',   'Averses de neige fortes'],
            95: ['⛈️',   'Orage'],
            96: ['⛈️',   'Orage avec grêle'],
            99: ['⛈️',   'Orage avec forte grêle'],
        };

        var _wxLat = null, _wxLon = null, _wxTimer = null;
        var _WX_CACHE_KEY = 'aprs_wx_cache';
        var _WX_CACHE_TTL = 15 * 60 * 1000; // 15 minutes

        function _wxIcon(code)  { return (WMO[code] || ['🌡️', 'Inconnu'])[0]; }
        function _wxDesc(code)  { return (WMO[code] || ['🌡️', 'Inconnu'])[1]; }

        function _wxDirArrow(deg) {
            var dirs = ['↑N','↗NE','→E','↘SE','↓S','↙SO','←O','↖NO'];
            return dirs[Math.round(deg / 45) % 8] || '';
        }

        function _wxRender(d) {
            var c = d.current;
            var code = c.weather_code;
            document.getElementById('wx-icon').textContent  = _wxIcon(code);
            document.getElementById('wx-temp').textContent  = Math.round(c.temperature_2m) + '°C';
            document.getElementById('wx-feels').textContent = '(res. ' + Math.round(c.apparent_temperature) + '°C)';
            document.getElementById('wx-desc').textContent  = _wxDesc(code);
            var wSpd = Math.round(c.wind_speed_10m);
            var wDir = _wxDirArrow(c.wind_direction_10m);
            document.getElementById('wx-wind').textContent  = '💨 ' + wSpd + ' km/h ' + wDir;
            document.getElementById('wx-hum').textContent   = '💧 ' + Math.round(c.relative_humidity_2m) + '%';
            var precip = c.precipitation !== undefined ? c.precipitation : 0;
            var rainEl = document.getElementById('wx-rain');
            if (precip > 0) {
                rainEl.textContent = '🌧 ' + precip.toFixed(1) + ' mm';
                rainEl.style.display = '';
                document.getElementById('wx-sep4').style.display = '';
            } else {
                rainEl.style.display = 'none';
                document.getElementById('wx-sep4').style.display = 'none';
            }
            // Visibilité des séparateurs
            ['wx-sep1','wx-sep2','wx-sep3'].forEach(function(id){ document.getElementById(id).style.display = ''; });
        }

        function _wxFetch(lat, lon) {
            // Proxy local pour eviter le CORS (fetch direct bloque par le navigateur)
            var url = '/wx_proxy'
                    + '?lat=' + lat.toFixed(4)
                    + '&lon=' + lon.toFixed(4);

            fetch(url)
                .then(function(r){ return r.ok ? r.json() : Promise.reject(r.status); })
                .then(function(data) {
                    _wxRender(data);
                    // Stocker en cache localStorage avec timestamp
                    try {
                        localStorage.setItem(_WX_CACHE_KEY, JSON.stringify({
                            t: Date.now(), lat: lat, lon: lon, data: data
                        }));
                    } catch(e) {}
                })
                .catch(function(err) {
                    console.warn('[WX] Open-Meteo erreur :', err);
                    document.getElementById('wx-icon').textContent = '⚠️';
                    document.getElementById('wx-temp').textContent = 'Météo indisponible';
                });
        }

        function _wxStart(lat, lon, locName) {
            _wxLat = lat; _wxLon = lon;
            document.getElementById('wx-loc-name').textContent = locName || (lat.toFixed(2) + ',' + lon.toFixed(2));
            _wxFetch(lat, lon);
            // Rafraîchissement auto toutes les 15 min
            if (_wxTimer) clearInterval(_wxTimer);
            _wxTimer = setInterval(function(){ _wxFetch(_wxLat, _wxLon); }, _WX_CACHE_TTL);
        }

        window._wxRefresh = function() {
            if (_wxLat !== null) _wxFetch(_wxLat, _wxLon);
        };

        // ── Initialisation : attendre que la config station soit chargée ──
        function _wxInit() {
            // 1) Essayer le cache localStorage en premier
            try {
                var cached = JSON.parse(localStorage.getItem(_WX_CACHE_KEY) || 'null');
                if (cached && (Date.now() - cached.t) < _WX_CACHE_TTL) {
                    _wxRender(cached.data);
                    _wxLat = cached.lat; _wxLon = cached.lon;
                    document.getElementById('wx-loc-name').textContent =
                        cached.lat.toFixed(2) + ',' + cached.lon.toFixed(2);
                }
            } catch(e) {}

            // 2) Attendre que _stationLat/_stationLon soient définis (config station)
            var _tries = 0;
            var _poll = setInterval(function() {
                _tries++;
                if (typeof _stationLat !== 'undefined' && _stationLat !== null
                    && typeof _stationLon !== 'undefined' && _stationLon !== null) {
                    clearInterval(_poll);
                    _wxStart(_stationLat, _stationLon, 'Station');
                } else if (_tries > 30) {
                    clearInterval(_poll);
                    // Fallback : géolocalisation navigateur
                    if (navigator.geolocation) {
                        navigator.geolocation.getCurrentPosition(function(pos) {
                            _wxStart(pos.coords.latitude, pos.coords.longitude, 'Vous');
                        }, function() {
                            document.getElementById('wx-icon').textContent = '📡';
                            document.getElementById('wx-temp').textContent = '';
                            document.getElementById('wx-desc').textContent = 'Position inconnue';
                        });
                    }
                }
            }, 200);
        }

        document.addEventListener('DOMContentLoaded', function() {
            // Lancer après un court délai pour laisser la config se charger
            setTimeout(_wxInit, 600);
        });

        // ── Mode jour : adapter les couleurs du widget ────────────────────
        document.addEventListener('DOMContentLoaded', function() {
            var bar = document.getElementById('weather-bar');
            if (!bar) return;
            function _wxTheme() {
                if (document.body.classList.contains('day-mode')) {
                    bar.style.background    = 'rgba(236,239,245,0.72)';
                    bar.style.borderColor   = 'rgba(196,202,218,0.5)';
                    bar.style.color         = '#3a4460';
                    ['wx-sep1','wx-sep2','wx-sep3','wx-sep4'].forEach(function(id){
                        var el = document.getElementById(id);
                        if (el) el.style.color = '#c8cedc';
                    });
                } else {
                    bar.style.background    = 'rgba(15,23,42,0.45)';
                    bar.style.borderColor   = 'rgba(148,163,184,0.08)';
                    bar.style.color         = '#94a3b8';
                    ['wx-sep1','wx-sep2','wx-sep3','wx-sep4'].forEach(function(id){
                        var el = document.getElementById(id);
                        if (el) el.style.color = '#334155';
                    });
                }
            }
            _wxTheme();
            // Observer les changements de thème
            new MutationObserver(_wxTheme).observe(document.body, {attributeFilter: ['class']});
        });
    })();

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
                    <li>Sélectionner le port série PTT, le mode PTT et les périphériques audio RX/TX.</li>
                    <li>Configurer les balises automatiques (intervalle par type) puis cliquer <span class="text-white font-bold">✅ APPLIQUER</span>.</li>
                    <li>Les trames APRS décodées par Direwolf apparaissent en temps réel dans <span class="text-white font-bold">📻 TRAFIC</span>.</li>
                </ol>
            </section>

            <hr class="border-slate-700/60"/>

            <section>
                <h3 class="text-[10px] font-black text-blue-400 uppercase tracking-widest mb-3 flex items-center gap-2">📻 Onglet TRAFIC</h3>
                <div class="space-y-2 text-slate-400">
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">📨 Envoyer message</span><span>Saisir indicatif destinataire + texte → <span class="text-white">ENVOYER</span>. Confirmation ACK attendue automatiquement. Les messages sont sauvegardés dans <span class="text-white font-mono">chat.json</span> et restaurés au redémarrage.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🛸 Beacon ISS</span><span>Émet une balise positionnée via ARISS (QRG 145.825 MHz). Le bouton est <span class="text-amber-400 font-bold">désactivé automatiquement</span> hors passage ISS et s'active (fond indigo animé) uniquement quand l'ISS est en vue au-dessus de l'horizon.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">📡 Beacon Station</span><span>Émet la balise de position standard avec commentaire et locator.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🌦️ Beacon Météo</span><span>Récupère les données Open-Meteo (lat/lon depuis locator) et émet un beacon APRS météo format <span class="text-emerald-400">@…_</span>.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">📶 Beacon Propagation</span><span>Interroge NOAA SWPC (SFI, Kp, A-index) et émet un beacon statut <span class="text-emerald-400">&gt;SFI:NNN K:N.N HF:xxx VHF:yyy {NOAA}</span>.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🛰️ Envoyer statut</span><span>Émet le statut texte libre configuré dans les réglages (trame <span class="text-emerald-400">&gt;</span>).</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Console</span><span>Affiche les trames TX/RX. Clic sur un indicatif → fiche QRZ.com. Clic sur les coordonnées → onglet MAP. Le flux SSE se reconnecte automatiquement avec backoff exponentiel (3 s → 30 s) en cas de coupure réseau ou redémarrage du serveur.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🛸 Passages ISS (jour)</span><span>Widget affichant <b>tous les passages de l'ISS du jour courant</b> (minuit → minuit heure locale) avec durée et <span class="text-white font-mono">élév. max</span>. Calculés localement par SGP4 — fonctionne hors-ligne. Passages passés grisés, prochain surligné. Mis à jour toutes les 15 min.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🛸 Passages ISS 24 h</span><span>Bloc dans l'onglet <span class="text-white font-bold">🛸 ISS</span> affichant tous les passages sur <b>24 heures glissantes</b> (depuis maintenant). Badge <span class="text-white font-mono">J+0 / J+1</span>, barre d'élévation, étiquette "demain" si le passage est le lendemain. Mis à jour toutes les 15 min ou sur demande.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Beacons auto</span><span>Chaque type de balise possède son propre intervalle configurable dans <span class="text-white font-bold">⚙️ RÉGLAGES</span>. Le temps restant avant la prochaine émission est affiché en temps réel.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">📋 Log système</span><span>Journal rotatif <span class="text-white font-mono">aprs.log</span> : rotation quotidienne à minuit avec compression gzip automatique. Archives nommées <span class="text-white font-mono">aprs.log.YYYY-MM-DD.gz</span>, rétention 2 jours. Consultable via <span class="text-white font-mono">GET /log?n=200&amp;level=ERROR</span>. Liste des archives via <span class="text-white font-mono">GET /log/archives</span>.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🔒 Arrêt propre</span><span>À l'arrêt (SIGTERM / Ctrl-C / <span class="text-white font-mono">systemctl stop</span>), le serveur sauvegarde automatiquement <span class="text-white font-mono">chat.json</span> et <span class="text-white font-mono">config.json</span>, ferme les sockets KISS TX et iGate, et attend la fin du thread RX avant de quitter.</span></div>
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
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">📶 Alertes Propagation</span><span>Surveille en continu SFI (NOAA SWPC) et les mesures X-ray GOES. Bannière + bip triple sur tempête géomagnétique (Kp ≥ seuil), dégradation HF (SFI bas) ou éruption solaire (classe C1–X5). Cooldown 1 h par type. Bouton <span class="text-white font-bold">🔬 Tester</span> pour forcer une vérification immédiate.</span></div>
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
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">💬 QSO</span><span>Messagerie APRS par indicatif. Historique persistant dans <span class="text-white font-mono">chat.json</span>. Badge rouge = messages non lus. ACK automatique. Bouton 🗑️ dans le header chat pour supprimer une conversation.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🗺️ MAP</span><span>Carte temps réel des stations APRS en français (OSM France). Clic marqueur → infos PHG, vitesse, altitude. Widget <span class="text-violet-400">📶 Propagation VHF</span> NOAA en direct.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">🛰️ ISS</span><span>Suivi temps réel via OrbTrack. APRS : <span class="text-emerald-400">145.825 MHz</span> · Voice FM : <span class="text-emerald-400">437.800 MHz</span>.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-24 shrink-0">📊 STATS</span><span>Histogramme 24 h (TX / RX / IS) et top 10 des stations les plus actives. Persisté dans <span class="text-white font-mono">stats.json</span>.</span></div>
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
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Moteur SGP4</span><span>Calcul orbital embarqué (stdlib pure, sans dépendance externe). Précision ±30 s / ±5 km — suffisant pour planifier l'écoute APRS 145.825 MHz. Fonctionne entièrement hors-ligne.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">TLE ISS</span><span>Rechargé toutes les 6 h depuis CelesTrak (3 sources en cascade). En cas de coupure réseau, le cache disque <span class="text-white font-mono">.iss_tle_cache</span> prend le relais jusqu'à <span class="text-white font-bold">7 jours</span> sans Internet.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Élévation maximale</span><span>Chaque passage affiche l'élévation max (en degrés) calculée par SGP4. Au-dessus de <span class="text-white">10°</span> la réception APRS est généralement possible. Au-dessus de <span class="text-emerald-400">40°</span> la liaison est excellente.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Panel EN VUE</span><span>Affiché <b>en haut de la sidebar Trafic</b> dès que l'ISS passe au-dessus de l'horizon local. Montre élévation courante, azimut AOS/LOS, compas SVG et compte à rebours LOS. Se masque automatiquement hors passage.</span></div>
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

            <hr class="border-slate-700/60"/>

            <section>
                <h3 class="text-[10px] font-black text-yellow-400 uppercase tracking-widest mb-3 flex items-center gap-2">🔑 Sécurité</h3>
                <div class="space-y-2 text-slate-400">
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Mot de passe</span><span>Modifiable depuis <span class="text-white font-bold">⚙️ RÉGLAGES</span>, section <span class="text-white font-bold">🔑 Changer le mot de passe</span> en bas de page. Saisir l'ancien mot de passe, le nouveau (min 6 caractères) et confirmer.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Stockage</span><span>Les mots de passe sont stockés sous forme de hash <span class="text-white font-mono">werkzeug</span> (PBKDF2-SHA256) dans <span class="text-white font-mono">users.json</span>. Jamais en clair.</span></div>
                    <div class="flex gap-3"><span class="text-slate-500 w-36 shrink-0">Session</span><span>Session Flask signée côté serveur. Déconnexion via <span class="text-white font-mono">/logout</span>.</span></div>
                </div>
            </section>

            <div class="text-center pt-2 pb-1 text-slate-700 text-[9px]">Py-APRS v2.6.8 · Direwolf + SGP4 · 73 de F1RIQ</div>
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
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Passages ISS absents</span><span>Locator ou coordonnées absents dans les réglages, ou TLE ISS non rechargeable (pas d'Internet). Le calcul SGP4 fonctionne hors-ligne via le cache disque <code>.iss_tle_cache</code>.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Alerte météo muette</span><span>Vérifier que le toggle est actif et que la position (locator ou lat/lon) est renseignée. Cliquer <span class="text-white font-bold">🔍 Vérifier maintenant</span> pour diagnostiquer.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Voyant ISS absent</span><span>Normal si l'alerte ISS n'est pas activée dans les Réglages. Le voyant n'apparaît que lorsque le toggle est actif.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">iGate : pastille orange</span><span>Serveur APRS-IS inaccessible. Vérifier connexion Internet, <span class="text-white font-mono">rotate.aprs2.net</span> et port <span class="text-white font-mono">14580</span>. Reconnexion automatique toutes les 30 s.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">iGate : compteur bloqué</span><span>Vérifier que l'iGate est activé, le passcode correct (bouton <span class="text-white font-bold">🔑 Calculer</span>) et que Direwolf reçoit bien des trames.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Beacon TX échoue</span><span>Socket KISS Direwolf non disponible. Vérifier que Direwolf écoute sur le port KISS TCP (défaut 8001). Redémarrer le serveur Python si nécessaire.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Lien QRZ absent</span><span>L'indicatif <span class="text-white font-mono">BEACON</span>, <span class="text-white font-mono">APRS</span> ou <span class="text-white font-mono">?</span> n'ont pas de lien QRZ (adresses non callsign). Seuls les vrais indicatifs sont cliquables.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">config.json perdu à l'arrêt</span><span>Utiliser <span class="text-white font-mono">systemctl stop</span> ou <span class="text-white">Ctrl-C</span> plutôt que <span class="text-white font-mono">kill -9</span>. Le graceful shutdown sauvegarde automatiquement la config. Un <span class="text-white font-mono">kill -9</span> contourne cette protection.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">TLE ISS périmés</span><span>Si le cache <span class="text-white font-mono">.iss_tle_cache</span> a plus de 7 jours et qu'Internet est indisponible, les prédictions sont désactivées. Supprimer le fichier cache puis rétablir la connexion pour forcer un rechargement.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Élévation ISS = 0°</span><span>Peut indiquer des TLE trop anciens ou une position de station invalide. Vérifier le locator dans les réglages et forcer la mise à jour TLE via le bouton <span class="text-white font-bold">↺ MAJ</span>.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Moniteur bloqué / figé</span><span>Le flux SSE se reconnecte seul après coupure. Si le moniteur reste figé, recharger la page (F5). Si le problème persiste, vérifier que le serveur Python tourne (<span class="text-white font-mono">systemctl status aprs</span>) et que le port n'est pas saturé.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Log aprs.log trop volumineux</span><span>La rotation est automatique (quotidienne, gzip, 30 jours). Vérifier l'espace disque avec <span class="text-white font-mono">ls -lh aprs.log*</span>. Les archives <span class="text-white font-mono">.gz</span> peuvent être supprimées manuellement sans impact sur le fonctionnement.</span></div>
                    <div class="flex gap-3"><span class="text-red-400/70 w-48 shrink-0">Mot de passe oublié</span><span>Éditer manuellement <span class="text-white font-mono">users.json</span> et remplacer le hash par un nouveau généré avec <span class="text-white font-mono">python3 -c "from werkzeug.security import generate_password_hash as g; print(g('nouveaumdp'))"</span>.</span></div>
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
                        <div class="text-slate-600 text-[9px] mt-1">Py-APRS · version 2.6.7 · Backend Dire Wolf + SGP4</div>
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
    if (e.key === 'Escape') {
        document.getElementById('modal-aide').classList.add('hidden');
    }
});
</script>

<script>
async function submitPasswd() {
    var cur  = document.getElementById('passwd-current').value;
    var nw   = document.getElementById('passwd-new').value;
    var conf = document.getElementById('passwd-confirm').value;
    var msg  = document.getElementById('passwd-msg');
    function showMsg(text, ok) {
        msg.textContent = text;
        msg.className = 'text-xs rounded-xl px-4 py-2.5 font-bold '
            + (ok ? 'bg-emerald-900/50 text-emerald-400 border border-emerald-700/50'
                  : 'bg-red-900/50 text-red-400 border border-red-700/50');
        msg.classList.remove('hidden');
    }
    if (!cur || !nw || !conf) return showMsg('Tous les champs sont requis.', false);
    if (nw.length < 6)        return showMsg('Minimum 6 caractères.', false);
    if (nw !== conf)          return showMsg('Les mots de passe ne correspondent pas.', false);
    try {
        var r = await fetch('/auth/change_password', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({current_password: cur, new_password: nw})
        });
        var d = await r.json();
        if (r.ok && d.ok) {
            showMsg('✅ Mot de passe modifié avec succès !', true);
            document.getElementById('passwd-current').value = '';
            document.getElementById('passwd-new').value     = '';
            document.getElementById('passwd-confirm').value = '';
        } else {
            showMsg('❌ ' + (d.error || 'Mot de passe actuel incorrect.'), false);
        }
    } catch(e) {
        showMsg('❌ Erreur réseau.', false);
    }
}
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

</script>

<script>
/* ── SSTV RX UI — stub (récepteur intégré supprimé) ── */
(function(){
    window.sstvStart  = function() {};
    window.sstvStop   = function() {};
    window.sstvMonUpdate = function() {};
    window.sstvRefreshGallery = function() {};
    window.sstvMonClear = function() {};
    window._sstvSelectMonitor = function() {};
    window._sstvRefreshDevices = function() {};
})();
/* ── fin SSTV RX UI ── */
</script>

<style>
@keyframes issLivePulse {
    0%,100% { border-color:rgba(124,58,237,.35); box-shadow:0 10px 40px rgba(124,58,237,.18); }
    50%      { border-color:rgba(167,139,250,.70); box-shadow:0 10px 40px rgba(167,139,250,.35); }
}
</style>

<script>
/* ── ISS LIVE TRACKING (onglet Trafic) ────────────────────────────────────── */
(function(){
    var _issLiveTimer = null;
    var _issPassEnd   = 0;
    var _issCountdown = null;

    var _AZ_CARDS = [
        "N","NNE","NE","ENE","E","ESE","SE","SSE",
        "S","SSO","SO","OSO","O","ONO","NO","NNO"
    ];

    function _azCard(az) {
        return _AZ_CARDS[Math.round(az / 22.5) % 16];
    }

    function _elToRadius(el) {
        return Math.max(0, 34 * (1 - el / 90));
    }

    function _updateCompass(az, el) {
        var ptr = document.getElementById("iss-compass-ptr");
        if (ptr) ptr.setAttribute("transform", "rotate(" + az + ",45,45)");
        var r  = _elToRadius(Math.max(0, Math.min(90, el)));
        var cx = 45 + r * Math.cos((az - 90) * Math.PI / 180);
        var cy = 45 + r * Math.sin((az - 90) * Math.PI / 180);
        var dot = document.getElementById("iss-compass-dot");
        var ico = document.getElementById("iss-compass-icon");
        if (dot) { dot.setAttribute("cx", cx.toFixed(1)); dot.setAttribute("cy", cy.toFixed(1)); }
        if (ico) { ico.setAttribute("x",  cx.toFixed(1)); ico.setAttribute("y", (cy+3).toFixed(1)); }
    }

    function _setText(id, val) {
        var el = document.getElementById(id);
        if (el) el.textContent = val;
    }

    function _startCountdown(endTs) {
        _issPassEnd = endTs;
        if (_issCountdown) clearInterval(_issCountdown);
        _issCountdown = setInterval(function() {
            var rem = Math.round(_issPassEnd - Date.now() / 1000);
            if (rem <= 0) {
                clearInterval(_issCountdown);
                _setText("iss-live-timer", "00:00");
            } else {
                var m = Math.floor(rem / 60);
                var s = rem % 60;
                _setText("iss-live-timer",
                    (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s);
            }
        }, 1000);
    }

    function _poll() {
        fetch("/iss_now").then(function(r){ return r.json(); }).then(function(d) {
            var panel = document.getElementById("iss-live-panel");
            if (!panel) return;

            var issBtn = document.getElementById("btn-iss-send");
            if (d.error || !d.visible) {
                panel.style.display = "none";
                if (_issCountdown) { clearInterval(_issCountdown); _issCountdown = null; }
                if (issBtn) {
                    issBtn.disabled = false;
                    issBtn.className = "flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-[10px] font-black transition-all active:scale-95 bg-slate-800/50 border border-slate-700 text-slate-400 hover:bg-indigo-700/40 hover:border-indigo-500 hover:text-indigo-200";
                    issBtn.title = "ISS hors de portée — le beacon sera refusé";
                }
                return;
            }

            panel.style.display = "";
            if (issBtn) {
                issBtn.disabled = false;
                issBtn.className = "flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-[10px] font-black transition-all active:scale-95 bg-indigo-700/60 hover:bg-indigo-600/80 border border-indigo-500 text-white animate-pulse cursor-pointer";
                issBtn.title = "ISS EN VUE — Envoyer un beacon ARISS maintenant !";
            }

            // Élévation
            var elColor = d.elevation > 45 ? "#a78bfa" : d.elevation > 20 ? "#c4b5fd" : "#7c3aed";
            var elEl = document.getElementById("iss-live-el");
            if (elEl) { elEl.textContent = d.elevation.toFixed(1) + "°"; elEl.style.color = elColor; }

            // Azimut
            _setText("iss-live-az",      d.azimuth.toFixed(1) + "°");
            _setText("iss-live-az-card", _azCard(d.azimuth));

            // Boussole
            _updateCompass(d.azimuth, d.elevation);

            // Télémétrie
            _setText("iss-live-dist", d.slant_km.toFixed(0) + " km");
            _setText("iss-live-alt",  d.alt_km.toFixed(0)   + " km");

            // Doppler
            var dop   = d.doppler_hz;
            var dopEl = document.getElementById("iss-live-doppler");
            if (dopEl) {
                dopEl.textContent = (dop > 0 ? "+" : "") + dop + " Hz";
                dopEl.style.color = dop < -200 ? "#34d399" : dop > 200 ? "#f87171" : "#fbbf24";
            }
            _setText("iss-live-freq", d.freq_rx_khz.toFixed(3) + " kHz");

            // Compte à rebours fin de passage
            if (!_issCountdown || _issPassEnd <= Date.now() / 1000) {
                fetch("/iss_passes?range=24h")
                    .then(function(r2){ return r2.json(); })
                    .then(function(d2) {
                        var now = Date.now() / 1000;
                        var cur = (d2.passes || []).find(function(p) {
                            return p.risetime <= now && (p.risetime + p.duration) >= now;
                        });
                        if (cur) _startCountdown(cur.risetime + cur.duration);
                    }).catch(function(){});
            }
        }).catch(function(){});
    }

    function _startPoll() {
        if (_issLiveTimer) return;
        _poll();
        _issLiveTimer = setInterval(_poll, 2000);
    }
    function _stopPoll() {
        if (_issLiveTimer) { clearInterval(_issLiveTimer); _issLiveTimer = null; }
    }

    // Terminal est l'onglet par défaut
    _startPoll();

    document.addEventListener("aprs-switchtab", function(e) {
        if (e.detail === "terminal") {
            _startPoll();
            // Sur mobile : remonter la console pour afficher les dernieres trames
            if (window.innerWidth < 768) {
                var con = document.getElementById("console");
                if (con) setTimeout(function(){ con.scrollTop = 0; }, 80);
            }
        } else {
            _stopPoll();
        }
    });
})();
/* ── fin ISS Live Tracking ── */
</script>


<!-- Modale recherche perimetrique -->
<div id="nearby-modal" style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.7);align-items:center;justify-content:center;">
  <div style="background:#0f172a;border:1px solid #334155;border-radius:16px;padding:24px;width:min(520px,96vw);max-height:80vh;display:flex;flex-direction:column;gap:12px;">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <span style="font-size:15px;font-weight:700;color:#e2e8f0;">📡 Stations a proximite</span>
      <button onclick="closeNearbyModal()" style="color:#64748b;font-size:18px;line-height:1;background:none;border:none;cursor:pointer;">✕</button>
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;">
      <label style="font-size:11px;color:#94a3b8;display:flex;align-items:center;gap:6px;">
        Rayon
        <input type="range" id="nearby-radius" min="5" max="500" step="5" value="50"
               oninput="document.getElementById('nearby-radius-val').textContent=this.value"
               style="width:120px;">
        <span id="nearby-radius-val" style="color:#60a5fa;font-weight:700;min-width:32px;">50</span> km
      </label>
      <label style="font-size:11px;color:#94a3b8;display:flex;align-items:center;gap:6px;">
        Age max
        <select id="nearby-age" style="background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:2px 6px;font-size:11px;">
          <option value="900">15 min</option>
          <option value="3600" selected>1 h</option>
          <option value="10800">3 h</option>
          <option value="86400">24 h</option>
        </select>
      </label>
      <label style="font-size:11px;color:#94a3b8;display:flex;align-items:center;gap:6px;">
        <input type="checkbox" id="nearby-mobile-only"> Mobiles uniquement
      </label>
      <button onclick="runNearbySearch()"
              style="background:#1d4ed8;color:#fff;border:none;border-radius:8px;padding:5px 14px;font-size:11px;font-weight:700;cursor:pointer;">
        🔍 Chercher
      </button>
    </div>
    <div id="nearby-results" style="overflow-y:auto;flex:1;min-height:80px;">
      <span style="color:#475569;font-size:12px;">Definissez un rayon puis cliquez sur Chercher.</span>
    </div>
  </div>
</div>

</body>
</html>
""").replace("__CFG_JSON__", _cfg_json).replace("__CALLSIGN_JSON__", _callsign_json)

# ── Routes Flask ─────────────────────────────────────────────────────────────

@app.route('/sw.js')
def sw_js():
    """Service Worker minimal pour les notifications push."""
    js = (
        "self.addEventListener('push',function(e){"
        "var d=e.data?e.data.json():{};e.waitUntil("
        "self.registration.showNotification(d.title||'Py-APRS',"
        "{body:d.body||'',tag:d.tag||'aprs',renotify:true}));});"
        "self.addEventListener('notificationclick',function(e){"
        "e.notification.close();});"
    )
    from flask import Response as _R
    return _R(js, mimetype='application/javascript',
              headers={'Service-Worker-Allowed': '/'})

@app.route('/send_beacon', methods=['POST'])
@_login_required
def send_beacon():
    _do_send_beacon()
    return jsonify({"status": "queued"})

@app.route('/send_weather', methods=['POST'])
@_login_required
def send_weather():
    """Récupère la météo Open-Meteo et émet un beacon APRS météo (@…_)."""
    result = _do_send_weather()
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)

@app.route('/send_propagation', methods=['POST'])
@_login_required
def send_propagation():
    """Récupère les indices NOAA et émet un beacon APRS propagation (>SFI:… A:… K:…)."""
    result = _do_send_propagation()
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)

@app.route('/send_status', methods=['POST'])
@_login_required
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

@app.route('/send_text1', methods=['POST'])
@_login_required
def send_text1():
    """Émet immédiatement le texte libre 1 comme beacon APRS."""
    text = (config_manager.data.get("beacon_text1") or "").strip()
    if not text:
        return jsonify({"error": "Texte libre 1 non configuré"}), 400
    _do_send_text1()
    return jsonify({"status": "queued"})

@app.route('/send_text2', methods=['POST'])
@_login_required
def send_text2():
    """Émet immédiatement le texte libre 2 comme beacon APRS."""
    text = (config_manager.data.get("beacon_text2") or "").strip()
    if not text:
        return jsonify({"error": "Texte libre 2 non configuré"}), 400
    _do_send_text2()
    return jsonify({"status": "queued"})

@app.route('/send_raw', methods=['POST'])
@_login_required
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

    logger.debug("[TX] send_raw -> dest=%s payload=%s", dest_addr, payload[:60])
    try:
        tx_queue.put_nowait({"dest": dest_addr, "payload": payload, "path": path,
                             "aprs_type": aprs_type, "extra": {"comment": msg}})
        return jsonify({"status": "queued"})
    except queue.Full:
        return jsonify({"status": "busy", "error": "File TX pleine"}), 429

@app.route('/rx_test')
@_login_required
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
@_login_required
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
@_login_required
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
            b = p[0].strip('*').ljust(6)
            ssid = int(p[1].strip('*')) if len(p)>1 else 0
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
@_login_required
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
@_login_required
def config_status():
    return jsonify(_reload_status)

@app.route('/igate_status')
@_login_required
def igate_status():
    return jsonify({
        "enabled":      config_manager.data.get("igate_enabled", False),
        "connected":    igate_client.connected,
        "status":       igate_client.status,
        "frames_gated": igate_client.frames_rx_gated,
        "frames_is_rx": igate_client.frames_is_rx,
    })

@app.route('/digi/status')
@_login_required
def digi_status():
    return jsonify({
        "enabled":    config_manager.data.get("digi_enabled", False),
        "aliases":    config_manager.data.get("digi_aliases", []),
        "digi_limit": config_manager.data.get("digi_limit", 2),
        "digipeated": digipeater.frames_digipeated,
    })

@app.route('/log')
@_login_required
def view_log():
    """Retourne les N dernières lignes du log courant (JSON)."""
    lines_req = min(int(request.args.get("n", 200)), 2000)
    level_filter = request.args.get("level", "").upper()   # DEBUG INFO WARNING ERROR
    try:
        with open(_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        # Filtrer par niveau si demandé
        if level_filter:
            all_lines = [l for l in all_lines if ("  " + level_filter + " ") in l
                         or ("  " + level_filter + "  ") in l]
        tail = all_lines[-lines_req:]
        return jsonify({"lines": [l.rstrip() for l in tail], "total": len(all_lines)})
    except FileNotFoundError:
        return jsonify({"lines": [], "total": 0})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/log/archives')
@_login_required
def list_log_archives():
    """Liste les archives de log disponibles."""
    import glob
    pattern = _LOG_FILE + ".*.gz"
    files = sorted(glob.glob(pattern), reverse=True)
    result = []
    for f in files:
        try:
            size = os.path.getsize(f)
            result.append({"name": os.path.basename(f), "size": size,
                           "size_human": f"{size/1024:.1f} Ko"})
        except OSError:
            pass
    return jsonify({"archives": result, "log_file": _LOG_FILE,
                    "backup_count": _LOG_BACKUP_COUNT})


@app.route('/rx_stream')
@_login_required
def rx_stream():
    def generate():
        local_q = queue.Queue(maxsize=200)
        with _listeners_lock:
            listeners.append(local_q)
        try:
            yield "data: {\"type\":\"connected\"}\n\n"
            consecutive_errors = 0
            while True:
                try:
                    frame = local_q.get(timeout=8)  # PATCH_MONITOR_FREEZE_267
                    yield "data: %s\n\n" % json.dumps(frame, ensure_ascii=False)
                    consecutive_errors = 0
                except queue.Empty:
                    # Keepalive — si le client est mort, GeneratorExit sera levé
                    yield ": keepalive\n\n"
                except GeneratorExit:
                    break
                except Exception:
                    consecutive_errors += 1
                    if consecutive_errors > 3:
                        break
        finally:
            with _listeners_lock:
                try:
                    listeners.remove(local_q)
                except ValueError:
                    pass
    return Response(generate(), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})

# ── Routes Chat ───────────────────────────────────────────────────────────────

@app.route('/chat/contacts')
@_login_required
def chat_contacts():
    return jsonify(chat_manager.get_contacts())

@app.route('/chat/history/<callsign>')
@_login_required
def chat_history(callsign):
    chat_manager.mark_read(callsign)
    return jsonify(chat_manager.get_history(callsign))

@app.route('/chat/send', methods=['POST'])
@_login_required
def chat_send():
    data  = request.json
    dest  = data.get('dest', '').upper().strip()
    text  = data.get('text', '').strip()[:67]
    if not dest or not text:
        return jsonify({"error": "dest/text requis"}), 400
    msgno   = chat_manager._next_msgno()
    payload = ":" + dest.ljust(9) + ":" + text + "{" + msgno + "}"
    chat_manager.add_outgoing(dest, text, msgno)
    # custom_path : permet de forcer ARISS pour les messages ISS
    custom_path = (data.get('custom_path') or '').strip() or None
    try:
        tx_queue.put_nowait({
            "dest": "APRS", "payload": payload, "path": custom_path,
            "aprs_type": "Message", "extra": {"comment": text, "msg_dest": dest}
        })
        return jsonify({"status": "queued", "msgno": msgno})
    except queue.Full:
        return jsonify({"status": "busy"}), 429

@app.route('/chat/delete/<callsign>', methods=['DELETE'])
@_login_required
def chat_delete(callsign):
    """Supprime toute la conversation avec un indicatif."""
    cs = callsign.upper().strip()
    if not cs:
        return jsonify({"error": "indicatif requis"}), 400
    deleted = chat_manager.delete_conversation(cs)
    if deleted:
        logger.info("[QSO] Conversation supprimée : %s", cs)
        return jsonify({"ok": True, "callsign": cs})
    return jsonify({"ok": False, "error": "conversation introuvable"}), 404

# ── Broadcaster SSE ───────────────────────────────────────────────────────────

listeners = []
_listeners_lock = threading.Lock()

stations_positions      = {}
stations_positions_lock = threading.Lock()

# ── Traînée (breadcrumbs) pour stations mobiles et ballons ───────────────────
# MOBILE_TRAIL_PATCH_APPLIED
# Symboles APRS considérés comme mobiles (table primaire '/' + secondaire '\\')
_TRAIL_SYMBOLS = frozenset({
    '/j', '/k', '/u', '/v', '/O', '/f', '/g',   # voiture, 4x4, bus, camion, ballon, moto
    '/[', '/X', '/Y',                             # piéton, hélico, yacht
    '/a', '/s', '/>', '/<', '/^', '/n',           # ambulance, bateau, voiture, moto, avion, camion
    '\\j', '\\k', '\\u', '\\v',                   # variantes table secondaire
    '\\a', '\\s', '\\>', '\\<', '\\^',           # variantes secondaires
    '/r',                                          # récréatif/vélo
})
TRAIL_MAX = 100   # nombre max de positions mémorisées par station mobile



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
            logger.info("[IGATE] Connecté à %s:%d — %s", server, port, banner.strip())

            # Login
            login_line = "user %s pass %s vers Py-APRS 2.0" % (callsign, passcode)
            if filt:
                login_line += " filter %s" % filt
            self._send_raw(login_line)

            # Lire la réponse au login
            resp = self._fobj.readline()
            logger.debug("[IGATE] Login réponse : %s", resp.strip())
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
            logger.error("[IGATE] Erreur connexion : %s", e)
            return False

    # ── Envoi d'une ligne brute ────────────────────────────────────────────
    def _send_raw(self, line):
        try:
            with self._lock:
                self._sock.sendall((line + "\r\n").encode('utf-8', errors='replace'))
        except Exception as e:
            logger.error("[IGATE] Erreur envoi : %s", e)
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
                    logger.warning("[IGATE] Connexion fermée par le serveur")
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
                    logger.error("[IGATE] Erreur lecture : %s", e)
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
                logger.info("[IGATE] Reconnexion dans %ds…", self.RECONNECT_DELAY)
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


# ══════════════════════════════════════════════════════════════════════════════
# ── DIGIPEATER APRS ───────────────────────────────────────────────────────────
# Retransmet les trames RF reçues en décrémentant WIDEn-N et en gérant les
# alias RELAY / WIDE1. Conforme au standard APRS 1.0.1.
# ══════════════════════════════════════════════════════════════════════════════

class APRSDigipeater:
    """Moteur digipeater APRS logiciel.

    Règles appliquées :
      1. Trame ignorée si émise par notre propre station.
      2. Trame ignorée si déjà retransmise (marqueur *) par nous.
      3. Alias simples (RELAY, WIDE1, WIDE1-1) : retransmettre + marquer *.
      4. WIDEn-N (n>0, N>0) :
           - Si N==1 → transmettre + marquer chemin* (nœud terminal).
           - Si N>1  → transmettre + décrémenter vers WIDEn-(N-1) + marquer *.
      5. Filtre anti-dupli : hash SHA1 (src+dest+payload) dans fenêtre 30 s.
      6. Limite max_hops configurable (digi_limit).
    """

    _WIDE_RE = re.compile(r'^WIDE(\d)-(\d)$', re.IGNORECASE)
    DEDUP_WINDOW = 30   # secondes

    def __init__(self):
        self.frames_digipeated = 0
        self._dedup: dict = {}           # hash → timestamp
        self._lock  = threading.Lock()

    # ── Vérification/déduplication ────────────────────────────────────────
    def _is_duplicate(self, frame) -> bool:
        # Clé = src + dest + payload (tronqué à 80 car pour ignorer les timestamps variables)
        raw  = "%s>%s:%s" % (frame.get("src",""), frame.get("dest",""), frame.get("payload","")[:80])
        h    = hashlib.sha1(raw.encode()).hexdigest()[:16]
        now  = time.time()
        with self._lock:
            expired = [k for k, t in self._dedup.items() if now - t > self.DEDUP_WINDOW]
            for k in expired:
                del self._dedup[k]
            if h in self._dedup:
                return True
            self._dedup[h] = now
            return False

    # ── Traitement d'une trame ────────────────────────────────────────────
    def process(self, frame: dict) -> bool:
        """Analyse la trame et la retransmet si nécessaire.
        Retourne True si une retransmission a été effectuée."""
        cfg      = config_manager.data
        if not cfg.get("digi_enabled", False):
            return False

        my_call  = cfg.get("callsign", "N0CALL").upper().strip()
        aliases  = [a.upper().strip() for a in cfg.get("digi_aliases", [])]
        max_hops = int(cfg.get("digi_limit", 2))
        src      = frame.get("src", "").upper().strip()
        payload  = frame.get("payload", "")
        path_str = frame.get("path", "")

        # 1. Ne pas digi ses propres trames
        if src.split('-')[0] == my_call.split('-')[0]:
            return False

        # 2. Ignorer les trames venant d'IS
        if frame.get("_source") == "IS":
            return False

        # 3. Anti-dupli
        if self._is_duplicate(frame):
            logger.debug("[DIGI] Duplicat ignoré : %s", src)
            return False

        # Analyser le path
        path_parts = [p.strip() for p in path_str.split(',') if p.strip()] if path_str else []
        new_path   = list(path_parts)
        retransmit = False

        for i, hop in enumerate(path_parts):
            hop_clean = hop.rstrip('*').upper()

            # Déjà traité par nous (notre call dans le path avec *) — comparer la base sans SSID
            if hop_clean.split('-')[0] == my_call.split('-')[0] and hop.endswith('*'):
                return False

            # --- Alias simple (RELAY, WIDE1, WIDE1-1, etc.) ---
            if hop_clean in aliases and not hop.endswith('*'):
                new_path[i] = my_call + '*'
                retransmit = True
                break

            # --- WIDEn-N standard ---
            m = self._WIDE_RE.match(hop_clean)
            if m and not hop.endswith('*'):
                n = int(m.group(1))
                N = int(m.group(2))
                if N == 0:
                    continue   # épuisé
                if n > max_hops:
                    logger.debug("[DIGI] WIDEn-N ignoré (n=%d > limit=%d) : %s", n, max_hops, src)
                    continue
                if N == 1:
                    # Dernier saut : marquer comme consommé (N → 0)
                    # Le bit H (has-been-repeated) est porté par notre call inséré, pas le hop WIDE
                    new_path[i] = "WIDE%d-0" % n
                else:
                    # Décrémenter N sans marquer * (le hop suivant n'est pas encore traité)
                    new_path[i] = "WIDE%d-%d" % (n, N - 1)
                # Insérer notre call juste avant pour traçabilité (chemin parcouru)
                new_path.insert(i, my_call + '*')
                retransmit = True
                break

        if not retransmit:
            return False

        # Reconstruire le path : supprimer les WIDEn-0* épuisés (inutiles dans la trame AX.25)
        clean_path   = [h for h in new_path if not re.match(r'^WIDE\d-0\*?$', h, re.IGNORECASE)]
        new_path_str = ','.join(clean_path)
        dest         = frame.get("dest", "APRS")
        tnc2_payload = payload

        logger.info("[DIGI] Retransmission : %s>%s,%s (%d car.)",
                    src, dest, new_path_str, len(tnc2_payload))

        try:
            tx_queue.put_nowait({
                "dest":      dest,
                "src":       src,          # préserver l'indicatif original
                "payload":   tnc2_payload,
                "path":      new_path_str,
                "aprs_type": "Digi",
                "extra":     {"comment": "[DIGI] %s" % src}
            })
            with self._lock:
                self.frames_digipeated += 1
            return True
        except queue.Full:
            logger.warning("[DIGI] TX queue pleine, trame abandonnée")
            return False


digipeater = APRSDigipeater()  # PATCH_DIGI_APPLIED_v1




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
                    logger.debug("[TELEM] PARM recu de %s", _src)
                elif _body.startswith("UNIT."):
                    with _telem_meta_lock:
                        _telem_meta.setdefault(_src, {})["unit"] = _parse_telem_parm(_body[5:])
                    logger.debug("[TELEM] UNIT recu de %s", _src)
                elif _body.startswith("EQNS."):
                    with _telem_meta_lock:
                        _telem_meta.setdefault(_src, {})["eqns"] = _parse_telem_eqns(_body[5:])
                    logger.debug("[TELEM] EQNS recu de %s", _src)
                elif _body.startswith("BITS."):
                    parts_b = _body[5:].split(',', 9)
                    with _telem_meta_lock:
                        meta = _telem_meta.setdefault(_src, {})
                        meta["bits_mask"]  = parts_b[0].strip() if parts_b else ""
                        meta["bits_label"] = parts_b[1].strip() if len(parts_b) > 1 else ""
                        meta["bits_names"] = [p.strip() for p in parts_b[2:10]]
                    logger.debug("[TELEM] BITS recu de %s", _src)

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
                    found_dest, found_msgno = chat_manager.mark_ack(ackto)
                    logger.info("[MSG] ACK recu pour msgno=%s de %s", ackto, src)
                    if found_dest:
                        # Diffuser l'event ack au frontend pour mise à jour live de la bulle TX
                        ack_event = {
                            "type":   "msg_ack",
                            "msgno":  found_msgno,
                            "dest":   found_dest,
                            "src":    src,
                        }
                        for _q in list(listeners):
                            try: _q.put_nowait(ack_event)
                            except queue.Full: pass
                elif dest and dest in my_calls():
                    chat_manager.add_incoming(src, text, msgno)
                    frame["_chat"] = True
                    logger.info("[MSG] Message recu de %s : %s (msgno=%s)", src, text[:40], msgno)
                    if msgno:
                        ack_payload = ":" + src.ljust(9) + ":ack" + msgno
                        try:
                            tx_queue.put_nowait({
                                "dest": "APRS", "payload": ack_payload, "path": None,
                                "aprs_type": "ACK", "extra": {"comment": "ack" + msgno}
                            })
                            logger.debug("[MSG] ACK envoye : ack%s -> %s", msgno, src)
                        except Exception as e:
                            logger.error("[MSG] Erreur envoi ACK : %s", e)

            # ── iGate : forward RF → APRS-IS ─────────────────────────────
            if (config_manager.data.get("igate_enabled") and
                    igate_client.connected and
                    frame.get("type") != "tx_event" and
                    frame.get("type") != "rx_level" and
                    frame.get("_source") != "IS" and          # pas de boucle
                    frame.get("aprs_type") not in ("Telemetrie",) and
                    frame.get("src")):
                igate_client.gate_rf_to_is(frame)

            # ── Digipeater : retransmission RF ────────────────────────────
            if (frame.get("type") not in ("tx_event", "rx_level") and
                    frame.get("_source") != "IS" and
                    frame.get("src")):
                digipeater.process(frame)

            # Assigner _fid AVANT de broadcaster (SSE + historique ont le même id)
            if frame.get("type") != "rx_level" and "_fid" not in frame:
                frame["_fid"] = next(_fid_counter)

            # Historique (hors rx_level)
            if frame.get("type") != "rx_level":
                if "_ts" not in frame:
                    frame["_ts"] = time.time()
                with rx_history_lock:
                    rx_history.append(frame)
                # Marquer l'activité pour la jauge
                if frame.get("type") != "tx_event":
                    _rx_activity_ts[0] = time.time()
            # ── Mise à jour positions stations ──────────────────────────
            _e = frame.get("extra", {})
            if _e.get("lat") is not None and frame.get("type") not in ("tx_event", "rx_level"):
                _cs  = frame.get("src", "").upper().strip().split(",")[0]
                _lat = _e["lat"];  _lon = _e["lon"];  _now = time.time()
                # Détecter si c'est une station mobile/ballon (table+code symbol)
                _sym = (_e.get("symbol_table", "") + _e.get("symbol_code", "")).lower()
                _mobile = _sym in _TRAIL_SYMBOLS or bool(_re.search(r'[/\\][jkuvoOXY\[]', _sym))
                with stations_positions_lock:
                    _prev = stations_positions.get(_cs, {})
                    _trail = list(_prev.get("trail", []))
                    if _mobile:
                        # N'ajouter un point que si la position a réellement changé
                        _spd = _e.get("speed_kmh", -1)
                        _crs = _e.get("course", -1)
                        if _spd is None: _spd = -1
                        if _crs is None: _crs = -1
                        if _trail:
                            _last = _trail[-1]
                            if abs(_last[0] - _lat) > 1e-5 or abs(_last[1] - _lon) > 1e-5:
                                _trail.append((_lat, _lon, _now, _spd, _crs))
                        else:
                            _trail.append((_lat, _lon, _now, _spd, _crs))
                        if len(_trail) > TRAIL_MAX:
                            _trail = _trail[-TRAIL_MAX:]
                    stations_positions[_cs] = {
                        "lat": _lat, "lon": _lon, "ts": _now,
                        "trail": _trail,
                        "mobile": _mobile,
                    }

            # ── Carnet de trafic : enregistrement automatique ─────────────
# Diffuser à tous les clients SSE — retrait automatique des listeners morts
# PATCH_MONITOR_FREEZE_267
            _MAX_LISTENER_ERRORS = 5
            _dead = []
            for q in list(listeners):
                try:
                    q.put_nowait(frame)
                    # Réinitialiser le compteur d'erreurs si l'envoi réussit
                    if hasattr(q, '_err_count'):
                        q._err_count = 0
                except queue.Full:
                    q._err_count = getattr(q, '_err_count', 0) + 1
                    if q._err_count >= _MAX_LISTENER_ERRORS:
                        _dead.append(q)
                        logger.warning("[SSE] Listener mort retiré (queue pleine x%d)", _MAX_LISTENER_ERRORS)
            if _dead:
                with _listeners_lock:
                    for _dq in _dead:
                        try:
                            listeners.remove(_dq)
                        except ValueError:
                            pass

        except queue.Empty:
            pass
        except Exception as _bcast_exc:
            logger.error("[BROADCASTER] Exception inattendue (trame ignorée) : %s", _bcast_exc, exc_info=True)

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
@_login_required
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
            # Pour les retransmissions digi, préserver le src original
            _job_src = job.get("src") if job.get("aprs_type") == "Digi" else None
            modem.send_packet(job["dest"], job["payload"], job.get("path"), custom_src=_job_src)
        except Exception as e:
            APRSModem.tx_last_error = str(e)
            logger.error("[TX WORKER] ERREUR : %s", e)
            import traceback; traceback.print_exc()
        finally:
            tx_queue.task_done()

threading.Thread(target=_tx_worker, daemon=True, name="tx-worker").start()

# ── Démarrage du thread de retry ACK (après tx_queue et listeners) ────────────
threading.Thread(target=_ack_retry_worker, daemon=True, name="ack-retry").start()

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
            "text1":       _do_send_text1,
            "text2":       _do_send_text2,
        }
        logger.info("[BCN] Thread démarré pour type=%s", btype)
        while True:
            schedules = config_manager.data.get('beacon_schedules', {})
            interval  = schedules.get(btype, 0)
            if interval and interval > 0:
                # Émettre
                logger.info("[BCN] Émission type=%s interval=%d min", btype, interval)
                fn = _dispatch.get(btype)
                if fn:
                    try:
                        fn()
                    except Exception as e:
                        logger.error("[BCN] Erreur type=%s : %s", btype, e)
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
                        logger.info("[BCN] Config changée type=%s (%d→%d) — redémarrage", btype, interval, new_interval)
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
    _ALL_TYPES = ["station", "iss", "meteo", "propagation", "text1", "text2"]
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
        logger.debug("[TX] Beacon queued : %s", payload[:60])
    except Exception as e:
        logger.error("[TX] Beacon queue erreur : %s", e)


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

    logger.debug("[WX] Requete Open-Meteo : lat=%.4f lon=%.4f", lat, lon)
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
        logger.error("[WX] Erreur Open-Meteo : %s", e)
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
                logger.debug("[PROP] Kp=%.2f A=%.0f (%s)",
                    result.get("k_index", 0), result.get("a_index", 0),
                    last.get("time_tag", "?"))
    except Exception as e:
        logger.error("[PROP] Erreur Kp NOAA : %s", e)

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
                    logger.debug("[PROP] SFI=%.0f (%s)", result["sfi"], entry.get("time_tag", "?"))
                    break
    except Exception as e:
        logger.error("[PROP] Erreur SFI NOAA : %s", e)

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

    logger.info("[PROP] SFI=%s A=%s K=%s HF=%s VHF=%s",
        ("%.0f" % sfi) if sfi else "?",
        ("%.0f" % a)   if a   else "?",
        ("%.1f" % k)   if k   else "?",
        result["hf_cond"], result["vhf_cond"]
    )
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
        logger.debug("[PROP] Beacon propagation queued : %s", payload)
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
        logger.debug("[TX] Beacon ISS queued : %s", payload[:60])
    except Exception as e:
        logger.error("[TX] Beacon ISS erreur : %s", e)


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
            logger.warning("[WX] Coordonnées manuelles absentes ou invalides — beacon météo annulé")
            return {"error": "Latitude et longitude requises dans les réglages"}
    else:
        # ── Mode Locator Maidenhead (défaut) ──────────────────────────────────
        grid = cfg.get('maidenhead', '')
        lat, lon = _grid_to_latlon(grid)
        if lat is None:
            logger.warning("[WX] Locator Maidenhead absent ou invalide — beacon météo annulé")
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

    logger.debug("[WX] Payload : %s", payload[:80])
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

def _do_send_text1():
    """Émet un beacon texte libre APRS depuis beacon_text1."""
    text = (config_manager.data.get("beacon_text1") or "").strip()
    if not text:
        logger.warning("[TX] Texte libre 1 vide — beacon annulé")
        return
    payload = ">" + text
    try:
        tx_queue.put_nowait({"dest": "APRS", "payload": payload, "path": None,
                             "aprs_type": "Texte1", "extra": {"comment": text}})
        logger.debug("[TX] Texte1 queued : %s", payload[:60])
    except Exception as e:
        logger.error("[TX] Texte1 queue erreur : %s", e)


def _do_send_text2():
    """Émet un beacon texte libre APRS depuis beacon_text2."""
    text = (config_manager.data.get("beacon_text2") or "").strip()
    if not text:
        logger.warning("[TX] Texte libre 2 vide — beacon annulé")
        return
    payload = ">" + text
    try:
        tx_queue.put_nowait({"dest": "APRS", "payload": payload, "path": None,
                             "aprs_type": "Texte2", "extra": {"comment": text}})
        logger.debug("[TX] Texte2 queued : %s", payload[:60])
    except Exception as e:
        logger.error("[TX] Texte2 queue erreur : %s", e)


threading.Thread(target=_beacon_scheduler, daemon=True, name="beacon-supervisor").start()


@app.route('/version')
@_login_required
def get_version():
    import json as _j
    return _j.dumps({
        "version": APP_VERSION,
        "date":    APP_VERSION_DATE,
        "changelog": APP_CHANGELOG,
    }), 200, {'Content-Type': 'application/json'}

@app.route('/known_callsigns')
@_login_required
def known_callsigns():
    """Retourne la liste des indicatifs dont la position a été reçue depuis le démarrage."""
    with stations_positions_lock:
        cs_list = sorted(stations_positions.keys())
    return jsonify(cs_list)

@app.route('/map_clear_trails', methods=['DELETE', 'POST'])
@_login_required
def map_clear_trails():
    """Supprime les traînées (breadcrumbs) de toutes les stations mobiles.
    Les marqueurs et positions de base sont conservés ; seul l'historique
    de trajets (champ 'trail') est effacé.
    """
    count = 0
    with stations_positions_lock:
        for pos in stations_positions.values():
            if pos.get('trail'):
                pos['trail'] = []
                count += 1
    logger.info("[MAP] Traînées mobiles effacées (%d station(s))", count)
    import json as _j
    return _j.dumps({"status": "ok", "cleared": count}), 200, {'Content-Type': 'application/json'}


@app.route('/map_trails')
@_login_required
def map_trails():
    """Retourne les traînées (breadcrumbs) des stations mobiles.
    Format : { "F4XXX-9": {"lat":..,"lon":..,"mobile":true,"trail":[[lat,lon,ts],..]}, … }
    """
    with stations_positions_lock:
        data = {
            cs: {
                "lat":    pos["lat"],
                "lon":    pos["lon"],
                "mobile": pos.get("mobile", False),
                "trail":  pos.get("trail", []),
            }
            for cs, pos in stations_positions.items()
            if pos.get("mobile") and pos.get("trail")
        }
    import json as _j
    return _j.dumps(data), 200, {'Content-Type': 'application/json'}

@app.route('/nearby_stations')
@_login_required
def nearby_stations():
    """Recherche périmétrique des stations APRS connues.
    Paramètres GET :
      lat        float  — latitude centre (défaut : station locale)
      lon        float  — longitude centre (défaut : station locale)
      radius_km  float  — rayon en km (défaut : 50, max : 1000)
      max_age_s  float  — âge max des données en secondes (défaut : 3600)
      mobile_only int   — 1 = mobiles uniquement (défaut : 0)
    Réponse JSON :
      { "center": {lat, lon}, "radius_km": float, "stations": [
          { "callsign", "lat", "lon", "distance_km", "mobile", "age_s", "ts" }, …
        ] }
    """
    import math as _m
    import json as _j
    import time as _t

    # ── Coordonnées centre ──────────────────────────────────────────────────
    def _safe_float(v):
        try: return float(v)
        except: return None

    lat = _safe_float(request.args.get("lat"))
    lon = _safe_float(request.args.get("lon"))
    if lat is None or lon is None:
        # Fallback 1 : coordonnées manuelles
        lat = _safe_float(config_manager.data.get("lat_manual"))
        lon = _safe_float(config_manager.data.get("lon_manual"))
    if lat is None or lon is None:
        # Fallback 2 : dériver depuis le locator Maidenhead
        mh = (config_manager.data.get("maidenhead") or "").strip().upper()
        if len(mh) >= 4:
            try:
                _lo = (ord(mh[0]) - 65) * 20 - 180
                _la = (ord(mh[1]) - 65) * 10 - 90
                _lo += int(mh[2]) * 2
                _la += int(mh[3]) * 1
                if len(mh) >= 6:
                    _lo += (ord(mh[4]) - 65) * (2/24) + (1/24)
                    _la += (ord(mh[5]) - 65) * (1/24) + (0.5/24)
                else:
                    _lo += 1.0; _la += 0.5
                lat, lon = _la, _lo
            except Exception:
                pass
    if lat is None or lon is None:
        return _j.dumps({"error": "lat/lon non disponibles — configurez les coordonnées ou le locator Maidenhead dans les réglages"}),\
               400, {'Content-Type': 'application/json'}

    radius_km   = min(float(request.args.get("radius_km",  50)),  1000.0)
    max_age_s   = float(request.args.get("max_age_s",  3600))
    mobile_only = request.args.get("mobile_only", "0") == "1"

    # ── Haversine (Python) ──────────────────────────────────────────────────
    def _hav_km(la1, lo1, la2, lo2):
        R = 6371.0
        dlat = _m.radians(la2 - la1)
        dlon = _m.radians(lo2 - lo1)
        a = _m.sin(dlat/2)**2 + _m.cos(_m.radians(la1))*_m.cos(_m.radians(la2))*_m.sin(dlon/2)**2
        return R * 2 * _m.asin(_m.sqrt(a))

    now = _t.time()
    result = []

    with stations_positions_lock:
        snapshot = dict(stations_positions)

    for cs, pos in snapshot.items():
        if pos.get("lat") is None: continue
        age = now - pos.get("ts", 0)
        if age > max_age_s: continue
        if mobile_only and not pos.get("mobile"): continue
        dist = _hav_km(lat, lon, pos["lat"], pos["lon"])
        if dist > radius_km: continue
        result.append({
            "callsign":    cs,
            "lat":         round(pos["lat"], 6),
            "lon":         round(pos["lon"], 6),
            "distance_km": round(dist, 2),
            "mobile":      bool(pos.get("mobile")),
            "age_s":       round(age),
            "ts":          pos.get("ts", 0),
        })

    result.sort(key=lambda x: x["distance_km"])
    return _j.dumps({
        "center":     {"lat": round(lat, 6), "lon": round(lon, 6)},
        "radius_km":  radius_km,
        "count":      len(result),
        "stations":   result,
    }), 200, {'Content-Type': 'application/json'}


@app.route('/beacon_status')
@_login_required
def beacon_status():
    """Retourne l'état de chaque type de balise : interval + next_in."""
    _ALL_TYPES = ["station", "iss", "meteo", "propagation", "text1", "text2"]
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
@_login_required
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


# ── Ville la plus proche (reverse geocoding Nominatim) ────────────────────────
# Cache LRU serveur : clé = (lat arrondi à 2 déc., lon arrondi à 2 déc.)
#   → ~1 km de précision, évite les requêtes répétées pour la même zone
_nearest_city_cache = {}
_nearest_city_lock  = threading.Lock()
_NOMINATIM_UA       = "APRS-Station/1.0 (local)"  # User-Agent requis par Nominatim

def _fetch_nearest_city(lat: float, lon: float) -> dict:
    """Interroge Nominatim et retourne un dict {city, state, country, display}."""
    import urllib.request as _ur
    import urllib.parse   as _up
    url = ("https://nominatim.openstreetmap.org/reverse"
           "?format=jsonv2&lat={lat}&lon={lon}&zoom=10&accept-language=fr"
           ).format(lat=round(lat, 5), lon=round(lon, 5))
    req = _ur.Request(url, headers={"User-Agent": _NOMINATIM_UA})
    try:
        with _ur.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        addr = data.get("address", {})
        city = (addr.get("city")
                or addr.get("town")
                or addr.get("village")
                or addr.get("municipality")
                or addr.get("county")
                or "")
        state   = addr.get("state", "")
        country = addr.get("country_code", "").upper()
        display = city
        if state and city and state.lower() != city.lower():
            display = city + ", " + state
        if country and country != "FR":
            display = display + " (" + country + ")" if display else country
        return {"city": city, "state": state, "country": country,
                "display": display or data.get("display_name", "")[:60]}
    except Exception as exc:
        logger.debug("[CITY] Nominatim erreur : %s", exc)
        return {"city": "", "state": "", "country": "", "display": ""}

@app.route('/nearest_city')
@_login_required
def nearest_city():
    """Retourne la ville la plus proche d'une position GPS.
    Paramètres GET : lat, lon
    Réponse JSON   : {city, state, country, display, cached}
    Utilise un cache interne pour limiter les appels à Nominatim (1 rq/s max).
    """
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except (TypeError, ValueError):
        return json.dumps({"error": "lat/lon invalides"}), 400, \
               {'Content-Type': 'application/json'}

    # Clé de cache : grille ~1 km (2 décimales ≈ 1.1 km)
    cache_key = (round(lat, 2), round(lon, 2))
    with _nearest_city_lock:
        if cache_key in _nearest_city_cache:
            result = dict(_nearest_city_cache[cache_key])
            result["cached"] = True
            return json.dumps(result), 200, {'Content-Type': 'application/json'}

    result = _fetch_nearest_city(lat, lon)
    result["cached"] = False

    with _nearest_city_lock:
        # Limiter la taille du cache à 2000 entrées (~ 2 Mo max)
        if len(_nearest_city_cache) >= 2000:
            try:
                del _nearest_city_cache[next(iter(_nearest_city_cache))]
            except StopIteration:
                pass
        _nearest_city_cache[cache_key] = {k: v for k, v in result.items()
                                          if k != "cached"}

    logger.debug("[CITY] %s → %s", cache_key, result.get("display"))
    return json.dumps(result), 200, {'Content-Type': 'application/json'}


# ── Alerte propagation VHF ────────────────────────────────────────────────────
_vhf_alert_state = {
    "kp_prev":   None,   # dernier Kp connu
    "es_prev":   False,  # Es actif au dernier poll
}

@app.route('/vhf_alert_check')
@_login_required
def vhf_alert_check():
    """
    Vérifie si les conditions VHF ont changé de façon favorable depuis
    le dernier appel. Retourne les alertes à déclencher.
    Réponse : {alerts: [{type, label, detail, level}], kp, sfi, a, es, score}
    """
    import json as _j
    from datetime import datetime

    data   = _fetch_solar_indices()
    kp     = data.get("k_index")
    sfi    = data.get("sfi")
    a      = data.get("a_index")
    aurora = data.get("aurora", False)

    month  = datetime.utcnow().month
    es_season = 4 <= month <= 9
    es     = es_season and (kp is None or kp <= 3)

    # Score VHF global (même logique que le JS)
    score = 50
    if sfi is not None: score += min(25, max(-10, (sfi - 100) * 0.25))
    if kp  is not None: score += 10 if kp<=2 else 0 if kp<=4 else -15 if kp<=6 else -30
    if a   is not None: score += 5  if a<=7  else -5 if a<=20 else -20
    if es:              score += 15
    score = max(0, min(100, round(score)))

    prev_kp = _vhf_alert_state["kp_prev"]
    prev_es = _vhf_alert_state["es_prev"]

    alerts = []

    # Kp vient de descendre sous 2 (favorable) depuis une valeur plus haute
    if kp is not None and kp <= 2:
        if prev_kp is None or prev_kp > 2:
            alerts.append({
                "type":   "kp_drop",
                "level":  "good",
                "label":  "Kp favorable",
                "detail": "Kp %.1f — conditions VHF/UHF calmes, bonne propagation tropo" % kp,
            })

    # Kp monte au-dessus de 5 → aurora possible (ouverture exceptional VHF)
    if kp is not None and kp >= 5:
        if prev_kp is None or prev_kp < 5:
            alerts.append({
                "type":   "aurora",
                "level":  "special",
                "label":  "Aurora possible",
                "detail": "Kp %.1f — ouverture aurora VHF 144 MHz possible !" % kp,
            })

    # Es (Sporadic-E) vient d'apparaître
    if es and not prev_es:
        alerts.append({
            "type":   "es_open",
            "level":  "special",
            "label":  "Sporadic-E détecté",
            "detail": "Saison Es active, Kp %.1f — ouvertures 6m/2m possibles" % (kp or 0),
        })

    # Score VHF global vient de dépasser 70
    if score >= 70 and prev_kp is not None:
        prev_score = 50
        if prev_kp is not None:
            prev_score += 10 if prev_kp<=2 else 0 if prev_kp<=4 else -15 if prev_kp<=6 else -30
        if prev_score < 70 and not any(a["type"] == "kp_drop" for a in alerts):
            alerts.append({
                "type":   "score_up",
                "level":  "good",
                "label":  "Conditions VHF améliorées",
                "detail": "Score VHF %d/100 — bonne fenêtre de propagation" % score,
            })

    # Mettre à jour l'état
    _vhf_alert_state["kp_prev"] = kp
    _vhf_alert_state["es_prev"] = es

    return _j.dumps({
        "alerts": alerts,
        "kp":     kp,
        "sfi":    sfi,
        "a":      a,
        "es":     es,
        "score":  score,
        "aurora": aurora,
    }), 200, {'Content-Type': 'application/json'}


STATS_FILE = 'stats.json'

# ── Cache mémoire météo (évite les appels HTTP bloquants répétés) ─────────────
_WX_PROXY_CACHE    = {}   # clé "lat,lon" → {body, ts}
_WX_HISTORY_CACHE  = {}   # clé "lat,lon" → {payload, ts}
_WX_CACHE_TTL_S    = 600  # 10 minutes

@app.route('/stats/load')
@_login_required
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


@app.route('/stats/reset_mesh', methods=['POST'])
@_login_required
def stats_reset_mesh():
    """Efface uniquement les cases de maillage dans stats.json."""
    import json as _j
    try:
        data = {}
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, 'r') as f:
                data = _j.load(f)
        data.pop('gridCells', None)
        with open(STATS_FILE, 'w') as f:
            _j.dump(data, f)
        return _j.dumps({'ok': True}), 200, {'Content-Type': 'application/json'}
    except Exception as e:
        return _j.dumps({'ok': False, 'error': str(e)}), 500, {'Content-Type': 'application/json'}


@app.route('/stats/reset_dx', methods=['POST'])
@_login_required
def stats_reset_dx():
    """Efface le meilleur DX et la distance maximale dans stats.json."""
    import json as _j
    try:
        data = {}
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, 'r') as f:
                data = _j.load(f)
        data.pop('bestDx',    None)
        data.pop('maxDistKm', None)
        with open(STATS_FILE, 'w') as f:
            _j.dump(data, f)
        return _j.dumps({'ok': True}), 200, {'Content-Type': 'application/json'}
    except Exception as e:
        return _j.dumps({'ok': False, 'error': str(e)}), 500, {'Content-Type': 'application/json'}


@app.route('/wx_proxy')
@_login_required
def wx_proxy():
    """Proxy météo avec cache 10 min pour eviter les appels bloquants repetes."""
    import json as _j
    import urllib.request as _ur
    import ssl as _ssl
    lat = request.args.get("lat", "")
    lon = request.args.get("lon", "")
    if not lat or not lon:
        return _j.dumps({"error": "lat/lon manquants"}), 400, {"Content-Type": "application/json"}
    _ck = lat + "," + lon
    _cached = _WX_PROXY_CACHE.get(_ck)
    if _cached and (time.time() - _cached["ts"]) < _WX_CACHE_TTL_S:
        return _cached["body"], 200, {"Content-Type": "application/json"}
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude="  + lat +
        "&longitude=" + lon +
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m"
        ",wind_speed_10m,wind_direction_10m,weather_code,precipitation"
        "&wind_speed_unit=kmh&timezone=auto"
    )
    # Essai 1 : SSL systeme
    try:
        ctx = _ssl.create_default_context()
        req = _ur.Request(url, headers={"User-Agent": "RASPI-RADIO/1.0"})
        with _ur.urlopen(req, timeout=5, context=ctx) as resp:
            body = resp.read().decode("utf-8")
        _WX_PROXY_CACHE[_ck] = {"body": body, "ts": time.time()}
        return body, 200, {"Content-Type": "application/json"}
    except Exception as e1:
        logger.warning("[WX-PROXY] SSL systeme echoue : %s - tentative sans verif SSL", e1)
    # Essai 2 : SSL non verifie (fallback RPi)
    try:
        ctx2 = _ssl.create_default_context()
        ctx2.check_hostname = False
        ctx2.verify_mode    = _ssl.CERT_NONE
        req2 = _ur.Request(url, headers={"User-Agent": "RASPI-RADIO/1.0"})
        with _ur.urlopen(req2, timeout=5, context=ctx2) as resp2:
            body2 = resp2.read().decode("utf-8")
        _WX_PROXY_CACHE[_ck] = {"body": body2, "ts": time.time()}
        return body2, 200, {"Content-Type": "application/json"}
    except Exception as e2:
        logger.error("[WX-PROXY] Echec total : %s", e2)
        if _cached:
            return _cached["body"], 200, {"Content-Type": "application/json"}
        return _j.dumps({"error": str(e2)}), 502, {"Content-Type": "application/json"}


@app.route('/stats/save', methods=['POST'])
@_login_required
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


# ══════════════════════════════════════════════════════════════════════════════
# SGP4 simplifié — calcul des passages ISS (stdlib pure, sans open-notify.org)
# Précision orbitale : ±5 km / ±30 s  — suffisant pour planifier l'écoute APRS
# Sources TLE : CelesTrak GP (primaire) → CelesTrak stations (fallback)
#               → cache disque .iss_tle_cache (survit aux coupures réseau)
# ══════════════════════════════════════════════════════════════════════════════

import math   as _math
import struct as _struct   # noqa — utilisé par _tle_checksum

_SGP4_DEG2RAD = _math.pi / 180
_SGP4_TWOPI   = 2 * _math.pi
_SGP4_EARTH_R = 6378.137      # km — rayon équatorial WGS84
_SGP4_MU      = 398600.4418   # km³/s²
_SGP4_J2      = 1.08262668e-3
_SGP4_TLE_CACHE = ".iss_tle_cache"

# URLs de récupération TLE — tentées dans l'ordre
_SGP4_TLE_SOURCES = [
    # CelesTrak GP (nouveau format — 2024+)
    "https://celestrak.org/SATCAT/gp.php?CATNR=25544&FORMAT=TLE",
    # CelesTrak groupe stations (filtre ISS parmi les autres)
    "https://celestrak.org/satcat/elements/gp.php?GROUP=stations&FORMAT=TLE",
    # Heavens-Above (texte brut)
    "https://www.heavens-above.com/tle.aspx?satid=25544",
    # Satnogs (JSON → on extrait les lignes)
    "https://db.satnogs.org/api/tle/?norad_cat_id=25544&format=json",
]

# TLE de secours embarqué — mis à jour lors des releases, valide ~30 jours
_SGP4_FALLBACK_TLE = (
    "1 25544U 98067A   26145.50000000  .00016717  00000-0  10270-3 0  9999",
    "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.50372314441433",
)

# Cache mémoire du TLE courant : (line1, line2, fetch_ts)
_iss_tle_memory = [None, None, 0.0]
_ISS_TLE_TTL    = 3600 * 6   # recharge les TLE toutes les 6h


def _sgp4_tle_exp(s):
    """Décode la notation exposant compacte TLE : '-11606-4' → -1.1606e-5."""
    s = s.strip()
    if not s or set(s) <= {'0', '+', '-', ' '}:
        return 0.0
    try:
        sign = -1 if s[0] == '-' else 1
        body = s[1:] if s[0] in ('+', '-') else s
        mantissa = body[:-2].replace(' ', '')
        exp_s    = body[-2:]
        return sign * float("0." + mantissa) * 10 ** int(exp_s)
    except Exception:
        return 0.0


def _sgp4_parse_tle(line1, line2):
    """Parse deux lignes TLE → dict orbital."""
    import datetime as _dt_sgp4
    yy  = int(line1[18:20])
    yr  = 2000 + yy if yy < 57 else 1900 + yy
    doy = float(line1[20:32])
    epoch = (_dt_sgp4.datetime(yr, 1, 1, tzinfo=_dt_sgp4.timezone.utc)
             + _dt_sgp4.timedelta(days=doy - 1))

    inc  = float(line2[8:16])  * _SGP4_DEG2RAD
    raan = float(line2[17:25]) * _SGP4_DEG2RAD
    ecc  = float("0." + line2[26:33].strip())
    argp = float(line2[34:42]) * _SGP4_DEG2RAD
    ma   = float(line2[43:51]) * _SGP4_DEG2RAD
    mm   = float(line2[52:63])          # tr/jour
    n0   = mm * _SGP4_TWOPI / 86400     # rad/s
    a0   = (_SGP4_MU / (n0 * n0)) ** (1.0/3)

    return dict(epoch=epoch, inc=inc, raan=raan, ecc=ecc,
                argp=argp, ma=ma, n0=n0, a0=a0)


def _sgp4_mean_to_true(ma, ecc):
    """Résolution de l'équation de Kepler → anomalie vraie (rad)."""
    E = ma
    for _ in range(50):
        dE = (ma - E + ecc * _math.sin(E)) / (1.0 - ecc * _math.cos(E))
        E += dE
        if abs(dE) < 1e-10:
            break
    ta = 2.0 * _math.atan2(
        _math.sqrt(1 + ecc) * _math.sin(E / 2),
        _math.sqrt(1 - ecc) * _math.cos(E / 2),
    )
    return ta % _SGP4_TWOPI


def _sgp4_propagate(tle, t_unix):
    """
    Propagation SGP4 simplifiée (J2 uniquement).
    Retourne (lat_deg, lon_deg, alt_km).
    """
    dt    = t_unix - tle["epoch"].timestamp()
    n0    = tle["n0"]
    a0    = tle["a0"]
    ecc   = tle["ecc"]
    inc   = tle["inc"]

    # Précession J2
    p        = a0 * (1 - ecc * ecc)
    coef     = -1.5 * _SGP4_J2 * (_SGP4_EARTH_R / p) ** 2 * n0
    raan_t   = (tle["raan"] + coef * _math.cos(inc) * dt) % _SGP4_TWOPI
    argp_t   = (tle["argp"] + coef * (2.5 * _math.sin(inc)**2 - 2) * dt) % _SGP4_TWOPI
    ma_t     = (tle["ma"]   + n0 * dt) % _SGP4_TWOPI

    ta = _sgp4_mean_to_true(ma_t, ecc)
    r  = a0 * (1 - ecc * ecc) / (1 + ecc * _math.cos(ta))
    u  = argp_t + ta

    # Position ECI
    x = r * (_math.cos(raan_t) * _math.cos(u)
              - _math.sin(raan_t) * _math.sin(u) * _math.cos(inc))
    y = r * (_math.sin(raan_t) * _math.cos(u)
              + _math.cos(raan_t) * _math.sin(u) * _math.cos(inc))
    z = r *  _math.sin(inc) * _math.sin(u)

    # Latitude géocentrique + altitude
    rxy     = _math.sqrt(x*x + y*y)
    lat_gc  = _math.atan2(z, rxy)
    lon_eci = _math.atan2(y, x)
    alt     = _math.sqrt(x*x + y*y + z*z) - _SGP4_EARTH_R

    # GAST (temps sidéral de Greenwich)
    jd   = 2440587.5 + t_unix / 86400.0
    gast = _math.radians((280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360)

    lon_deg = (_math.degrees(lon_eci - gast)) % 360
    if lon_deg > 180:
        lon_deg -= 360

    return _math.degrees(lat_gc), lon_deg, alt


def _sgp4_elevation(sat_lat, sat_lon, sat_alt, obs_lat, obs_lon):
    """Angle d'élévation (degrés) du satellite depuis le sol observateur."""
    slat = _math.radians(sat_lat)
    slon = _math.radians(sat_lon)
    olat = _math.radians(obs_lat)
    olon = _math.radians(obs_lon)

    r_sat = _SGP4_EARTH_R + sat_alt
    sx = r_sat * _math.cos(slat) * _math.cos(slon)
    sy = r_sat * _math.cos(slat) * _math.sin(slon)
    sz = r_sat * _math.sin(slat)

    ox = _SGP4_EARTH_R * _math.cos(olat) * _math.cos(olon)
    oy = _SGP4_EARTH_R * _math.cos(olat) * _math.sin(olon)
    oz = _SGP4_EARTH_R * _math.sin(olat)

    dx, dy, dz = sx - ox, sy - oy, sz - oz
    dist = _math.sqrt(dx*dx + dy*dy + dz*dz)
    if dist < 1.0:
        return -90.0

    # Vecteur vertical local
    ux = _math.cos(olat) * _math.cos(olon)
    uy = _math.cos(olat) * _math.sin(olon)
    uz = _math.sin(olat)

    dot = (dx*ux + dy*uy + dz*uz) / dist
    return _math.degrees(_math.asin(max(-1.0, min(1.0, dot))))


def _sgp4_azimuth(sat_lat, sat_lon, obs_lat, obs_lon):
    """Retourne l'azimut (0-360°) depuis l'observateur vers le satellite."""
    import math as _m2
    slat = _m2.radians(sat_lat); slon = _m2.radians(sat_lon)
    olat = _m2.radians(obs_lat); olon = _m2.radians(obs_lon)
    dlon = slon - olon
    y_az = _m2.sin(dlon) * _m2.cos(slat)
    x_az = (_m2.cos(olat) * _m2.sin(slat)
            - _m2.sin(olat) * _m2.cos(slat) * _m2.cos(dlon))
    return (_m2.degrees(_m2.atan2(y_az, x_az)) + 360) % 360


def _sgp4_compute_passes(tle, obs_lat, obs_lon,
                         n=5, horizon=0.0, hours_ahead=48):
    """
    Calcule les n prochains passages ISS visibles depuis (obs_lat, obs_lon).
    horizon : élévation minimale en degrés (0 = horizon géométrique).
    Retourne list[dict] : risetime, duration, risetime_fmt, duration_min, max_el.
    """
    import datetime as _dt_sgp4
    now    = time.time()
    t_end  = now + hours_ahead * 3600.0
    step   = 20.0      # pas de propagation (s) — grossier pour la détection
    passes = []

    in_pass  = False
    rise_t   = 0.0
    max_el   = 0.0
    t        = now

    while t < t_end and len(passes) < n:
        try:
            lat, lon, alt = _sgp4_propagate(tle, t)
            el = _sgp4_elevation(lat, lon, alt, obs_lat, obs_lon)
        except Exception:
            t += step
            continue

        if el > horizon:
            if not in_pass:
                in_pass = True
                rise_t  = t
                max_el  = el
            else:
                if el > max_el:
                    max_el = el
        else:
            if in_pass:
                in_pass  = False
                set_t    = t

                # Affiner le début de passage par bisection (6 itérations → ±0.3 s)
                lo, hi = rise_t - step, rise_t
                for _ in range(6):
                    mid = (lo + hi) / 2.0
                    try:
                        lt2, ln2, al2 = _sgp4_propagate(tle, mid)
                        em = _sgp4_elevation(lt2, ln2, al2, obs_lat, obs_lon)
                    except Exception:
                        break
                    if em > horizon:
                        hi = mid
                    else:
                        lo = mid
                rise_ref = (lo + hi) / 2.0
                duration = set_t - rise_ref

                # Azimuts au lever et au coucher
                try:
                    rl, rn, ra = _sgp4_propagate(tle, rise_ref)
                    rise_az = round(_sgp4_azimuth(rl, rn, obs_lat, obs_lon), 1)
                except Exception:
                    rise_az = None
                try:
                    sl2, sn2, sa2 = _sgp4_propagate(tle, rise_ref + duration)
                    set_az = round(_sgp4_azimuth(sl2, sn2, obs_lat, obs_lon), 1)
                except Exception:
                    set_az = None

                dt_obj  = _dt_sgp4.datetime.fromtimestamp(rise_ref)
                set_ts  = rise_ref + duration
                set_obj = _dt_sgp4.datetime.fromtimestamp(set_ts)
                passes.append({
                    "risetime":     int(rise_ref),
                    "duration":     int(duration),
                    "risetime_fmt": dt_obj.strftime("%d/%m %H:%M"),
                    "set_fmt":      set_obj.strftime("%H:%M"),
                    "duration_min": round(duration / 60.0, 1),
                    "max_el":       round(max_el, 1),
                    "rise_az":      rise_az,
                    "set_az":       set_az,
                })
                max_el = 0.0   # ← reset pour le passage suivant
        t += step

    # Flush d'un passage qui serait encore ouvert en fin de fenêtre temporelle
    if in_pass and len(passes) < n:
        set_t    = t_end
        rise_ref = rise_t
        duration = set_t - rise_ref
        try:
            dt_obj  = _dt_sgp4.datetime.fromtimestamp(rise_ref)
            set_obj = _dt_sgp4.datetime.fromtimestamp(set_t)
            try:
                rl, rn, ra = _sgp4_propagate(tle, rise_ref)
                rise_az_f = round(_sgp4_azimuth(rl, rn, obs_lat, obs_lon), 1)
            except Exception:
                rise_az_f = None
            passes.append({
                "risetime":     int(rise_ref),
                "duration":     int(duration),
                "risetime_fmt": dt_obj.strftime("%d/%m %H:%M"),
                "set_fmt":      set_obj.strftime("%H:%M"),
                "duration_min": round(duration / 60.0, 1),
                "max_el":       round(max_el, 1),
                "rise_az":      rise_az_f,
                "set_az":       None,
            })
        except Exception:
            pass

    return passes


def _sgp4_parse_tle_text(txt):
    """Extrait line1 + line2 ISS (NORAD 25544) depuis un bloc texte TLE brut."""
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    # Cherche d'abord la ligne TLE ISS explicite
    for i, line in enumerate(lines):
        if (line.startswith("1 25544") or
                (line.startswith("1 ") and len(line) >= 69 and "25544" in line[2:8])):
            if i + 1 < len(lines) and lines[i+1].startswith("2 25544"):
                return line, lines[i+1]
    # Fallback : deux lignes TLE consécutives quelconques (fichier mono-sat)
    for i in range(len(lines) - 1):
        if (lines[i].startswith("1 ") and lines[i+1].startswith("2 ")
                and len(lines[i]) >= 69 and len(lines[i+1]) >= 69):
            return lines[i], lines[i+1]
    return None, None


def _sgp4_parse_satnogs_json(txt):
    """Extrait line1+line2 depuis la réponse JSON de l'API SatNOGS."""
    import json as _jsgp4
    try:
        data = _jsgp4.loads(txt)
        if isinstance(data, list) and data:
            entry = data[0]
            return entry.get("tle1"), entry.get("tle2")
        if isinstance(data, dict):
            return data.get("tle1"), data.get("tle2")
    except Exception:
        pass
    return None, None


def _sgp4_save_cache(line1, line2):
    try:
        with open(_SGP4_TLE_CACHE, "w") as _f:
            _f.write(line1 + "\n" + line2 + "\n")
    except Exception:
        pass


def _sgp4_load_cache():
    """Charge le TLE depuis le cache disque. Retourne (l1, l2) ou (None, None)."""
    try:
        if not os.path.exists(_SGP4_TLE_CACHE):
            return None, None
        mtime = os.path.getmtime(_SGP4_TLE_CACHE)
        # Cache valide 7 jours maximum
        if time.time() - mtime > 7 * 86400:
            return None, None
        with open(_SGP4_TLE_CACHE) as _f:
            lines = [l.strip() for l in _f.readlines() if l.strip()]
        if len(lines) >= 2:
            return lines[0], lines[1]
    except Exception:
        pass
    return None, None


def _sgp4_fetch_tle():
    """
    Récupère les TLE ISS depuis les sources distantes (multi-source avec fallback).
    Retourne (line1, line2) ou le TLE embarqué si tout échoue.
    """
    import urllib.request as _ureq_sgp4

    now = time.time()
    # Cache mémoire encore valide ?
    if (_iss_tle_memory[2] and
            now - _iss_tle_memory[2] < _ISS_TLE_TTL and
            _iss_tle_memory[0]):
        return _iss_tle_memory[0], _iss_tle_memory[1]

    # Tentative réseau — essayer chaque source dans l'ordre
    for url in _SGP4_TLE_SOURCES:
        try:
            req = _ureq_sgp4.Request(
                url,
                headers={"User-Agent": "Py-APRS/2.2 (+https://github.com/LesF4)"}
            )
            with _ureq_sgp4.urlopen(req, timeout=8) as resp:
                raw = resp.read().decode("utf-8", errors="replace")

            # Parser selon la source
            if "satnogs" in url or url.endswith("json"):
                l1, l2 = _sgp4_parse_satnogs_json(raw)
            else:
                l1, l2 = _sgp4_parse_tle_text(raw)

            if l1 and l2 and len(l1) >= 69 and len(l2) >= 69:
                _iss_tle_memory[0] = l1
                _iss_tle_memory[1] = l2
                _iss_tle_memory[2] = now
                _sgp4_save_cache(l1, l2)
                logger.info("[ISS] TLE ISS rechargé depuis %s", url)
                return l1, l2

        except Exception as e:
            logger.debug("[ISS] Source TLE %s : %s", url, e)
            continue

    # Fallback cache disque
    l1, l2 = _sgp4_load_cache()
    if l1 and l2:
        logger.warning("[ISS] TLE chargé depuis le cache disque (réseau indisponible)")
        _iss_tle_memory[0] = l1
        _iss_tle_memory[1] = l2
        _iss_tle_memory[2] = now - _ISS_TLE_TTL + 300  # forcer rechargement dans 5 min
        return l1, l2

    # Dernière ressource : TLE embarqué
    logger.warning("[ISS] Utilisation du TLE de secours embarqué (peut être périmé)")
    return _SGP4_FALLBACK_TLE

def _fetch_iss_passes(lat, lon, n=5):
    """
    Calcule les n prochains passages ISS via SGP4 (stdlib pure).
    Remplace l'appel à open-notify.org (hors-service depuis 2025).
    Retourne une liste de dicts : {risetime, duration, risetime_fmt,
                                   duration_min, max_el}.
    """
    try:
        line1, line2 = _sgp4_fetch_tle()
        tle = _sgp4_parse_tle(line1, line2)
        passes = _sgp4_compute_passes(tle, lat, lon, n=n)
        logger.debug("[ISS] %d passage(s) calculé(s) via SGP4", len(passes))
        return passes
    except Exception as e:
        logger.error("[ISS] Erreur calcul SGP4 : %s", e)
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


# Cache journalier des passages ISS — calculé en arrière-plan à minuit et au démarrage
_iss_today_cache: dict = {}   # {"date": "YYYY-MM-DD", "data": [pass, ...]}


def _iss_rebuild_today_cache():
    """Calcule le cache de tous les passages ISS du jour courant (appel non-bloquant)."""
    import datetime as _dt_c, time as _t_c
    lat, lon = _iss_get_latlon()
    if lat is None:
        return
    today      = _dt_c.datetime.now().date()
    now_ts     = _t_c.time()
    midnight_end = _dt_c.datetime.combine(
        today + _dt_c.timedelta(days=1), _dt_c.time.min).timestamp()
    hours_remaining = max(1.0, (midnight_end - now_ts) / 3600.0 + 0.5)
    try:
        line1, line2 = _sgp4_fetch_tle()
        tle          = _sgp4_parse_tle(line1, line2)
        passes       = _sgp4_compute_passes(tle, lat, lon, n=10,
                                            hours_ahead=hours_remaining)
        # Mise à jour atomique (remplace le dict d'un coup pour éviter la race condition)
        _iss_today_cache.clear()
        _iss_today_cache.update({"data": passes, "date": str(today)})
        logger.info("[ISS] Cache journalier mis a jour : %d passage(s) le %s",
                    len(passes), today)
    except Exception as _e_cache:
        logger.error("[ISS] Erreur cache journalier : %s", _e_cache)


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
                    logger.info("[ISS] Alerte passage dans %.1f min (début %s, durée %s min)",
                        delta / 60.0, p["risetime_fmt"], p["duration_min"])
        except Exception as _e:
            logger.error("[ISS] Erreur worker : %s", _e)

threading.Thread(target=_iss_pass_worker, daemon=True).start()
# Pré-calculer le cache journalier ISS au démarrage (thread pour ne pas bloquer Flask)
threading.Thread(target=_iss_rebuild_today_cache, daemon=True).start()


@app.route('/iss_passes')
@_login_required
def iss_passes():
    """Retourne les passages ISS du jour (?range=today, défaut) ou sur 24h glissantes (?range=24h)."""
    import json as _j, time as _t, datetime as _dt
    lat, lon = _iss_get_latlon()
    if lat is None:
        return _j.dumps({"error": "Position non configurée", "passes": []}), 200, \
               {'Content-Type': 'application/json'}

    range_mode = request.args.get("range", "today")   # "today" | "24h"
    now_ts = _t.time()
    today  = _dt.datetime.now().date()
    midnight_start = _dt.datetime.combine(today,                         _dt.time.min).timestamp()
    midnight_end   = _dt.datetime.combine(today + _dt.timedelta(days=1), _dt.time.min).timestamp()

    if range_mode == "24h":
        # ── Mode 24h glissantes : calcul direct, cache séparé ────────────────
        cached24     = _iss_today_cache.get("data24")
        cached24_ts  = _iss_today_cache.get("ts24", 0)
        # Invalider toutes les 15 min
        if cached24 is None or (now_ts - cached24_ts) > 900:
            try:
                line1, line2 = _sgp4_fetch_tle()
                tle          = _sgp4_parse_tle(line1, line2)
                cached24     = _sgp4_compute_passes(tle, lat, lon, n=20, hours_ahead=24.0)
                _iss_today_cache.update({"data24": cached24, "ts24": now_ts})
            except Exception as _e24:
                logger.error("[ISS] Erreur SGP4 24h : %s", _e24)
                cached24 = []
        passes_list = cached24

    else:
        # ── Mode jour courant (défaut) ────────────────────────────────────────
        cached      = _iss_today_cache.get("data")
        cached_date = _iss_today_cache.get("date")
        if cached is None or cached_date != str(today):
            hours_remaining = max(1.0, (midnight_end - now_ts) / 3600.0 + 1.0)
            try:
                line1, line2  = _sgp4_fetch_tle()
                tle           = _sgp4_parse_tle(line1, line2)
                cached        = _sgp4_compute_passes(tle, lat, lon, n=10,
                                                     hours_ahead=hours_remaining)
                _iss_today_cache.clear()
                _iss_today_cache.update({"data": cached, "date": str(today)})
            except Exception as e:
                logger.error("[ISS] Erreur calcul SGP4 : %s", e)
                cached = []
        passes_list = [p for p in cached
                       if midnight_start <= p["risetime"] < midnight_end]
        # Fallback : si aucun passage aujourd'hui (fin de soirée), prendre les 3 prochains
        if not passes_list:
            passes_list = [p for p in cached if p["risetime"] >= now_ts][:3]

    result = []
    for p in passes_list:
        q = dict(p)
        q["in_min"] = round((p["risetime"] - now_ts) / 60.0, 0)
        result.append(q)

    logger.debug("[ISS] %d passage(s) retournés (mode=%s)", len(result), range_mode)
    return _j.dumps({"passes": result, "lat": lat, "lon": lon,
                     "today": str(today), "range": range_mode}), 200, \
           {'Content-Type': 'application/json'}


@app.route('/iss_now')
@_login_required
def iss_now():
    """Position ISS temps réel : lat, lon, alt, élévation, azimut, distance, Doppler 145.825 MHz."""
    import json as _j, time as _t, math as _m
    obs_lat, obs_lon = _iss_get_latlon()
    if obs_lat is None:
        return _j.dumps({"error": "Position non configurée"}), 200, \
               {'Content-Type': 'application/json'}
    try:
        line1, line2 = _sgp4_fetch_tle()
        tle          = _sgp4_parse_tle(line1, line2)
        now          = _t.time()
        lat, lon, alt = _sgp4_propagate(tle, now)
        el           = _sgp4_elevation(lat, lon, alt, obs_lat, obs_lon)

        # ── Azimut (direction depuis l'observateur vers l'ISS) ────────────────
        slat = _m.radians(lat);       slon = _m.radians(lon)
        olat = _m.radians(obs_lat);   olon = _m.radians(obs_lon)
        dlon = slon - olon
        y_az = _m.sin(dlon) * _m.cos(slat)
        x_az = (_m.cos(olat) * _m.sin(slat)
                - _m.sin(olat) * _m.cos(slat) * _m.cos(dlon))
        az   = (_m.degrees(_m.atan2(y_az, x_az)) + 360) % 360

        # ── Distance slant (km) ───────────────────────────────────────────────
        r_sat = _SGP4_EARTH_R + alt
        sx = r_sat * _m.cos(slat) * _m.cos(slon)
        sy = r_sat * _m.cos(slat) * _m.sin(slon)
        sz = r_sat * _m.sin(slat)
        ox = _SGP4_EARTH_R * _m.cos(olat) * _m.cos(olon)
        oy = _SGP4_EARTH_R * _m.cos(olat) * _m.sin(olon)
        oz = _SGP4_EARTH_R * _m.sin(olat)
        slant_km = _m.sqrt((sx-ox)**2 + (sy-oy)**2 + (sz-oz)**2)

        # ── Doppler sur 145.825 MHz (vitesse radiale sur 2 s) ─────────────────
        dt_dop         = 2.0
        lat2, lon2, alt2 = _sgp4_propagate(tle, now + dt_dop)
        r2    = _SGP4_EARTH_R + alt2
        slat2 = _m.radians(lat2); slon2 = _m.radians(lon2)
        sx2 = r2 * _m.cos(slat2) * _m.cos(slon2)
        sy2 = r2 * _m.cos(slat2) * _m.sin(slon2)
        sz2 = r2 * _m.sin(slat2)
        slant2  = _m.sqrt((sx2-ox)**2 + (sy2-oy)**2 + (sz2-oz)**2)
        v_rad   = (slant2 - slant_km) / dt_dop          # km/s (+ = s'éloigne)
        C       = 299792.458                              # km/s
        F0      = 145.825e6                               # Hz APRS ISS
        doppler = -round(v_rad / C * F0)                 # Hz (négatif = approche)
        freq_rx = round((F0 - v_rad / C * F0) / 1000, 3) # kHz corrigée

        visible = el > 0.0

        return _j.dumps({
            "lat": round(lat, 4), "lon": round(lon, 4), "alt_km": round(alt, 1),
            "elevation": round(el, 1), "azimuth": round(az, 1),
            "slant_km":  round(slant_km, 1),
            "doppler_hz": int(doppler), "freq_rx_khz": freq_rx,
            "visible": visible, "obs_lat": obs_lat, "obs_lon": obs_lon,
        }), 200, {'Content-Type': 'application/json'}
    except Exception as _e:
        return _j.dumps({"error": str(_e)}), 200, {'Content-Type': 'application/json'}


@app.route('/iss_alert_config', methods=['GET', 'POST'])
@_login_required
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


# ══════════════════════════════════════════════════════════════════════════════
# ── Alertes météo — WEATHER_ALERT_PATCH ──────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
#
# Worker thread interrogeant Open-Meteo toutes les N minutes et diffusant
# un événement SSE "weather_alert" aux clients connectés en cas de dépassement
# de seuil.  Modèle identique à _iss_pass_worker.
# ──────────────────────────────────────────────────────────────────────────────

# Codes WMO considérés comme sévères (pluie forte, orages…)
_WX_SEVERE_CODES = {65, 75, 82, 95, 96, 99}

# Timestamps des dernières alertes envoyées (évite le spam)
_wx_last_alert: dict = {}     # clé = type d'alerte, valeur = timestamp float


def _wx_should_alert(key: str, cooldown_s: float = 1800.0) -> bool:
    """Retourne True si l'alerte (key) n'a pas encore été envoyée dans le délai cooldown."""
    import time as _t
    now = _t.time()
    if now - _wx_last_alert.get(key, 0) >= cooldown_s:
        _wx_last_alert[key] = now
        return True
    return False


def _wx_check_and_broadcast(force_test: bool = False):
    """
    Interroge Open-Meteo, compare aux seuils configurés et diffuse les
    alertes actives via la queue SSE et le bulletin APRS.
    force_test=True : ignore l'état "enabled" et garantit au moins une
    alerte (bouton "Tester les alertes maintenant"), même hors seuils.
    """
    import time as _t

    cfg_alert = config_manager.data.get("weather_alert", {})
    if not cfg_alert.get("enabled") and not force_test:
        return

    # Position station
    from_locator = config_manager.data.get("geo_mode", "locator") == "locator"
    if from_locator:
        maidenhead = config_manager.data.get("maidenhead", "")
        lat, lon = _grid_to_latlon(maidenhead) if maidenhead else (None, None)  # WX_FIX_MAIDENHEAD_v1
    else:
        lat = _safe_float(config_manager.data.get("lat_manual"))
        lon = _safe_float(config_manager.data.get("lon_manual"))

    if lat is None or lon is None:
        if not force_test:
            logger.warning("[WX-ALERT] Position non configurée — alerte ignorée")
            return
        wx = {}
    else:
        wx = _fetch_openmeteo(lat, lon) or {}
        if not wx and not force_test:
            return

    alerts = []

    # ── Température max ───────────────────────────────────────────────────────
    temp = wx.get("temp_c")
    t_max = float(cfg_alert.get("temp_max", 38.0))
    if temp is not None and temp >= t_max and _wx_should_alert("temp_max"):
        alerts.append({
            "key": "temp_max",
            "icon": "🌡️",
            "title": f"Chaleur extreme : {temp:.1f} C",  # WX_ALERT_NO_ACCENT_v1
            "detail": f"Seuil configuré : {t_max:.0f} °C",
            "color_border": "#ef4444",
            "color_bg": "#450a0a",
            "color_text": "#fca5a5",
        })

    # ── Température min (gel) ─────────────────────────────────────────────────
    t_min = float(cfg_alert.get("temp_min", -5.0))
    if temp is not None and temp <= t_min and _wx_should_alert("temp_min"):
        alerts.append({
            "key": "temp_min",
            "icon": "🧊",
            "title": f"Gel : {temp:.1f} C",  # WX_ALERT_NO_ACCENT_v1
            "detail": f"Seuil configuré : {t_min:.0f} °C",
            "color_border": "#38bdf8",
            "color_bg": "#0c2a3a",
            "color_text": "#7dd3fc",
        })

    # ── Vent moyen ────────────────────────────────────────────────────────────
    wind_ms = wx.get("wind_speed_ms")
    w_max = float(cfg_alert.get("wind_max", 60.0))
    if wind_ms is not None:
        wind_kmh = wind_ms * 3.6
        if wind_kmh >= w_max and _wx_should_alert("wind_max"):
            alerts.append({
                "key": "wind_max",
                "icon": "💨",
                "title": f"Vent fort : {wind_kmh:.0f} km/h",  # WX_ALERT_NO_ACCENT_v1
                "detail": f"Seuil : {w_max:.0f} km/h",
                "color_border": "#f59e0b",
                "color_bg": "#2d1b00",
                "color_text": "#fcd34d",
            })

    # ── Rafales ──────────────────────────────────────────────────────────────
    gust_ms = wx.get("gust_ms")
    g_max = float(cfg_alert.get("gust_max", 80.0))
    if gust_ms is not None:
        gust_kmh = gust_ms * 3.6
        if gust_kmh >= g_max and _wx_should_alert("gust_max"):
            alerts.append({
                "key": "gust_max",
                "icon": "🌪️",
                "title": f"Rafales : {gust_kmh:.0f} km/h",  # WX_ALERT_NO_ACCENT_v1
                "detail": f"Seuil : {g_max:.0f} km/h",
                "color_border": "#fb923c",
                "color_bg": "#3a1200",
                "color_text": "#fdba74",
            })

    # ── Précipitations ───────────────────────────────────────────────────────
    rain_mm = wx.get("rain_mm", 0) or 0
    r_max = float(cfg_alert.get("rain_mm", 10.0))
    if rain_mm >= r_max and _wx_should_alert("rain_mm"):
        alerts.append({
            "key": "rain_mm",
            "icon": "🌧️",
            "title": f"Pluie intense : {rain_mm:.1f} mm",  # WX_ALERT_NO_ACCENT_v1
            "detail": f"Seuil : {r_max:.0f} mm",
            "color_border": "#818cf8",
            "color_bg": "#1e1b4b",
            "color_text": "#c7d2fe",
        })

    # ── Code WMO sévère ──────────────────────────────────────────────────────
    wmo = wx.get("wmo_code", 0)
    if cfg_alert.get("wmo_severe", True) and wmo in _WX_SEVERE_CODES and _wx_should_alert(f"wmo_{wmo}"):
        alerts.append({
            "key": f"wmo_{wmo}",
            "icon": "⛈️",
            "title": f"Meteo severe : {wx.get('description', 'Alerte')}",  # WX_ALERT_NO_ACCENT_v1
            "detail": f"Code WMO {wmo}",
            "color_border": "#a21caf",
            "color_bg": "#3b0764",
            "color_text": "#e879f9",
        })

    # ── Test manuel — garantit un bulletin même hors seuils ────────────────────
    if force_test and not alerts:
        alerts.append({
            "key": "test_manuel",
            "icon": "🧪",
            "title": "Test bulletin météo (déclenché manuellement)",
            "detail": wx.get("description") or "Aucun seuil réel dépassé",
            "color_border": "#38bdf8",
            "color_bg": "#0c4a6e",
            "color_text": "#bae6fd",
        })

    # ── Diffusion SSE ────────────────────────────────────────────────────────
    for a in alerts:
        frame = {
            "type":         "weather_alert",
            "key":          a["key"],
            "icon":         a["icon"],
            "title":        a["title"],
            "detail":       a["detail"],
            "color_border": a["color_border"],
            "color_bg":     a["color_bg"],
            "color_text":   a["color_text"],
            "temp_c":       wx.get("temp_c"),
            "wind_kmh":     round(wind_ms * 3.6, 1) if wind_ms else None,
            "description":  wx.get("description"),
        }
        for _q in list(listeners):
            try: _q.put_nowait(frame)
            except: pass
        logger.warning("[WX-ALERT] %s — %s", a["key"], a["title"])

        # ── Bulletin APRS RF — WX_APRS_BULLETIN_PATCH_v1 ─────────────────
        _wx_send_aprs_bulletin(a["icon"], a["title"], a["detail"], alerts.index(a))



def _wx_send_aprs_bulletin(icon: str, title: str, detail: str, idx: int = 0):
    """
    Émet un bulletin APRS BLNWXn via Dire Wolf (KISS TCP).
    Format AX.25 : dest=APRS, payload=:BLNWXn   :<texte>
    WX_APRS_BULLETIN_PATCH_v1
    """
    try:
        global modem
        if modem is None:
            logger.warning("[WX-BLN] modem non initialisé — bulletin non envoyé")
            return

        cfg_alert = config_manager.data.get("weather_alert", {})
        if not cfg_alert.get("bulletin_aprs", True):
            logger.info("[WX-BLN] Bulletin APRS désactivé dans la config — non envoyé")
            return

        # Identifiant bulletin : BLNWX0 … BLNWX9 (9 chars, padded avec espaces)
        bln_id   = f"BLNWX{idx % 10}"
        bln_addr = bln_id.ljust(9)          # 9 caractères exactement

        # Texte : PAS d'emoji dans le payload RF — AX.25/KISS n'accepte que des
        # octets 0-255, un emoji (code point > 255) fait planter l'encodage en
        # bytes() et la trame est silencieusement perdue dans _tx_worker.
        # WX_ALERT_NO_ACCENT_v1 : le detail (seuil configuré) n'est plus inclus
        # dans le payload RF, uniquement le titre de l'alerte.
        text = f"{title}"
        # Sécurité supplémentaire : on retire tout caractère non Latin-1
        # (emoji résiduel, etc.) pour ne jamais faire planter send_packet().
        text = text.encode("latin-1", errors="ignore").decode("latin-1")
        if len(text) > 67:
            text = text[:67]

        payload = f":{bln_addr}:{text}"

        # Passe par tx_queue (thread dédié) pour éviter tout conflit de tx_lock
        # WX_ALERT_NO_ACCENT_v1 : aprs_type="Meteo" pour affichage avec le
        # badge/couleur météo dans le moniteur (au lieu du badge TX générique).
        try:
            tx_queue.put_nowait({
                "dest":      "APRS",
                "payload":   payload,
                "path":      "WIDE1-1",
                "aprs_type": "Meteo",
                "extra":     {"comment": text},
            })
            logger.info("[WX-BLN] Bulletin APRS mis en queue TX : %s", payload)
        except Exception as _qe:
            logger.error("[WX-BLN] Erreur mise en queue TX : %s", _qe)

    except Exception as _e:
        logger.error("[WX-BLN] Erreur émission bulletin : %s", _e)


def _weather_alert_worker():
    """Thread daemon : vérifie périodiquement les conditions météo et déclenche les alertes."""
    import time as _t
    while True:
        try:
            interval_min = float(
                config_manager.data.get("weather_alert", {}).get("interval_min", 30)
            )
            _t.sleep(max(5.0, interval_min) * 60)
        except Exception:
            _t.sleep(1800)
        try:
            _wx_check_and_broadcast()
        except Exception as _e:
            logger.error("[WX-ALERT] Erreur worker : %s", _e)


threading.Thread(target=_weather_alert_worker, daemon=True, name="wx-alert-worker").start()
logger.info("[WX-ALERT] Worker thread démarré")


@app.route('/weather_alert_config', methods=['GET', 'POST'])
@_login_required
def weather_alert_config():
    """GET → retourne la config alertes météo. POST → met à jour."""
    import json as _j
    if request.method == 'POST':
        data = request.get_json(force=True, silent=True) or {}
        wa = config_manager.data.get("weather_alert", {}).copy()
        _bool_keys  = ("enabled", "wmo_severe", "bulletin_aprs")
        _float_keys = ("temp_max", "temp_min", "wind_max", "gust_max", "rain_mm")
        _int_keys   = ("interval_min",)
        for k in _bool_keys:
            if k in data: wa[k] = bool(data[k])
        for k in _float_keys:
            if k in data:
                try: wa[k] = float(data[k])
                except (TypeError, ValueError): pass
        for k in _int_keys:
            if k in data:
                try: wa[k] = int(data[k])
                except (TypeError, ValueError): pass
        config_manager.data["weather_alert"] = wa
        with open("config.json", "w") as _f:
            _j.dump(config_manager.data, _f, ensure_ascii=False)
        logger.info("[WX-ALERT] Config mise à jour : %s", wa)
        return _j.dumps({"status": "ok", "weather_alert": wa}), 200,                {'Content-Type': 'application/json'}
    return _j.dumps(config_manager.data.get("weather_alert", {})), 200,            {'Content-Type': 'application/json'}


@app.route('/weather_alert_test', methods=['POST'])
@_login_required
def weather_alert_test():
    """Force une vérification météo immédiate ET garantit un bulletin de test
    (ignore l'état "enabled" et les seuils, pour vérifier la chaîne TX/APRS)."""
    import json as _j
    # Réinitialiser les cooldowns pour forcer le déclenchement
    _wx_last_alert.clear()
    try:
        _wx_check_and_broadcast(force_test=True)
        return _j.dumps({"status": "ok"}), 200, {'Content-Type': 'application/json'}
    except Exception as e:
        return _j.dumps({"status": "error", "error": str(e)}), 500,                {'Content-Type': 'application/json'}


# ── FIN WEATHER_ALERT_PATCH ───────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
# ── Alertes propagation / blackout ── PROP_ALERT_PATCH ───────────────────────
# Surveille SFI, Kp et éruptions X-ray GOES — SSE "prop_alert" aux clients
# ══════════════════════════════════════════════════════════════════════════════

# Horodatages des dernières alertes émises (anti-spam par type)
_prop_alerted_ts: dict = {}   # {"kp": float, "sfi": float, "xray": float}
_PROP_ALERT_COOLDOWN = 3600   # 1 heure entre deux alertes du même type

# Historique circulaire des alertes déclenchées (50 entrées max)
_prop_alerts_log: list = []
_prop_alerts_log_lock = threading.Lock()
_PROP_ALERTS_LOG_MAX  = 50


def _xray_class_value(label: str) -> float:
    """Convertit "M1.5" → valeur numérique brute (mantisse * puissance)."""
    label = label.strip().upper()
    if not label:
        return 0.0
    letter = label[0]
    try: mantissa = float(label[1:]) if len(label) > 1 else 1.0
    except ValueError: mantissa = 1.0
    exponents = {"A": 1e-8, "B": 1e-7, "C": 1e-6, "M": 1e-5, "X": 1e-4}
    return exponents.get(letter, 0) * mantissa


def _fetch_xray_latest():
    """
    Récupère la dernière mesure GOES X-ray 1-8 Å (flare) depuis NOAA SWPC.
    Retourne (class_str, flux_w_m2) ou (None, None).
    """
    import urllib.request, json as _j
    url = "https://services.swpc.noaa.gov/json/goes/primary/xrays-1-day.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Py-APRS/2.2"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = _j.loads(resp.read())
        # Filtrer canal long 0.1-0.8 nm (champ "energy" contient "0.1-0.8")
        longs = [e for e in data
                 if isinstance(e, dict) and "0.1-0.8" in str(e.get("energy", ""))]
        # Fallback : prendre uniquement la moitié haute du tableau (canal long en dernier)
        if not longs and isinstance(data, list) and len(data) >= 2:
            longs = data[len(data)//2:]
        last = longs[-1] if longs else ({} if not isinstance(data, list) else (data[-1] if data else {}))
        flux = last.get("flux") if isinstance(last, dict) else None
        if flux is None:
            return None, None
        flux = float(flux)
        if flux <= 0:
            return None, None   # valeur invalide (capteur hors ligne ou repos plat)
        # Convertir flux W/m² → classe
        if   flux >= 1e-4: cl = "X%.1f" % (flux / 1e-4)
        elif flux >= 1e-5: cl = "M%.1f" % (flux / 1e-5)
        elif flux >= 1e-6: cl = "C%.1f" % (flux / 1e-6)
        elif flux >= 1e-7: cl = "B%.1f" % (flux / 1e-7)
        else:              cl = "A%.1f" % (flux / 1e-8)
        return cl, flux
    except Exception as _e:
        logger.debug("[PROP-ALERT] X-ray fetch error: %s", _e)
        return None, None


def _prop_should_alert(key: str) -> bool:
    """Retourne True si le cooldown est écoulé pour ce type d'alerte."""
    import time as _t
    now = _t.time()
    last = _prop_alerted_ts.get(key, 0)
    if now - last >= _PROP_ALERT_COOLDOWN:
        _prop_alerted_ts[key] = now
        return True
    return False


def _prop_check_and_broadcast():
    """Évalue les indices et diffuse un SSE 'prop_alert' si seuil dépassé."""
    cfg = config_manager.data.get("prop_alert", {})
    if not cfg.get("enabled"):
        return

    data  = _fetch_solar_indices()
    kp    = data.get("k_index")
    sfi   = data.get("sfi")
    alerts = []

    # ── Tempête géomagnétique (Kp) ───────────────────────────────────────────
    kp_max = float(cfg.get("kp_max", 5.0))
    if kp is not None and kp >= kp_max and _prop_should_alert("kp"):
        alerts.append({
            "type":         "prop_alert",
            "key":          "kp_storm",
            "icon":         "🧲",
            "title":        f"Tempête géomagnétique — Kp {kp:.1f}",
            "detail":       f"Seuil : Kp ≥ {kp_max:.0f} · Aurora possible en VHF",
            "color_border": "#f59e0b",
            "color_bg":     "#2d1b00",
            "color_text":   "#fcd34d",
            "kp":           kp,
            "sfi":          sfi,
        })

    # ── Blackout HF (SFI bas) ────────────────────────────────────────────────
    sfi_min = float(cfg.get("sfi_min", 70.0))
    if sfi is not None and sfi < sfi_min and _prop_should_alert("sfi_low"):
        alerts.append({
            "type":         "prop_alert",
            "key":          "sfi_low",
            "icon":         "📉",
            "title":        f"Conditions HF dégradées — SFI {sfi:.0f}",
            "detail":       f"Seuil : SFI < {sfi_min:.0f} · Propagation HF mauvaise",
            "color_border": "#ef4444",
            "color_bg":     "#450a0a",
            "color_text":   "#fca5a5",
            "kp":           kp,
            "sfi":          sfi,
        })

    # ── Éruption solaire X-ray (GOES) ────────────────────────────────────────
    xray_threshold = cfg.get("xray_class", "M1")
    xray_cl, xray_flux = _fetch_xray_latest()
    if xray_cl and xray_flux is not None:
        threshold_val = _xray_class_value(xray_threshold)
        if xray_flux >= threshold_val and _prop_should_alert("xray"):
            alerts.append({
                "type":         "prop_alert",
                "key":          "xray_flare",
                "icon":         "☀️",
                "title":        f"Éruption solaire {xray_cl}",
                "detail":       f"Seuil : {xray_threshold} · Risque blackout HF dayside",
                "color_border": "#f97316",
                "color_bg":     "#3a1200",
                "color_text":   "#fdba74",
                "kp":           kp,
                "sfi":          sfi,
                "xray_class":   xray_cl,
            })

    # ── Diffusion SSE ────────────────────────────────────────────────────────
    for a in alerts:
        # Enregistrer dans l'historique circulaire
        import time as _ti
        entry = dict(a)
        entry["ts"] = _ti.time()
        with _prop_alerts_log_lock:
            _prop_alerts_log.append(entry)
            if len(_prop_alerts_log) > _PROP_ALERTS_LOG_MAX:
                del _prop_alerts_log[0]
        for _q in list(listeners):
            try: _q.put_nowait(a)
            except: pass
        logger.warning("[PROP-ALERT] %s — %s", a["key"], a["title"])


def _prop_alert_worker():
    """Thread daemon : vérifie périodiquement les indices de propagation."""
    import time as _t
    while True:
        try:
            interval_min = float(
                config_manager.data.get("prop_alert", {}).get("interval_min", 15)
            )
            _t.sleep(max(5.0, interval_min) * 60)
        except Exception:
            _t.sleep(900)
        try:
            _prop_check_and_broadcast()
        except Exception as _e:
            logger.error("[PROP-ALERT] Erreur worker : %s", _e)


threading.Thread(target=_prop_alert_worker, daemon=True, name="prop-alert-worker").start()
logger.info("[PROP-ALERT] Worker thread démarré")


@app.route('/prop_alert_config', methods=['GET', 'POST'])
@_login_required
def prop_alert_config():
    """GET → config alertes propagation. POST → mise à jour."""
    import json as _j
    if request.method == 'POST':
        data = request.get_json(force=True, silent=True) or {}
        pa = config_manager.data.get("prop_alert", {}).copy()
        if "enabled"      in data: pa["enabled"]      = bool(data["enabled"])
        if "interval_min" in data:
            try: pa["interval_min"] = int(data["interval_min"])
            except (TypeError, ValueError): pass
        if "kp_max"  in data:
            try: pa["kp_max"]  = float(data["kp_max"])
            except (TypeError, ValueError): pass
        if "sfi_min" in data:
            try: pa["sfi_min"] = float(data["sfi_min"])
            except (TypeError, ValueError): pass
        if "xray_class" in data: pa["xray_class"] = str(data["xray_class"])
        config_manager.data["prop_alert"] = pa
        with open("config.json", "w") as _f:
            _j.dump(config_manager.data, _f, ensure_ascii=False)
        logger.info("[PROP-ALERT] Config mise à jour : %s", pa)
        return _j.dumps({"status": "ok", "prop_alert": pa}), 200, \
               {'Content-Type': 'application/json'}
    return _j.dumps(config_manager.data.get("prop_alert", {})), 200, \
           {'Content-Type': 'application/json'}


@app.route('/prop_alert_test', methods=['POST'])
@_login_required
def prop_alert_test():
    """Force une vérification propagation immédiate (test des seuils)."""
    import json as _j
    # Réinitialiser les cooldowns pour forcer le déclenchement
    _prop_alerted_ts.clear()
    try:
        _prop_check_and_broadcast()
        return _j.dumps({"status": "ok"}), 200, {'Content-Type': 'application/json'}
    except Exception as e:
        return _j.dumps({"status": "error", "error": str(e)}), 500, \
               {'Content-Type': 'application/json'}


@app.route('/prop_alert_history')
@_login_required
def prop_alert_history():
    """Retourne les dernières alertes propagation déclenchées (log circulaire)."""
    import json as _j, time as _ti
    now = _ti.time()
    with _prop_alerts_log_lock:
        entries = list(reversed(_prop_alerts_log))   # plus récent en premier
    result = []
    for e in entries:
        result.append({
            "ts":           e.get("ts", 0),
            "ago_s":        int(now - e.get("ts", now)),
            "key":          e.get("key", ""),
            "icon":         e.get("icon", "📡"),
            "title":        e.get("title", ""),
            "detail":       e.get("detail", ""),
            "color_text":   e.get("color_text", "#94a3b8"),
            "color_border": e.get("color_border", "#475569"),
            "kp":           e.get("kp"),
            "sfi":          e.get("sfi"),
            "xray_class":   e.get("xray_class"),
        })
    return _j.dumps({"alerts": result}), 200, {'Content-Type': 'application/json'}


# ── FIN PROP_ALERT_PATCH ──────────────────────────────────────────────────────


@app.route('/prop_status')
@_login_required
def prop_status():
    """Retourne les indices de propagation courants + drapeaux d'alerte (sans cooldown)."""
    import json as _j
    cfg  = config_manager.data.get("prop_alert", {})
    data = _fetch_solar_indices()
    kp   = data.get("k_index")
    sfi  = data.get("sfi")
    xray_cl, xray_flux = _fetch_xray_latest()
    kp_max  = float(cfg.get("kp_max",  5.0))
    sfi_min = float(cfg.get("sfi_min", 70.0))
    xray_threshold = cfg.get("xray_class", "M1")
    threshold_val  = _xray_class_value(xray_threshold)
    result = {
        "kp":           round(kp,  2) if kp  is not None else None,
        "sfi":          round(sfi, 1) if sfi is not None else None,
        "xray_class":   xray_cl,
        "kp_alert":     bool(kp  is not None and kp  >= kp_max),
        "sfi_alert":    bool(sfi is not None and sfi <  sfi_min),
        "xray_alert":   bool(xray_flux is not None and xray_flux >= threshold_val),
        "kp_max":       kp_max,
        "sfi_min":      sfi_min,
        "xray_thr":     xray_threshold,
        "enabled":      bool(cfg.get("enabled")),
    }
    return _j.dumps(result), 200, {"Content-Type": "application/json"}



# ── WEATHER_HISTORY_PATCH_APPLIED ──
# Route GET /weather_history — historique Open-Meteo 48h (hourly)

@app.route('/weather_history')
@_login_required
def weather_history():
    """
    Retourne l'historique météo horaire des 48 dernières heures via Open-Meteo.
    Utilise les coordonnées de la station (config maidenhead ou lat/lon manuel).
    Réponse JSON : {temperature_2m, wind_speed_10m, precipitation, location}
    """
    import urllib.request, json as _j

    # ── Coordonnées station ───────────────────────────────────────────────────
    cfg = config_manager.data
    lat, lon = None, None
    geo_mode = cfg.get("geo_mode", "locator")
    if geo_mode == "coords":
        try:
            lat = float(cfg.get("lat_manual", ""))
            lon = float(cfg.get("lon_manual", ""))
        except (TypeError, ValueError):
            lat = lon = None
    if lat is None or lon is None:
        mh = cfg.get("maidenhead", "")
        if mh and len(mh) >= 4:
            try:
                g = mh.upper()
                lon = (ord(g[0]) - 65) * 20 - 180
                lat = (ord(g[1]) - 65) * 10 - 90
                lon += int(g[2]) * 2
                lat += int(g[3]) * 1
                if len(g) >= 6:
                    lon += (ord(g[4]) - 65) * (2/24) + (1/24)
                    lat += (ord(g[5]) - 65) * (1/24) + (0.5/24)
                else:
                    lon += 1; lat += 0.5
            except Exception:
                lat = lon = None

    if lat is None or lon is None:
        return _j.dumps({"error": "Coordonnées station non configurées"}), 400,                {'Content-Type': 'application/json'}

    # ── Cache memoire 10 min ──────────────────────────────────────────────────
    _hck = "%.4f,%.4f" % (lat, lon)
    _hcached = _WX_HISTORY_CACHE.get(_hck)
    if _hcached and (time.time() - _hcached["ts"]) < _WX_CACHE_TTL_S:
        return _j.dumps(_hcached["payload"]), 200, {'Content-Type': 'application/json'}

    from datetime import datetime
    from concurrent.futures import ThreadPoolExecutor, as_completed

    url_fc = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=%.5f&longitude=%.5f"
        "&hourly=temperature_2m,wind_speed_10m,precipitation,wind_gusts_10m"
        "&past_days=2&forecast_days=1&wind_speed_unit=ms&timezone=auto"
    ) % (lat, lon)

    url_dwd = (
        "https://api.open-meteo.com/v1/dwd-icon"
        "?latitude=%.5f&longitude=%.5f"
        "&hourly=lightning_potential,cape"
        "&past_days=2&forecast_days=1&timezone=auto&models=icon_seamless"
    ) % (lat, lon)

    def _fetch(u):
        req = urllib.request.Request(u, headers={"User-Agent": "aprs_station/2.5"})
        with urllib.request.urlopen(req, timeout=6) as r:
            return _j.loads(r.read())

    raw = raw_dwd = None
    try:
        with ThreadPoolExecutor(max_workers=2) as _pool:
            fut_fc  = _pool.submit(_fetch, url_fc)
            fut_dwd = _pool.submit(_fetch, url_dwd)
            try:
                raw = fut_fc.result(timeout=7)
            except Exception as _e:
                logger.error("[WX-HIST] Open-Meteo forecast : %s", _e)
                return _j.dumps({"error": str(_e)}), 500, {'Content-Type': 'application/json'}
            try:
                raw_dwd = fut_dwd.result(timeout=7)
            except Exception as _e2:
                logger.warning("[WX-HIST] DWD ICON indisponible : %s", _e2)

        # ── Filtrage des 48 dernieres heures ──────────────────────────────────
        hourly = raw.get("hourly", {})
        times  = hourly.get("time", [])
        temp   = hourly.get("temperature_2m", [])
        wind   = hourly.get("wind_speed_10m", [])
        rain   = hourly.get("precipitation", [])
        gust   = hourly.get("wind_gusts_10m", [])
        cutoff = datetime.now().timestamp() - 48 * 3600

        t2, temp2, w2, r2, g2 = [], [], [], [], []
        for i, ts in enumerate(times):
            try:
                dt = datetime.strptime(ts.replace("T", " ")[:16], "%Y-%m-%d %H:%M")
                if dt.timestamp() >= cutoff:
                    t2.append(ts)
                    temp2.append(temp[i] if i < len(temp) else 0)
                    w2.append(wind[i]    if i < len(wind) else 0)
                    r2.append(rain[i]    if i < len(rain) else 0)
                    g2.append(gust[i]    if i < len(gust) else 0)
            except Exception:
                continue

        # ── DWD (facultatif) ──────────────────────────────────────────────────
        lpi2, cape2 = [], []
        if raw_dwd:
            h_dwd  = raw_dwd.get("hourly", {})
            times_d = h_dwd.get("time", [])
            lpi_d   = h_dwd.get("lightning_potential", [])
            cape_d  = h_dwd.get("cape", [])
            for i2, ts2 in enumerate(times_d):
                try:
                    dt2 = datetime.strptime(ts2.replace("T", " ")[:16], "%Y-%m-%d %H:%M")
                    if dt2.timestamp() >= cutoff:
                        lpi2.append(lpi_d[i2]  if i2 < len(lpi_d)  else 0)
                        cape2.append(cape_d[i2] if i2 < len(cape_d) else 0)
                except Exception:
                    continue
            logger.debug("[WX-HIST] DWD LPI: %d points", len(lpi2))

        tz_info  = raw.get("timezone_abbreviation", "")
        location = "%.4f, %.4f %s" % (lat, lon, tz_info)
        result = {
            "temperature_2m": temp2, "wind_speed_10m": w2,
            "precipitation": r2,     "wind_gusts_10m": g2,
            "lightning_potential": lpi2, "cape": cape2,
            "location": location, "lat": lat, "lon": lon,
        }
        _WX_HISTORY_CACHE[_hck] = {"payload": result, "ts": time.time()}
        return _j.dumps(result), 200, {'Content-Type': 'application/json'}

    except Exception as e:
        logger.error("[WX-HIST] Erreur : %s", e)
        if _hcached:
            return _j.dumps(_hcached["payload"]), 200, {'Content-Type': 'application/json'}
        return _j.dumps({"error": str(e)}), 500, {'Content-Type': 'application/json'}

# ── SIGNAL_PATH_PATCH_APPLIED ──
# ── APRSFI_CONFIG_PATCH_APPLIED ──
# ── LIGHTNING_PATCH_APPLIED ──
# ── FIN WEATHER_HISTORY_PATCH ─────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# Route GET /signal_path — chemins réseau de ma station (RF + IS + aprs.fi)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/signal_path')
@_login_required
def signal_path():
    """
    Analyse les trames de MA station dans rx_history (RF + IS)
    et interroge l'API aprs.fi si aprsfi_key est configurée.

    Retourne :
      callsign, last_path, digi_counts, history (20 dernières), rf_count, is_count
    """
    import json as _j, time as _t, urllib.request
    from datetime import datetime

    cfg      = config_manager.data
    my_call  = cfg.get("callsign", "").upper().strip()
    my_base  = my_call.split("-")[0]
    api_key  = cfg.get("aprsfi_key", "").strip()

    cutoff_24h = _t.time() - 86400   # 24 heures

    # ── helpers ───────────────────────────────────────────────────────────────

    def _ts_str(ts):
        try:
            return datetime.fromtimestamp(ts).strftime("%H:%M")
        except Exception:
            return "?"

    def _parse_path_nodes(path_str, source):
        """
        Retourne la liste ordonnée des noeuds du chemin APRS.

        RF  :  'F5ZKB*,WIDE2-1'           → ['F5ZKB*', 'WIDE2-1']
        IS  :  'WIDE1-1,WIDE2-1,qAR,F5XAJ-10' → idem, y compris qAR et iGate
        """
        if not path_str:
            return []
        return [p.strip() for p in path_str.split(',') if p.strip()]

    def _extract_digis(nodes):
        """Retourne la liste des vrais digis (marques * ou nom de station, hors WIDE/qA*)."""
        digis = []
        for n in nodes:
            upper = n.upper().rstrip('*')
            if upper.startswith('WIDE') or upper.startswith('TRACE') or upper.startswith('Q'):
                continue
            if n.endswith('*') or (not upper.startswith('WIDE') and '-' in n):
                digis.append(n.rstrip('*'))
            elif not upper.startswith('WIDE') and upper not in ('RELAY', 'ECHO', 'GATE'):
                # Callsign nu après qAR = iGate
                digis.append(n)
        return digis

    # ── Scan rx_history ───────────────────────────────────────────────────────

    paths_rf     = []   # {ts, nodes, source='RF'}
    paths_is     = []   # {ts, nodes, source='IS'}

    with rx_history_lock:
        snapshot = list(rx_history)

    for frame in snapshot:
        if frame.get("type") == "rx_level":
            continue
        # tx_event = trame émise par MA station : incluse dans le scan
        src = (frame.get("src") or "").upper().strip()
        # Accepter indicatif complet ET base (ex: F1RIQ et F1RIQ-9)
        if src not in (my_call, my_base) and not src.startswith(my_base + "-"):
            continue
        path_str = frame.get("path", "")
        if not path_str:
            continue

        ts      = frame.get("_ts", _t.time())
        source  = "IS" if frame.get("_source") == "IS" else "RF"
        nodes   = _parse_path_nodes(path_str, source)
        if not nodes:
            continue

        entry = {"ts": ts, "nodes": nodes, "source": source,
                 "time_str": _ts_str(ts)}

        if source == "IS":
            paths_is.append(entry)
        else:
            paths_rf.append(entry)

    # ── API aprs.fi (optionnel) ───────────────────────────────────────────────

    paths_aprsfi = []
    if api_key:
        try:
            url_fi = (
                "https://api.aprs.fi/api/get"
                "?name=%s&what=loc&apikey=%s&format=json&timerange=86400"
            ) % (my_call, api_key)
            req_fi = urllib.request.Request(
                url_fi, headers={"User-Agent": "aprs_station/2.5"})
            with urllib.request.urlopen(req_fi, timeout=8) as r:
                fi_data = _j.loads(r.read())
            for entry in fi_data.get("entries", []):
                path_str = entry.get("path", "")
                ts_fi    = int(entry.get("time", 0))
                if not path_str or ts_fi < cutoff_24h:
                    continue
                nodes = _parse_path_nodes(path_str, "aprsfi")
                if nodes:
                    paths_aprsfi.append({
                        "ts": ts_fi, "nodes": nodes, "source": "aprsfi",
                        "time_str": _ts_str(ts_fi),
                    })
            logger.debug("[SIGPATH] aprs.fi : %d entrées récupérées", len(paths_aprsfi))
        except Exception as e_fi:
            logger.warning("[SIGPATH] aprs.fi indisponible : %s", e_fi)

    # ── Fusion + tri chronologique ────────────────────────────────────────────

    all_paths = sorted(paths_rf + paths_is + paths_aprsfi,
                       key=lambda x: x["ts"], reverse=True)

    # ── Comptage par digi ─────────────────────────────────────────────────────

    digi_counts = {}   # digi_upper → {count, source, name}
    for p in all_paths:
        if p["ts"] < cutoff_24h:
            continue
        for digi in _extract_digis(p["nodes"]):
            key = digi.upper()
            if key not in digi_counts:
                digi_counts[key] = {"digi": digi, "count": 0, "source": p["source"]}
            digi_counts[key]["count"] += 1

    digi_list = sorted(digi_counts.values(), key=lambda x: x["count"], reverse=True)

    # ── Résultat ──────────────────────────────────────────────────────────────

    result = {
        "callsign":    my_call,
        "last_path":   all_paths[0] if all_paths else None,
        "digi_counts": digi_list,
        "history":     all_paths[:20],
        "rf_count":    len(paths_rf),
        "is_count":    len(paths_is),
        "aprsfi_count": len(paths_aprsfi),
    }
    return _j.dumps(result, default=str), 200, {'Content-Type': 'application/json'}

# ── FIN SIGNAL_PATH_PATCH ─────────────────────────────────────────────────────

def _safe_float(v):
    try:
        return float(v) if v not in (None, '', 'None') else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════





# ══════════════════════════════════════════════════════════════════════════════
# [SSTV RX supprimé — voir patch_sstv_remove.py]


# ══════════════════════════════════════════════════════════════════════════════
# Fermeture propre (Graceful Shutdown)
# ══════════════════════════════════════════════════════════════════════════════
#
# Séquence d'arrêt garantie lors d'un SIGTERM/SIGINT ou d'un arrêt Flask :
#   1. Signaler la fin aux threads de fond (flags + events)
#   2. Attendre la terminaison ordonnée du thread rx-decoder (Dire Wolf + socket KISS)
#   3. Fermer la socket KISS TX persistante
#   4. Fermer la connexion iGate APRS-IS
#   5. Sauvegarder chat.json de façon synchrone + atomique

import atexit as _atexit
import signal as _signal

# Durée max d'attente pour la fin du thread rx-decoder (en secondes)
_RX_THREAD_JOIN_TIMEOUT = 5

# Référence vers le thread rx-decoder (démarré dans APRSModem.start_rx)
_rx_thread_ref: list = []   # [Thread] – liste à un élément pour le rendre muable depuis l'extérieur


# Patch de start_rx pour capturer la référence du thread
_original_start_rx = APRSModem.start_rx

def _patched_start_rx(self):
    _original_start_rx(self)
    # Récupérer le thread nommé « rx-decoder » qui vient d'être démarré
    import threading as _thr
    for t in _thr.enumerate():
        if t.name == "rx-decoder" and t.is_alive():
            _rx_thread_ref.clear()
            _rx_thread_ref.append(t)
            break

APRSModem.start_rx = _patched_start_rx


def _graceful_shutdown():
    """
    Fonction de fermeture propre appelée par atexit et les handlers SIGTERM/SIGINT.
    Idempotente : protégée par un flag pour n'être exécutée qu'une seule fois.
    """
    import threading as _thr
    if getattr(_graceful_shutdown, "_done", False):
        return
    _graceful_shutdown._done = True

    logger.info("[SHUTDOWN] Fermeture propre en cours...")

    # ── 1. Arrêter le thread rx-decoder (Dire Wolf + socket KISS RX) ──────────
    global modem
    if modem is not None:
        modem.is_rx_running = False
        logger.info("[SHUTDOWN] Signal d'arrêt envoyé au thread rx-decoder")
    # Attendre la fin du thread rx-decoder
    if _rx_thread_ref:
        t = _rx_thread_ref[0]
        if t.is_alive():
            t.join(timeout=_RX_THREAD_JOIN_TIMEOUT)
            if t.is_alive():
                logger.warning("[SHUTDOWN] Thread rx-decoder n'a pas terminé dans le délai imparti")
            else:
                logger.info("[SHUTDOWN] Thread rx-decoder terminé proprement")

    # ── 2. Fermer la socket KISS TX persistante ───────────────────────────────
    with APRSModem._tx_sock_lock:
        if APRSModem._tx_sock is not None:
            try:
                import socket as _sock
                APRSModem._tx_sock.shutdown(_sock.SHUT_RDWR)
            except Exception:
                pass
            try:
                APRSModem._tx_sock.close()
            except Exception:
                pass
            APRSModem._tx_sock = None
            logger.info("[SHUTDOWN] Socket KISS TX fermée")

    # ── 3. Fermer la connexion iGate APRS-IS ──────────────────────────────────
    try:
        igate_client.stop()
        logger.info("[SHUTDOWN] iGate APRS-IS arrêté")
    except Exception as e:
        logger.error("[SHUTDOWN] Erreur arrêt iGate : %s", e)

    # ── 5. Sauvegarder chat.json de façon synchrone et atomique ──────────────
    try:
        with chat_manager._lock:
            chat_manager._save()
        logger.info("[SHUTDOWN] chat.json sauvegardé")
    except Exception as e:
        logger.error("[SHUTDOWN] Erreur sauvegarde chat.json : %s", e)

    # ── 6. Sauvegarder config.json ───────────────────────────────────────────
    try:
        _tmp_cfg = "config.json.tmp"
        with open(_tmp_cfg, "w", encoding="utf-8") as _f:
            json.dump(config_manager.data, _f, ensure_ascii=False, indent=2)
        os.replace(_tmp_cfg, "config.json")
        logger.info("[SHUTDOWN] config.json sauvegardé")
    except Exception as e:
        logger.error("[SHUTDOWN] Erreur sauvegarde config.json : %s", e)

    logger.info("[SHUTDOWN] Fermeture propre terminée. 73 !")


# ── Enregistrement atexit (exécuté à la fin du processus Python) ──────────────
_atexit.register(_graceful_shutdown)


# ── Handlers POSIX (SIGTERM = systemctl stop | SIGINT = Ctrl-C) ───────────────
def _signal_handler(signum, frame):
    sig_name = "SIGTERM" if signum == _signal.SIGTERM else "SIGINT"
    logger.info("[SHUTDOWN] Signal %s reçu — déclenchement de l'arrêt propre", sig_name)
    _graceful_shutdown()
    # Sortie propre après nettoyage
    raise SystemExit(0)


try:
    _signal.signal(_signal.SIGTERM, _signal_handler)
    _signal.signal(_signal.SIGINT,  _signal_handler)
except (OSError, ValueError):
    # Signal ne peut pas être intercepté dans certains contextes (thread non-principal)
    pass


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)

# PATCH_PROP_PILL_v1

# PATCH_NOTES_TAB_v1
