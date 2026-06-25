# Py-APRS v2.6.9 — 21 juin 2026

Version fournie par F1RIQ / Anthony.

## Nouveautés et corrections

* Bulletin APRS RF d’alerte météo : titres sans accents pour améliorer la compatibilité TNC / terminaux.
* Bulletin APRS RF d’alerte météo : retrait du seuil configuré dans le payload RF.
* Le seuil reste conservé dans l’alerte SSE / interface web.
* Affichage des alertes météo dans le moniteur de trafic avec le badge et la couleur météo, au lieu du badge TX générique.
* Journal système : rétention réduite de 30 à 2 jours d’archives compressées gzip.

## Remarques

Cette version poursuit l’amélioration de la stabilité, de la lisibilité et de la compatibilité APRS RF de Py-APRS.

Avant utilisation en station réelle, vérifier la configuration locale :

* indicatif ;
* port PTT ;
* périphériques audio RX / TX ;
* passcode APRS-IS ;
* mode iGate ;
* balises automatiques ;
* mot de passe d’administration.

## Sécurité

Ne jamais publier les fichiers locaux suivants :

* `config.json`
* `users.json`
* `.flask_secret`
* `aprs.log`
* `chat.json`
* `stats.json`
