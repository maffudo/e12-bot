# Alerte annonces — portière arrière gauche BMW E12

Un seul script Python (`bot.py`) qui interroge plusieurs sources, ne garde que les annonces
qui ressemblent à une portière de BMW E12, et vous envoie **une notification par nouvelle annonce**
avec prix, localisation, titre et lien direct.

```
 cron / GitHub Actions (toutes les 5-10 min)
          │
          ▼
      bot.py ──► eBay (API officielle, FR + DE)
          │  ├─► LeBonCoin (optionnel, depuis une IP de maison)
          │  └─► LesAnciennes + autres sites (URL de recherche dans sources.json)
          ▼
   filtre « E12 + porte » ──► seen.json (mémoire anti-doublons)
          ▼
   Telegram  /  SMS Free Mobile  /  SMS ou WhatsApp Twilio
```

Étiquettes dans les notifications : ✅ arrière gauche · 🟡 arrière (côté à vérifier) ·
🟠 gauche (avant/arrière à vérifier) · ⚪ porte (position non précisée). Le filtre est volontairement
tolérant (une annonce « porte BMW E12 » sans côté est envoyée) : seules les portières explicitement
**avant**, les miniatures et les « recherche… » sont écartées.

---

## 1. Telegram (recommandé : gratuit, instantané, pas de limite)

1. Dans Telegram, ouvrez une conversation avec **@BotFather** → `/newbot` → choisissez un nom.
   Il vous donne un **token** (`123456:ABC...`) → `TELEGRAM_BOT_TOKEN`.
2. Ouvrez la conversation avec votre nouveau bot et envoyez-lui `/start`.
3. Dans un navigateur : `https://api.telegram.org/bot<VOTRE_TOKEN>/getUpdates`.
   Repérez `"chat":{"id":123456789` → c'est `TELEGRAM_CHAT_ID`.
4. Activez les notifications sonores de cette conversation sur votre téléphone : c'est votre « SMS ».

## 2. (Option) SMS gratuit Free Mobile

Réservé aux abonnés Free Mobile, vers **leur propre ligne** uniquement.
Espace abonné → *Mes options* → *Notifications par SMS* → activer. Vous obtenez un identifiant
(`FREE_MOBILE_USER`) et une clé (`FREE_MOBILE_PASS`).

## 3. (Option) Twilio — SMS ou WhatsApp

1. Créez un compte sur twilio.com. Le compte d'essai est crédité mais n'envoie qu'à des numéros vérifiés.
2. **SMS** : achetez/obtenez un numéro Twilio → `TWILIO_FROM=+1...`, `TWILIO_TO=+33...`.
   Le coût est de quelques centimes par SMS.
3. **WhatsApp (sandbox)** : Console → Messaging → Try it out → WhatsApp, envoyez le code « join … » depuis
   votre téléphone, puis `TWILIO_FROM=whatsapp:+14155238886` et `TWILIO_TO=whatsapp:+33...`.
4. Renseignez aussi `TWILIO_ACCOUNT_SID` et `TWILIO_AUTH_TOKEN` (page d'accueil de la console).

Vous pouvez activer plusieurs canaux en même temps : tous ceux qui sont configurés reçoivent le message.

## 4. eBay (API officielle, gratuite)

1. Créez un compte sur developer.ebay.com → *Application Keys* → créez un **keyset Production**.
2. Un keyset neuf est **désactivé** tant que vous n'avez pas traité l'obligation « Marketplace Account
   Deletion ». Pour un usage personnel de lecture seule, choisissez l'option d'**exemption**
   (« I do not persist eBay data »). Le keyset s'active ensuite.
3. Copiez **App ID (Client ID)** → `EBAY_CLIENT_ID` et **Cert ID** → `EBAY_CLIENT_SECRET`.

Le bot interroge par défaut `EBAY_FR` et `EBAY_DE` (beaucoup de pièces d'E12 en Allemagne), en ne
gardant que les annonces livrables en France (`EBAY_DELIVERY_COUNTRY`). Modifiez `EBAY_MARKETPLACES`
pour ajouter `EBAY_IT`, `EBAY_GB`, etc.

## 5. LesAnciennes et autres sites d'annonces

1. Sur le site, faites la recherche à la main (« BMW E12 » + « porte »), **triez par date décroissante**.
2. Copiez l'URL de la page de résultats dans `sources.json` (les entrées « REMPLACER… » sont ignorées).
3. Vérifiez avec `python bot.py --dry-run --debug --sources html`.

L'extracteur est générique (il associe chaque lien au texte de sa carte) : il ne dépend pas de
sélecteurs CSS, mais la **localisation** n'est trouvée que si le site affiche un code postal ou un
département dans la carte (sinon « voir l'annonce »). Fonctionne mal sur les sites qui chargent leurs
résultats en JavaScript.

## 6. LeBonCoin

Pas d'API publique, et une protection anti-bot forte : les IP de GitHub Actions et des datacenters sont
presque toujours bloquées. Deux options :

- **Recommandé en filet de sécurité** : créez une alerte de recherche dans l'application LeBonCoin
  (« BMW E12 portière », notifications push/e-mail). C'est gratuit, fiable et conforme aux CGU.
- **Via ce bot** : `SOURCES=ebay,leboncoin,html` depuis une machine **à la maison** (PC, Raspberry Pi),
  idéalement avec `pip install curl_cffi`. Les CGU de LeBonCoin interdisent l'extraction automatisée :
  gardez une fréquence faible (≥ 5 min), usage strictement personnel, à vos risques. Si le site
  répond 403/captcha, le bot s'arrête pour cette source et vous prévient après 6 échecs consécutifs.

## 7. Lancer en local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # puis remplissez vos clés
python bot.py --test        # un message doit arriver sur votre téléphone
python bot.py --dry-run --debug   # lit les sources et affiche, sans rien envoyer
python bot.py               # 1re exécution : mémorise l'existant + 1 message récapitulatif
```

À la première exécution d'une source, les annonces déjà en ligne sont **mémorisées sans spam** et résumées
dans un seul message (10 max). Ajoutez `--notify-existing` pour recevoir chacune individuellement.

**Planification (cron, toutes les 5 minutes)** — `crontab -e` :

```
*/5 * * * * cd /home/pi/e12-alert && .venv/bin/python bot.py >> bot.log 2>&1
```

## 8. Héberger gratuitement ou presque

| Option | Coût | Fréquence réaliste | LeBonCoin possible ? |
|---|---|---|---|
| PC / Raspberry Pi à la maison + cron | 0 € (électricité) | 3-5 min | Oui (IP résidentielle) |
| GitHub Actions | 0 € | 10-15 min (retards possibles) | Non (IP bloquées) |
| VPS (Oracle Cloud Always Free, Hetzner ≈ 4 €/mois) | 0-4 € | 3-5 min | Rarement (IP datacenter) |

### GitHub Actions pas à pas

1. Créez un dépôt GitHub et poussez ce dossier (`.env` est dans `.gitignore`, ne le poussez pas).
2. *Settings → Secrets and variables → Actions → New repository secret* : ajoutez
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`
   (et `FREE_MOBILE_*` / `TWILIO_*` si utilisés). Les secrets sont masqués dans les logs.
3. Onglet *Actions* → « Alerte E12 » → *Run workflow* pour un premier lancement manuel.
4. Le workflow tourne ensuite tout seul et enregistre `seen.json` dans le dépôt.

À savoir :
- **Dépôt public** : minutes illimitées, mais `seen.json` (titres/liens d'annonces) est lisible par tous.
  **Dépôt privé** : 2000 min/mois gratuites ; passez le cron à `*/30` pour ne pas les dépasser.
- GitHub retarde parfois les tâches planifiées de plusieurs minutes et peut désactiver un workflow
  planifié après une longue inactivité du dépôt : si les alertes s'arrêtent, relancez-le à la main.
- Ce n'est donc pas du « vrai temps réel » : pour 3-5 minutes, préférez la machine à la maison.

## 9. Éviter les blocages

Principe : **être un client discret plutôt que contourner des protections.**

1. **API officielle quand elle existe** (eBay) : aucun risque de blocage.
2. **Fréquence raisonnable** : 5-10 min suffisent pour une pièce rare. Pas de requêtes à la seconde.
3. **En-têtes réalistes** : User-Agent de navigateur récent, `Accept-Language: fr-FR`, session HTTP
   réutilisée, pauses aléatoires de 1,5 à 4 s entre deux pages, timeouts, un seul nouvel essai.
4. **Arrêt net au premier 403/429/captcha** : le bot n'insiste pas, compte l'échec et vous prévient
   après `FAIL_ALERT_AFTER` exécutions (6 par défaut).
5. **IP résidentielle** pour les sites protégés (maison > serveur cloud).
6. **Filets de sécurité natifs** : alertes de recherche intégrées de LeBonCoin, eBay (« enregistrer la
   recherche »), Facebook Marketplace. Le bot ne gère volontairement ni résolution de captcha ni rotation de proxys.

## 10. Dépannage

- `--test` ne reçoit rien : vérifiez le token, que vous avez bien écrit `/start` au bot, et le `chat_id`.
- `[ebay] ignorée` : clés absentes. `401/403` : keyset encore désactivé (étape 4.2).
- `[leboncoin] échec … anti-bot` : normal depuis un serveur ; utilisez l'alerte native ou la machine à la maison.
- Une source ne trouve rien : `python bot.py --dry-run --debug --sources html` liste ce qui est rejeté.
- Tester le filtre : `python test_bot.py`.

## 11. Limites à connaître

- Les extracteurs LeBonCoin et « générique » ont été écrits d'après la structure habituelle de ces
  pages et testés sur des **données simulées** : vérifiez-les avec `--dry-run --debug` sur vos vraies URL,
  ces sites changent leur HTML sans prévenir.
- Une même pièce publiée sur deux sites générera deux alertes.
- Une annonce sans « E12 » ni « BMW + 520/525/528… » dans le titre ou le texte de la carte ne sera pas vue.
