# 📡 Py-APRS

![Downloads](https://img.shields.io/badge/downloads-15-brightgreen)

Station APRS web complète — interface Flask + backend Dire Wolf.

> **Version 2.8.5 — Juillet 2026**  
> Développé par **F1RIQ Anthony** — dépôt **LesF4** maintenu à jour

---

## Le mot de l'auteur

Py-APRS est un serveur web Flask exposant une interface APRS complète : balises, météo, chat, carte, statistiques, carnet de trafic, propagation et diagnostics.

La modulation / démodulation audio et la gestion PTT sont déléguées à **Dire Wolf** via le protocole **KISS/TCP** sur le port `8001`.

Compatible **Windows 10/11** et **Linux** : Debian, Ubuntu, Raspberry Pi OS.

73, F1RIQ Anthony

---

## ✨ Fonctionnalités principales

- Émission / réception APRS complète en AFSK 1200 bauds via Dire Wolf.
- Interface web accessible depuis un navigateur : `http://localhost:5001`.
- Moniteur de trafic APRS en temps réel : TX, RX et APRS-IS.
- Carte APRS avec positions, symboles, trajectoires et informations décodées.
- Gestion des messages APRS / QSO avec ACK, historique et conversations persistantes.
- Balises automatiques configurables : Station, ISS, Météo, Propagation, Lune et textes libres.
- Beacon météo via Open-Meteo.
- Beacon propagation via NOAA SWPC.
- Nouvelle balise Lune : azimut, élévation, distance et phase lunaire.
- Alertes météo et alertes propagation / blackout HF.
- Support APRS-IS : RX-iGate et mode iGate selon configuration.
- Digipeater APRS logiciel avec alias configurables.
- Onglet ISS avec suivi des passages et aide aux balises ARISS.
- Statistiques 24 h, meilleur DX entendu, maillage RF Maidenhead et historique de trafic.
- Mode jour / nuit.
- Authentification web avec changement de mot de passe.

---

## 🆕 Nouveautés v2.8.5

- Onglet Stats : bloc Alertes Propagation & Blackout HF déplacé au-dessus du graphique Trafic RX/TX/IS 24 h.
- Balise automatique Lune ajoutée : azimut, élévation, distance et phase lunaire.
- Bouton **BEACON LUNE** dans le tableau de bord.
- Intervalle de balise Lune configurable dans les réglages.
- Améliorations Dire Wolf : redétection audio ALSA à chaque redémarrage.
- Meilleure stabilité du watchdog RX et du pipeline Dire Wolf.
- Corrections Mic-E : filtrage des vitesses aberrantes et amélioration du décodage.
- Amélioration du mode jour : contraste corrigé dans le moniteur trafic et QSO.
- Améliorations statistiques : meilleur DX entendu, plafond de plausibilité et alertes propagation / blackout HF.
- Affichage du locator Maidenhead dans le moniteur de trafic.
- Améliorations QSO / messages APRS : support reply-ack et meilleure gestion des ACK.
- Corrections météo APRS RF : bulletins plus compatibles avec les TNC / terminaux.
- Logs : rotation quotidienne et rétention optimisée.

---

## ⚠️ Attention avant installation

Avant installation ou mise à jour sur une station réelle, sauvegarder les fichiers existants puis vérifier :

- indicatif ;
- coordonnées de station ou locator Maidenhead ;
- périphériques audio RX / TX ;
- port PTT ;
- mode PTT ;
- passcode APRS-IS ;
- mode iGate ;
- digipeater ;
- balises automatiques ;
- clé API APRS.fi éventuelle.

---

## 🚀 Démarrage rapide

1. Installer Python et les dépendances nécessaires.
2. Installer Dire Wolf.
3. Lancer `aprs.py`.
4. Ouvrir l’interface dans le navigateur :

```text
http://localhost:5001
```

5. Aller dans **⚙️ Réglages**.
6. Renseigner l’indicatif, la position, les périphériques audio et le PTT.
7. Cliquer sur **Appliquer**.
8. Vérifier le trafic dans l’onglet **📻 Trafic**.

---

## 🔐 Connexion par défaut

Identifiants par défaut lors de la première installation :

```text
Utilisateur : admin
Mot de passe : aprs1200
```

À modifier immédiatement après la première connexion dans les réglages.

---

## 📡 Modes d’utilisation possibles

### Station RF complète

- Radio VHF sur 144.800 MHz.
- Interface audio type Digirig, Signalink, carte son USB, etc.
- PTT RTS / DTR selon interface.
- Dire Wolf en backend KISS.
- Émission et réception APRS par radio.

### APRS-IS / Internet

Selon configuration, Py-APRS peut aussi utiliser APRS-IS pour le trafic Internet, l’iGate ou des usages sans émission RF.

---

## 🗺️ SSID APRS usuels

Quelques exemples de suffixes courants :

| SSID | Usage courant |
|---:|---|
| -0 | Station principale |
| -5 | Autre équipement |
| -7 | Portable / talkie |
| -9 | Mobile voiture |
| -10 | iGate / APRS-IS |
| -13 | Station météo |
| -15 | Station fixe |

---

## 🧩 Fichiers principaux

| Fichier | Rôle |
|---|---|
| `aprs.py` | Application principale Flask / APRS |
| `config.json` | Configuration station |
| `users.json` | Utilisateurs web |
| `chat.json` | Historique des messages APRS |
| `stats.json` | Statistiques locales |
| `aprs.log` | Journal courant |

---

## 🔧 Mise à jour

Avant toute mise à jour :

1. Sauvegarder le dossier existant.
2. Sauvegarder au minimum `config.json`, `users.json`, `chat.json` et `stats.json`.
3. Remplacer `aprs.py`.
4. Relancer Py-APRS.
5. Vérifier les réglages audio, PTT, APRS-IS et balises.

---

## 📝 Note LesF4

Cette publication met à jour le dépôt GitHub LesF4 avec la version fournie par Anthony / F1RIQ.

Les configurations locales existantes doivent être sauvegardées avant toute mise à jour.

---

## 📜 Licence / responsabilité

Projet radioamateur fourni à titre communautaire et expérimental.

L’utilisateur reste responsable de sa configuration radio, de ses émissions RF, de son indicatif, de ses chemins APRS, de son usage APRS-IS et du respect de la réglementation applicable dans son pays.
