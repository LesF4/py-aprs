# 📡 Py-APRS

![Downloads](https://img.shields.io/github/downloads/LesF4/py-aprs/total?label=DOWNLOADS&style=for-the-badge&color=brightgreen)

Station APRS web complète — interface Flask + backend Dire Wolf.

> Version 2.4 — Mai 2026  
> Développé par **F1RIQ Anthony**

---

## Le mot de l'auteur

Py-APRS est un serveur web Flask exposant une interface APRS complète : balises, météo, chat, carte, statistiques, carnet de trafic, propagation et diagnostics.

La modulation/démodulation audio et la gestion PTT sont déléguées à **Dire Wolf** via le protocole **KISS/TCP** sur le port `8001`.

Compatible **Windows 10/11** et **Linux** : Debian, Ubuntu, Raspberry Pi OS.

> 73, F1RIQ Anthony

---

## ✨ Fonctionnalités principales

- Émission/réception APRS complète en AFSK 1200 bauds via Dire Wolf.
- Interface web accessible depuis un navigateur : `http://localhost:5001`.
- Authentification intégrée par login/mot de passe.
- Balises de position, météo, ISS et propagation.
- Chat APRS en direct avec historique persistant et ACK automatique.
- Carte APRS avec marqueurs et informations enrichies.
- Liens QRZ.com sur les indicatifs.
- iGate APRS-IS : RX-iGate et Full-iGate.
- Widget propagation VHF/HF avec indices NOAA/SWPC.
- Passages ISS avec moteur SGP4 embarqué et cache TLE.
- Statistiques 24 h avec top stations.
- Interface responsive avec navigation mobile.
- Journalisation avec rotation automatique des logs.

---

## 🆕 Nouveautés V2.4 / V2.3

### Version 2.4

- Passage officiel en **version 2.4**.
- Mise à jour du changelog intégré.
- Interface d'aide et d'information cohérente avec la version 2.4.

### Version 2.3 intégrée dans cette publication

- Passages ISS avec moteur **SGP4 embarqué** en Python standard.
- Remplacement de l'ancien service `open-notify.org` devenu indisponible.
- Rechargement des TLE ISS toutes les 6 h depuis CelesTrak, avec sources de secours et cache disque hors-ligne.
- Élévation maximale affichée pour chaque passage ISS.
- Fermeture propre du programme via `SIGTERM`, `SIGINT` et `atexit`.
- Sauvegarde atomique de `chat.json` et `config.json` à l'arrêt.
- Fermeture ordonnée des sockets KISS TX et iGate.

---

## 🔐 Première connexion

Au premier lancement, Py-APRS crée automatiquement un compte local par défaut :

```text
Identifiant : admin
Mot de passe : aprs1200
```

⚠️ **Important : changez ce mot de passe dès la première connexion**, surtout si l'interface est accessible sur le réseau local ou à distance.

---

## 📦 Fichiers du dépôt

| Fichier | Description |
|---|---|
| `aprs.py` | Script principal Py-APRS |
| `README.md` | Présentation du projet |
| `RELEASE_NOTES_v2.4.md` | Notes de version V2.4 |

---

## ⚙️ Prérequis

- Python 3.9 ou supérieur.
- Dire Wolf installé et configuré.
- Modules Python nécessaires selon l'environnement : Flask, sounddevice, pyserial, crcmod, numpy, werkzeug.
- Interface audio compatible pour émission/réception APRS.

Exemple d'installation des dépendances Python :

```bash
pip install flask sounddevice pyserial crcmod numpy werkzeug
```

---

## 🚀 Lancement

```bash
python aprs.py
```

Puis ouvrir l'interface dans un navigateur :

```text
http://localhost:5001
```

---

## ⚠️ Responsabilité radioamateur

Ce logiciel est destiné aux radioamateurs titulaires d'une licence et aux SWL en réception uniquement.

L'utilisateur reste seul responsable de ses émissions, de sa configuration radio, du respect de la réglementation de son pays et de l'utilisation éventuelle d'un iGate APRS-IS.

---

## 🙏 Auteur

Développé par **F1RIQ Anthony**.  
Publication GitHub : **Les F4 Dev**.
