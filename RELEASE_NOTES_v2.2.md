# Py-APRS V2.2 — Mise à jour mobile, QRZ et aide intégrée

Station APRS web complète développée par **F1RIQ Anthony** : serveur Flask + interface web + backend Dire Wolf pour la modulation/démodulation APRS et la gestion PTT via KISS/TCP.

> Compatible Windows 10/11 et Linux (Debian / Ubuntu / Raspberry Pi OS).

---

## Nouveautés V2.2

- Liens QRZ.com sur les indicatifs dans la console, les QSO, la carte MAP et les statistiques.
- Navigation mobile améliorée avec barre fixe en bas d’écran.
- Interface mobile-first : viewport optimisé, zones tactiles 44 px, zoom iOS désactivé.
- Carte MAP avec hauteur dynamique sur smartphone.
- Suppression du bloc « Alerte Proximité ».
- Section Aide enrichie avec versioning et changelog intégrés.

---

## Rappel des évolutions V2.1

- iGate APRS-IS : RX-iGate et Full-iGate avec reconnexion automatique.
- Alertes météo : température, vent, rafales, pluie, pression, codes WMO.
- Widget propagation VHF : SFI / Kp / A-index NOAA en temps réel.
- Passages ISS : widget compact dans TRAFIC + alerte avant passage.
- Web Push Notifications via Notification API.
- Statistiques 24 h avec Top 10 stations.
- Persistance des statistiques via `/stats/save` et `/stats/load`.

---

## Téléchargement

Fichier principal à joindre à la Release :

| Fichier | Description |
|---|---|
| `aprs.py` | Script principal Python — Py-APRS V2.2 |

---

## Installation rapide

### Linux / Raspberry Pi OS

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv direwolf libportaudio2 portaudio19-dev -y
sudo usermod -aG dialout $USER

python3 -m venv venv_aprs
source venv_aprs/bin/activate
pip install numpy sounddevice pyserial crcmod flask

python3 aprs.py
```

### Windows 10 / 11

```powershell
python -m venv venv_aprs
.\venv_aprs\Scripts\Activate.ps1
pip install numpy sounddevice pyserial crcmod flask

python aprs.py
```

Puis ouvrir dans le navigateur :

```text
http://localhost:5001
```

---

## Remarques importantes

- Le fichier `config.json` est généré localement par l’application et ne doit pas être publié.
- Les fichiers `chat.json`, `logbook.json`, historiques, logs et configurations personnelles ne doivent pas être publiés.
- En émission APRS, l’opérateur reste responsable de ses émissions et de la conformité réglementaire dans son pays.

---

Logiciel gratuit — esprit Ham Spirit.

73 de **F1RIQ Anthony**  
Maintenance du dépôt GitHub : **Les F4 / F4MAJ**
