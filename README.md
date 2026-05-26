# 📡 Py-APRS

Station APRS web complète — interface Flask + backend Dire Wolf.

> Version 2.2 — Mai 2026  
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
- Balises de position, météo, ISS et propagation.
- Chat APRS en direct avec historique persistant.
- Carte APRS avec marqueurs et informations enrichies.
- Liens QRZ.com sur les indicatifs.
- iGate APRS-IS : RX-iGate et Full-iGate.
- Widget propagation VHF avec données NOAA.
- Passages ISS et alertes avant passage.
- Statistiques 24 h et carnet de trafic APRS.
- Export CSV / ADIF du carnet.
- Configuration persistante dans `config.json`.
- Interface mobile améliorée en V2.2.

---

## 📦 Dernière version

La dernière version publiée est **Py-APRS V2.2**.

Le script principal est disponible dans la section **Releases** du dépôt GitHub.

Fichier principal :

```text
aprs.py
```

---

## Prérequis

### Système

- Linux : Debian 11/12, Ubuntu 22.04/24.04, Raspberry Pi OS.
- Windows : Windows 10 ou Windows 11.
- Python 3.9 ou supérieur.

### Matériel

- Transceiver VHF/UHF relié à la carte son de l’ordinateur.
- Câble audio bidirectionnel.
- Commande PTT via port série RTS/DTR ou PTT désactivé pour les essais.
- Adaptateur USB-son ou carte son reconnue par le système.

### Logiciels

- Dire Wolf : TNC logiciel pour APRS/AX.25.
- Python 3.9+.
- Dépendances Python : `numpy`, `sounddevice`, `pyserial`, `crcmod`, `flask`.

---

## Installation rapide

### Linux / Raspberry Pi OS

```bash
sudo apt update
sudo apt upgrade -y

sudo apt install python3 python3-pip python3-venv -y
sudo apt install direwolf libportaudio2 portaudio19-dev -y
sudo usermod -aG dialout $USER

python3 -m venv venv_aprs
source venv_aprs/bin/activate

pip install numpy sounddevice pyserial crcmod flask

python3 aprs.py
```

### Windows 10 / 11

Installer d’abord :

- Python 3.9 ou supérieur depuis le site officiel Python.
- Dire Wolf pour Windows, puis l’ajouter au PATH si nécessaire.

Puis dans le dossier du fichier `aprs.py` :

```powershell
python -m venv venv_aprs
.\venv_aprs\Scripts\Activate.ps1

pip install numpy sounddevice pyserial crcmod flask

python aprs.py
```

Une fois lancé, ouvrir :

```text
http://localhost:5001
```

---

## ⚙️ Configuration

La configuration s’effectue depuis l’interface web.

Les paramètres sont sauvegardés automatiquement dans :

```text
config.json
```

Exemples de paramètres :

| Paramètre | Description |
|---|---|
| `callsign` | Indicatif de la station, par exemple `F4XXX-9` |
| `serial_port` | Port série PTT : `/dev/ttyUSB0` ou `COM3` |
| `ptt_mode` | Mode PTT : `RTS`, `DTR`, `RTS+DTR` ou `NONE` |
| `audio_device_tx` | Périphérique audio émission |
| `audio_device_rx` | Périphérique audio réception |
| `path` | Path APRS, par exemple `WIDE1-1,WIDE2-1` |
| `maidenhead` | Locator Maidenhead |
| `beacon_interval` | Intervalle de balise |

---

## Historique des versions

### Version 2.2 — Mai 2026

- Liens QRZ.com sur tous les indicatifs : console, QSO, MAP, statistiques.
- Navigation mobile avec barre fixe en bas d’écran.
- Interface mobile-first : viewport, zones tactiles 44 px, zoom iOS désactivé.
- Carte MAP avec hauteur dynamique sur smartphone.
- Suppression du bloc « Alerte Proximité ».
- Section Aide avec versioning et changelog intégrés.

### Version 2.1

- iGate APRS-IS : RX-iGate et Full-iGate avec reconnexion automatique.
- Alertes météo : température, vent, rafales, pluie, pression, codes WMO.
- Widget propagation VHF : SFI / Kp / A-index NOAA en temps réel.
- Passages ISS : widget compact dans TRAFIC + alerte avant passage.
- Web Push Notifications.
- Statistiques 24 h avec Top 10 stations.
- Persistance stats via `/stats/save` et `/stats/load`.

### Version 2.0

- Backend Dire Wolf via KISS/TCP.
- Interface web Flask complète.
- Carte MAP Leaflet.
- Chat APRS persistant.
- Beacons automatiques.
- Onglet ISS.
- Décodage Mic-E, PHG, RNG et positions compressées.
- Interface Tailwind CSS dark responsive.

---

## Fichiers à ne pas publier

Ne pas publier les fichiers personnels générés localement :

```text
config.json
chat.json
logbook.json
*.log
.venv/
venv_aprs/
__pycache__/
```

Ces fichiers peuvent contenir une configuration personnelle, un historique de trafic ou des données locales.

---

## Licence et usage

Logiciel gratuit développé dans l’esprit Ham Spirit.

- Utilisation personnelle autorisée.
- Distribution autorisée dans le respect des crédits.
- Modification autorisée en créditant l’auteur.
- L’opérateur reste responsable de ses émissions.

---

## Auteur

**F1RIQ Anthony** — Développeur principal  
Contact : `tonyf1riq@gmail.com`

---

## Maintenance du dépôt

Ce dépôt est maintenu par **F4MAJ / Les F4** au nom de la communauté Discord Les F4, afin de centraliser les développements logiciels de la communauté.

Site associé : `https://f4maj.fr`  
Discord : `https://discord.gg/u5Nqpu7ECM`

---

Bonnes balises APRS et bon trafic !

73 de F1RIQ Anthony
