# 🛰️ Py-APRS V2.4 — Notes de version

Publication GitHub Les F4 — Mai 2026  
Auteur : **F1RIQ Anthony**

---

## Résumé

Cette version publie **Py-APRS V2.4** sur le GitHub Les F4.

Elle inclut la mise à jour du changelog intégré, la base V2.4 du script principal et les évolutions intégrées depuis la V2.3.

---

## Nouveautés V2.4

- Passage officiel en version **2.4**.
- Date applicative : `2026-05-27`.
- Changelog intégré mis à jour.
- Interface d'aide cohérente avec la version 2.4.

---

## Évolutions V2.3 intégrées

- Passages ISS avec moteur **SGP4 embarqué**.
- TLE ISS rechargés toutes les 6 h depuis CelesTrak, avec sources de secours.
- Cache disque des TLE pour fonctionnement hors-ligne limité.
- Élévation maximale affichée pour chaque passage ISS.
- Fermeture propre du programme via `SIGTERM`, `SIGINT` et `atexit`.
- Sauvegarde synchrone et atomique de `chat.json` et `config.json` à l'arrêt.
- Fermeture ordonnée des sockets KISS TX et iGate.

---

## Points importants

- Le script principal est `aprs.py`.
- Le fichier annonce `APP_VERSION = "2.4"`.
- Le compte local créé au premier lancement est :

```text
Identifiant : admin
Mot de passe : aprs1200
```

⚠️ Le mot de passe doit être changé dès la première connexion.

---

## Contrôle avant publication

- Fichier contrôlé comme vrai fichier Python, et non HTML.
- Syntaxe Python : Syntaxe Python OK.
- Aucun token GitHub, clé privée ou mot de passe personnel détecté lors du contrôle rapide.

---

## Fichier téléchargeable conseillé pour la release

| Fichier | Description |
|---|---|
| `aprs.py` | Script principal Py-APRS V2.4 |

---

73 de l'équipe Les F4.
