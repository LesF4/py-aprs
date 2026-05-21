# 📡 Py-APRS

[![Downloads](https://img.shields.io/github/downloads/LesF4/py-aprs/latest/total?style=for-the-badge&label=DOWNLOADS&color=brightgreen)](https://github.com/LesF4/py-aprs/releases)

**Station APRS web complète — interface Flask + modem Dire Wolf**

> 📻 Version 2.0 — Mai 2026
> Développé par **F1RIQ Anthony**

---

## 💬 Le mot de l'auteur

Py-APRS est un serveur web Flask exposant une **interface APRS complète** (balises, météo, chat, propagation). La modulation/démodulation audio et la gestion PTT sont entièrement déléguées à **Dire Wolf** via le protocole KISS/TCP sur le port 8001.

Compatible **Windows 10/11** et **Linux** (Debian / Ubuntu / Raspberry Pi OS).

> **73, F1RIQ Anthony**

---

## ✨ Fonctionnalités principales

- 📡 **Émission/réception APRS** complète (AFSK 1200 Bd via Dire Wolf)
- 🌐 **Interface web** accessible depuis un navigateur (http://localhost:5001)
- 📍 **Balises de position** (mode manuel ou via locator Maidenhead)
- 🌤️ **Balises météo** intégrées
- 💬 **Chat APRS** en direct
- 📊 **Propagation** et diagnostics RX
- ⚙️ **Configuration persistante** dans `config.json`
- 🎛️ **Périphériques audio configurables** (entrée/sortie séparées)
- 🔌 **Contrôle PTT** par port série (RTS / DTR / RTS+DTR / NONE)

---

## 💻 Prérequis

### Système

- **Linux** : Debian 11/12, Ubuntu 22.04/24.04, Raspberry Pi OS (Bullseye/Bookworm)
- **Windows** : Windows 10 (64-bit) et Windows 11
- **Python** 3.9 ou supérieur

### Matériel

- Transceiver VHF/UHF relié à la carte son de l'ordinateur (câble audio bidirectionnel)
- Commande PTT via port série (RTS ou DTR sur un adaptateur USB-série) — ou PTT NONE si test audio uniquement
- Adaptateur USB-son ou carte son intégrée reconnue par le système

### Logiciels

| Logiciel | Rôle |
|---|---|
| **Dire Wolf** | TNC logiciel (modulation/démodulation AFSK, AX.25, PTT) |
| **Python 3.9+** | Environnement d'exécution |

---

## 📦 Installation

> 📚 **Notice d'installation détaillée** (Linux + Windows) disponible en PDF dans la [dernière Release](../../releases/latest).

### Linux (Debian / Ubuntu / Raspberry Pi OS)

```bash
# 1. Mettre à jour
sudo apt update && sudo apt upgrade -y

# 2. Installer Python3 et pip
sudo apt install python3 python3-pip python3-venv -y

# 3. Installer les dépendances système
sudo apt install direwolf libportaudio2 portaudio19-dev -y

# 4. Ajouter l'utilisateur au groupe dialout (PTT)
sudo usermod -aG dialout $USER

# 5. Créer et activer l'environnement virtuel
python3 -m venv venv_aprs
source venv_aprs/bin/activate

# 6. Installer les dépendances Python
pip install numpy sounddevice pyserial crcmod flask

# 7. Lancer
python3 aprs.py
```

### Windows 10 / 11

```powershell
# 1. Installer Python 3.9+ depuis https://www.python.org
#    Cocher "Add Python to PATH" lors de l'installation

# 2. Télécharger Dire Wolf (binaire Windows .zip)
#    https://github.com/wb2osz/direwolf/releases
#    Extraire dans C:\direwolf\ et ajouter au PATH

# 3. Créer et activer l'environnement virtuel
python -m venv venv_aprs
.\venv_aprs\Scripts\Activate.ps1

# 4. Installer les dépendances Python
pip install numpy sounddevice pyserial crcmod flask

# 5. Lancer
python aprs.py
```

Une fois lancé, ouvrir un navigateur et accéder à **`http://localhost:5001`**.

---

## ⚙️ Configuration

La configuration s'effectue directement depuis l'interface web. Les paramètres sont sauvegardés automatiquement dans `config.json`.

| Paramètre | Description |
|---|---|
| `callsign` | Indicatif de la station (ex. F4XXX-9) |
| `serial_port` | Port série PTT : `/dev/ttyUSB0` (Linux) ou `COM3` (Windows) |
| `ptt_mode` | Mode PTT : `RTS`, `DTR`, `RTS+DTR`, ou `NONE` |
| `audio_device_tx` | Périphérique audio émission (null = auto-detect) |
| `audio_device_rx` | Périphérique audio réception (null = auto-detect) |
| `tx_delay_ms` | Délai TX en millisecondes (défaut : 300 ms) |
| `path` | Path APRS (défaut : `WIDE1-1,WIDE2-1`) |
| `maidenhead` | Locator Maidenhead (ex. JN03QT) |
| `beacon_interval` | Intervalle balise en secondes (0 = désactivé) |

---

## 📜 Historique des versions

### Version 2.0 — Mai 2026

- Backend entièrement basé sur Dire Wolf (KISS/TCP port 8001)
- Interface web Flask complète (balises, météo, chat, propagation)
- Configuration persistante via `config.json`
- Compatible Windows 10/11 et Linux/Raspberry Pi
- Génération automatique du fichier de configuration Dire Wolf

---

## 📜 Licence

Logiciel **gratuit** développé dans l'esprit **HAM SPIRIT** :

- ✅ Utilisation libre
- ✅ Distribution autorisée
- ✅ Modification autorisée *(merci de créditer l'auteur)*

---

## 👤 Auteur

**F1RIQ Anthony** — Développeur principal
📧 `tonyf1riq@gmail.com`

## 🐛 Signaler un bug ou suggérer une amélioration

- Discord Les F4 : [discord.gg/u5Nqpu7ECM](https://discord.gg/u5Nqpu7ECM)
- Issues GitHub : [ouvrir une Issue](../../issues)
- Mail : `tonyf1riq@gmail.com`

---

## 🔗 Maintenance du dépôt

Ce dépôt est maintenu par **[F4MAJ](https://f4maj.fr)** au nom de la communauté **Discord Les F4**, afin de centraliser les développements logiciels de la communauté.

Site associé : [f4maj.fr](https://f4maj.fr)
Discord : [discord.gg/u5Nqpu7ECM](https://discord.gg/u5Nqpu7ECM)

---

📻 *Bonnes balises APRS et bon trafic !*

**73 de F1RIQ Anthony**
