# Guide de test de l'API Recouvrement

Référence pratique de tous les endpoints, avec le body attendu et ce que chacun fait, pour tester à la main (curl, Postman, Insomnia...).

## Conventions générales

```bash
export API_URL="http://localhost:8001/api/v1"
export ACCESS_TOKEN="<token-keycloak>"
```

- Toutes les routes exigent `Authorization: Bearer $ACCESS_TOKEN` (sauf `/webhook/`) et `Content-Type: application/json` sur les écritures.
- **Les routes se terminent toujours par `/`** (`APPEND_SLASH=False` : sans le `/` final, l'appel échoue).
- Isolation multi-tenant : chaque utilisateur ne voit/modifie que les données de son `compte_id`. Un **super-admin** doit préciser explicitement `"compte_id": <id>` dans le body de toute création, sinon 400.
- Chaque méthode HTTP exige la permission Django correspondante (`view_/add_/change_/delete_<modèle>`) en plus du token — un utilisateur authentifié mais sans le bon rôle reçoit un 403, pas juste un token invalide.
- Erreurs au format standard :
  ```json
  {"message": "Message utilisateur", "dev_message": "détail technique ou dict de champs invalides"}
  ```
- Listes paginées : 25 résultats par défaut, `?page=2`, `?page_size=50` (max 500) :
  ```json
  {"count": 125, "next": "...", "previous": null, "results": [...]}
  ```
- La plupart des listes acceptent `?search=...` (recherche insensible aux accents/casse) et `?ordering=champ` ou `?ordering=-champ`.

---

## 1. Chauffeurs

Base différente des autres ressources : `/api/v1/accounts/chauffeurs/` (pas `/api/v1/chauffeurs/`).

### `GET /api/v1/accounts/chauffeurs/`
Liste les utilisateurs ayant le rôle `DRIVER` de votre compte.

### `POST /api/v1/accounts/chauffeurs/`
```json
{
  "keycloak_id": "kc-id-du-chauffeur",
  "email": "chauffeur@example.com",
  "nom_complet": "Jean Dupont",
  "is_active": true
}
```
Crée un compte chauffeur local, désactive son mot de passe local (connexion uniquement via Keycloak) et lui attribue automatiquement le rôle `DRIVER`.

### `PATCH /api/v1/accounts/chauffeurs/{id}/`
```json
{"nom_complet": "Jean A. Dupont"}
```

### `DELETE /api/v1/accounts/chauffeurs/{id}/`
Suppression réelle **seulement** si le chauffeur n'a aucun contrat/paiement/session. S'il a un historique, il est **désactivé** (`is_active=false`) au lieu d'être supprimé. Un utilisateur ne peut pas se supprimer lui-même (403).

---

## 2. Agences

### `GET /api/v1/agences/`
Filtres : `?actif=true`, `?zone=DOUALA`. Recherche : `?search=akwa`.

### `POST /api/v1/agences/`
```json
{
  "nom": "Agence Akwa",
  "zone": "Douala",
  "code": "AKW",
  "adresse": "Rue de la joie, Akwa",
  "telephone": "+237600000000",
  "email": "akwa@example.com",
  "actif": true
}
```
`nom` et `code` sont obligatoires. `code` est unique par compte (majuscule automatique) ; `zone` regroupe plusieurs agences d'une même ville.

### `PATCH /api/v1/agences/{id}/` / `GET /api/v1/agences/{id}/`
Consultation ou modification.

### `DELETE /api/v1/agences/{id}/`
Refusé (403) si des contrats/leases/paiements y sont déjà rattachés — désactivez-la (`actif=false`) à la place.

---

## 3. Types de contrats

### `GET /api/v1/type-contrats/`
Filtre : `?est_principal=true`.

### `POST /api/v1/type-contrats/`
```json
{
  "libelle": "Moto",
  "code": "MOTO",
  "est_principal": true
}
```
`est_principal=true` pour un véhicule (contrat principal), `false` pour un accessoire (téléphone, caution, GPS, Royal Care...). `code` unique par compte.

### `PATCH /api/v1/type-contrats/{id}/`

### `DELETE /api/v1/type-contrats/{id}/`
Refusé (403) si des contrats l'utilisent déjà.

---

## 4. Propriétaires

Le propriétaire est le bénéficiaire des collectes d'un contrat.

### `GET /api/v1/proprietaires/`
Filtre : `?actif=true`. Recherche : `?search=royal` (nom ou numéro d'un de ses comptes de réception).

### `POST /api/v1/proprietaires/`
```json
{
  "nom_complet": "Société Royal Care",
  "actif": true
}
```

### `GET /api/v1/proprietaires/{id}/`
Renvoie aussi la liste imbriquée `comptes_reception` (ses comptes Mobile Money) :
```json
{
  "id": 4,
  "compte_id": 2,
  "nom_complet": "MBY Christian",
  "actif": true,
  "comptes_reception": [
    {"id": 4, "operateur": "ORANGE", "operateur_display": "Orange Money", "numero": "237690169694", "nom_titulaire": "MBY Christian", "actif": true}
  ],
  "created_at": "...", "updated_at": "..."
}
```

### `PATCH /api/v1/proprietaires/{id}/`
```json
{"actif": false}
```

### `DELETE /api/v1/proprietaires/{id}/`
Refusé (403) s'il a déjà des contrats, sessions de paiement ou comptes de réception — désactivez-le à la place.

---

## 5. Comptes de réception Mobile Money des propriétaires

### `GET /api/v1/comptes-reception-proprietaires/`
Filtres : `?proprietaire_id=4`, `?operateur=ORANGE`, `?actif=true`. Recherche : `?search=690169694`.

### `POST /api/v1/comptes-reception-proprietaires/`
```json
{
  "proprietaire": 4,
  "operateur": "orange",
  "numero": "690169694",
  "nom_titulaire": "MBY Christian"
}
```
- `operateur` accepte `ORANGE`/`MTN` en toute casse (normalisé en majuscule).
- `numero` est reformaté au standard camerounais (`690169694` → `237690169694`) et doit correspondre à ce format au final (9 chiffres, avec ou sans l'indicatif 237), sinon 400 — la vérification d'une preuve USSD compare ce numéro octet par octet.
- `proprietaire` doit appartenir à votre compte, sinon 400 (message volontairement identique à « propriétaire introuvable » pour ne pas révéler l'existence d'un identifiant chez un autre partenaire).
- Un même propriétaire ne peut avoir qu'un compte par opérateur, et un même numéro+opérateur ne peut être réutilisé qu'une fois par compte partenaire (violation → 400 propre).

### `PATCH /api/v1/comptes-reception-proprietaires/{id}/`
```json
{"actif": false}
```

### `DELETE /api/v1/comptes-reception-proprietaires/{id}/`
Refusé (403) s'il a déjà servi à une session de paiement.

---

## 6. Contrats

### `GET /api/v1/contrats/`
Filtres (`ContratFilter`) : `?statut=ACTIF`, `?statut__in=ACTIF,SUSPENDU`, `?frequence__in=JOURNALIER,MENSUEL`, `?agence_id=3`, `?agence_id__in=3,4`, `?proprietaire_id=4`, `?type_contrat_id=1`, `?date_debut_start=`/`?date_debut_end=`, `?date_fin_start=`/`?date_fin_end=`, `?prochaine_echeance_start=`/`?prochaine_echeance_end=`, `?montant_restant_min=`/`?montant_restant_max=`. Recherche : `?search=dupont` (nom, référence, VIN, immatriculation, agence, propriétaire...).

### `POST /api/v1/contrats/`
```json
{
  "chauffeur": 42,
  "type_contrat": 1,
  "immatriculation": "LT-123-AA",
  "vin": "ABCDEF12345678901",
  "agence": 3,
  "proprietaire": 4,
  "montant_total": "1500000.00",
  "montant_paye": "0.00",
  "montant_par_paiement": "3500.00",
  "frequence": "JOURNALIER",
  "date_debut": "2026-08-01",
  "date_fin": "2027-07-31",
  "prochaine_echeance": "2026-08-04T08:00:00+01:00",
  "specificites": {"marque": "TVS", "modele": "HLX"}
}
```
Crée un **contrat principal** (`type_contrat.est_principal` doit être `true`). Champs obligatoires : `chauffeur`, `type_contrat`, `vin`, `immatriculation`, `proprietaire`, `montant_total`, `montant_par_paiement`, `frequence`, `date_debut`, `date_fin`, `prochaine_echeance`. Un chauffeur ne peut avoir qu'un seul contrat parent actif à la fois (sinon 400). `frequence` : `JOURNALIER` / `HEBDOMADAIRE` / `MENSUEL`. `config_paiement` n'est **pas** saisissable ici : héritée automatiquement du propriétaire/compte par défaut.

### `GET /api/v1/contrats/{id}/`

### `PATCH /api/v1/contrats/{id}/`
```json
{"statut": "SUSPENDU"}
```
- Changer `proprietaire` ou `agence` est **refusé (400)** si le contrat (ou l'un de ses sous-contrats) a déjà des leases/paiements — protège l'audit financier.
- `montant_total`/`montant_paye` modifiés recalculent automatiquement `montant_restant` et soldent le contrat si le reste tombe à 0.

### `DELETE /api/v1/contrats/{id}/`
Hard-delete refusé (403) s'il a des sous-contrats, leases ou paiements.

### `POST /api/v1/contrats/{id}/sous-contrats/`
```json
{
  "type_contrat": 2,
  "montant_total": "180000.00",
  "montant_paye": "0.00",
  "montant_par_paiement": "1000.00",
  "frequence": "JOURNALIER",
  "date_debut": "2026-08-01",
  "date_fin": "2027-01-31",
  "prochaine_echeance": "2026-08-04T08:00:00+01:00",
  "specificites": {"categorie": "Royal Care"}
}
```
Ajoute un accessoire (téléphone, caution, Royal Care...) au contrat `{id}`, qui doit être un contrat parent (pas déjà un sous-contrat). Le sous-contrat hérite automatiquement de l'agence, du propriétaire, du chauffeur et de la configuration de paiement du parent — ces champs ne sont pas saisissables ici. `type_contrat` doit être `est_principal=false`.

### `POST /api/v1/contrats/annuler-leases/`
```json
{
  "lease_ids": [114, 115],
  "jours_a_prolonger": 2
}
```
Annule des échéances non payées (absence, panne...) et prolonge d'autant de jours ouvrés (hors jours de repos) la date de fin des contrats concernés. Échoue si une échéance est déjà (partiellement) payée.

### `GET /api/v1/contrats/impayes-du-jour/`
**Réservé au super-administrateur.** Liste tous les véhicules (tous comptes confondus) ayant un impayé aujourd'hui, avec leurs sous-contrats.

---

## 7. Leases (échéances) — lecture seule

### `GET /api/v1/leases/`
Filtres (`LeaseFilter`) : `?statut=NON_PAYE`, `?statut__in=NON_PAYE,PARTIEL`, `?agence_id=3`, `?agence_id__in=3,4`, `?proprietaire_id=4`, `?date_echeance=2026-08-04`, `?date_echeance_start=`/`?date_echeance_end=`, `?start_date=`/`?end_date=` (date de création). Recherche : `?search=dupont`.

### `GET /api/v1/leases/{id}/`
```json
{
  "id": 114, "compte_id": 21, "contrat_id": 10, "agence": 3, "agence_nom": "Agence Akwa",
  "chauffeur_nom_complet": "Jean Dupont", "type_contrat_libelle": "Moto",
  "date_echeance": "2026-08-04", "montant_attendu": "3500.00", "montant_paye": "0.00",
  "reste_a_payer": "3500.00", "paiement_en_verification": false, "statut": "NON_PAYE", "created_at": "..."
}
```
`paiement_en_verification=true` signale qu'un paiement USSD assisté est en attente de validation dessus (empêche de le payer une seconde fois par erreur).

### `GET /api/v1/leases/calendrier/?mois=8&annee=2026&chauffeur_id=42`
Vue calendrier mensuelle, regroupée par chauffeur, séparant échéances payées / impayées.

---

## 8. Paiements en espèces

### `GET /api/v1/paiements/`
Filtres (`PaiementFilter`) : `?statut=VALIDE`, `?statut__in=...`, `?methode__in=ESPECES,MOBILE_MONEY`, `?agence_id=3`, `?date_paiement=2026-08-04`, `?date_paiement_start=`/`?date_paiement_end=`, `?montant_min=`/`?montant_max=`, `?contrat_id=`, `?lease_id=`, `?session_id=`, `?chauffeur_id=`. Recherche : `?search=...` (référence, nom, téléphone de session...).

### `POST /api/v1/paiements/`
```json
{
  "lease_id": 114,
  "montant": "3500.00"
}
```
Enregistre un encaissement **espèces** effectué par un agent : `methode=ESPECES` et `statut=VALIDE` forcés côté serveur. Met à jour le lease (`PARTIEL`/`PAYE`) et le contrat (`montant_paye`, `montant_restant`, solde automatique). Refusé si le montant dépasse le reste à payer ou si l'échéance est déjà soldée.

### `PATCH /api/v1/paiements/{id}/`
```json
{"montant": "3000.00"}
```
Corrige le montant d'un paiement **espèces** uniquement (interdit sur Mobile Money). Répercute la différence sur le lease et le contrat.

### `DELETE /api/v1/paiements/{id}/`
Toujours refusé (403) — un paiement financier n'est jamais supprimable.

---

## 9. Paiement Mobile Money (passerelle PayGate)

### `POST /api/v1/initier-paiement/`
```json
{
  "phone_number": "677000000",
  "lignes": [
    {"lease_id": 114, "montant": "3500.00"},
    {"lease_id": 115, "montant": "1000.00"}
  ]
}
```
Ouvre un panier de paiement (une ou plusieurs échéances **du même véhicule**, même config de paiement) et déclenche un push USSD PayGate vers `phone_number`. Vérifications avant envoi : échéances non payées/annulées, même compte, même agence, pas d'arriéré antérieur oublié. Réponse `pending:true` — le statut final arrive par webhook, à consulter via `/transactions/`.
```json
{
  "success": true, "pending": true, "message": "Demande de paiement envoyée avec succès.",
  "reference_interne": "MOB.20260804.120000.A1B2C3", "gateway_reference": "...", "montant_total": 4500.0
}
```

### `GET /api/v1/transactions/`
Lecture seule des sessions de paiement (Mobile Money **et** USSD assisté). Filtres (`SessionPaiementFilter`) : `?statut=VALIDE`, `?statut__in=...`, `?canal__in=PASSERELLE,USSD_ASSISTE`, `?operateur__in=ORANGE,MTN`, `?agence_id=`, `?proprietaire_id=`, `?montant_total_min=`/`?montant_total_max=`, `?date_validation=`/`?date_validation_start=`/`?date_validation_end=`, `?reference__icontains=`, `?telephone__icontains=`. Recherche : `?search=MOB.20260804`.

### `GET /api/v1/transactions/{id}/`
Détail d'une session (référence, statut, montant, propriétaire destinataire, `preuve_ussd_statut` le cas échéant).

---

## 10. Paiement USSD assisté

Le chauffeur transfère lui-même par USSD vers le compte du propriétaire, puis colle la preuve SMS reçue ; un agent vérifie ensuite.

### `POST /api/v1/initier-paiement-ussd/`
```json
{
  "operateur": "ORANGE",
  "phone_number": "677000000",
  "lignes": [{"lease_id": 114, "montant": "3500.00"}]
}
```
Ouvre une session `EN_ATTENTE` vers le compte de réception actif du propriétaire du contrat (même vérifications que le paiement passerelle : même véhicule, même agence, même propriétaire). Réponse : référence de session + coordonnées du destinataire à qui transférer l'argent.

### `POST /api/v1/soumettre-preuve-paiement-ussd/`
```json
{
  "sessionReference": "USSD.20260908.120000.A1B2C3",
  "network": "ORANGE",
  "rawText": "Vous avez transféré 3500 F à 690... Frais 50F. Nouveau solde 12000F. Ref: OM240908.1200.A12345",
  "amount": "3500.00",
  "senderPhone": "677000000",
  "recipientPhone": "690123456",
  "reference": "OM240908.1200.A12345",
  "capturedAtDevice": "2026-09-08T12:00:05Z",
  "fee": "50.00",
  "newBalance": "12000.00"
}
```
Dépose la preuve capturée sur le téléphone (montant/opérateur/numéro doivent correspondre exactement à la session). Passe la session en `EN_VERIFICATION` — **aucun paiement n'est encore comptabilisé**, un agent doit valider.

### `GET /api/v1/preuves-paiement-ussd/`
Filtres (`PreuvePaiementUSSDFilter`) : `?statut=EN_VERIFICATION` (ou `VALIDEE`/`REJETEE`), `?statut__in=...`, `?operateur=ORANGE`, `?proprietaire_id=4`, `?session_reference=USSD.20260908`, `?created_at_start=`/`?created_at_end=`, `?verifie_le_start=`/`?verifie_le_end=`. Recherche : `?search=OM240908`.

### `GET /api/v1/preuves-paiement-ussd/{id}/`
```json
{
  "id": 42, "session": 17, "session_reference": "USSD.20260908.120000.A1B2C3",
  "proprietaire_nom_complet": "MBY Christian", "operateur": "ORANGE", "operateur_display": "Orange Money",
  "reference_operateur": "OM240908.1200.A12345", "texte_brut": "Vous avez transféré 3500 F...",
  "montant_transfere": "3500.00", "frais_operateur": "50.00",
  "numero_expediteur": "237677000000", "numero_destinataire": "237690123456",
  "statut": "EN_VERIFICATION", "verifie_par": null, "verifie_le": null, "motif_rejet": "", "created_at": "..."
}
```
Lecture seule (aucune création/modification/suppression directe ici).

### `POST /api/v1/preuves-paiement-ussd/{id}/valider/`
Pas de body. **Nécessite la permission spéciale `can_validate_ussd_payment`** (en plus du token). Valide la preuve, solde les leases/paiements concernés et déclenche en tâche de fond la génération de l'échéance suivante si applicable.
```json
{"message": "Preuve validée avec succès. La ventilation des paiements a été déclenchée.", "modifiee": true, "preuve": {"...": "..."}}
```
Rejouer l'appel sur une preuve déjà validée est sans risque (`modifiee: false`).

### `POST /api/v1/preuves-paiement-ussd/{id}/rejeter/`
```json
{"motif": "Référence de transaction illisible sur la capture"}
```
Même permission requise. `motif` obligatoire (400 sinon). Rejette la preuve, libère les échéances pour un nouveau paiement. Échoue proprement (400) si la preuve n'est plus en attente de vérification.

---

## 11. Webhook PayGate

### `POST /api/v1/webhook/`
Route publique côté HTTP (`AllowAny`) mais protégée par signature : chaque requête doit porter `X-Signature: <HMAC_SHA256_du_corps_brut>` calculée avec le `webhook_secret` de la `ConfigPaiement` du compte concerné. Non destiné à un test manuel classique (réservé à PayGate) — une signature invalide renvoie 403, une référence de session introuvable renvoie 404.

---

## 12. Paramètres (jours de repos)

Un seul enregistrement par compte (singleton).

### `GET /api/v1/parametres/`

### `POST /api/v1/parametres/`
```json
{"jours_repos": [6]}
```
`0=Lundi ... 6=Dimanche`. Refusé (400) si une configuration existe déjà pour le compte — utilisez `PATCH`.

### `PATCH /api/v1/parametres/{id}/`
```json
{"jours_repos": [0, 6]}
```

### `DELETE /api/v1/parametres/{id}/`
Toujours refusé (403).

---

## 13. Règles de pénalité

### `GET /api/v1/regles-penalites/`
Filtres (`ReglePenaliteFilter`) : `?frequence__in=D,W`, `?montant_min=`/`?montant_max=`, `?debut=`/`?debut_start=`/`?debut_end=`, `?occurrences=`. Recherche : `?search=retard`.

### `POST /api/v1/regles-penalites/`
```json
{
  "nom": "Retard journalier",
  "montant": "500.00",
  "occurrences": -1,
  "frequence": "D",
  "cron_expression": null,
  "debut": "2026-09-01T08:00:00+01:00"
}
```
`frequence` : `O`=une fois, `H`=toutes les heures, `D`=tous les jours, `W`=toutes les semaines, `M`=tous les mois, `C`=expression Cron (alors `cron_expression` obligatoire, ex. `"0 10,14,18 * * *"`). `occurrences` : `-1` = infini, `0` = désactivée, ou un nombre précis. Applique une pénalité de `montant` à chaque échéance en retard éligible, jusqu'à `occurrences` fois.

### `PATCH /api/v1/regles-penalites/{id}/` / `GET /api/v1/regles-penalites/{id}/`

### `POST /api/v1/regles-penalites/{id}/assigner-contrats/`
```json
{"contrat_ids": [10, 11, 12]}
```
Affecte la règle à plusieurs contrats **actifs** en une seule fois.

---

## 14. Pénalités — lecture seule

### `GET /api/v1/penalites/`
Filtres (`PenaliteFilter`) : `?statut=NON_PAYE`, `?montant_min=`/`?montant_max=`, `?date_application=`/`?date_application_start=`/`?date_application_end=`, `?contrat_id=`. Recherche : `?search=dupont`.

### `GET /api/v1/penalites/{id}/`

---

## 15. Règles de génération de leases

### `GET /api/v1/regles-gereration-leases/`
*(orthographe conservée telle quelle dans le routeur)*. Filtres : `?frequence=C`, `?actif=true`. Recherche : `?search=quotidienne`.

### `POST /api/v1/regles-gereration-leases/`
```json
{
  "nom": "Deux passages quotidiens",
  "frequence": "C",
  "cron_expression": "0 12,22 * * *",
  "debut": "2026-09-01T12:00:00+01:00",
  "actif": true,
  "defaut": false
}
```
Crée une planification Django Q2 qui génère automatiquement les échéances des contrats qui lui sont rattachés. `defaut: true` l'applique automatiquement à tout nouveau contrat du compte (une seule règle par défaut à la fois). `nom` et `cron_expression` doivent être uniques par compte.

### `PATCH /api/v1/regles-gereration-leases/{id}/` / `GET /api/v1/regles-gereration-leases/{id}/`

### `POST /api/v1/regles-gereration-leases/{id}/assigner-contrats/`
```json
{"contrat_ids": [10, 11, 12]}
```

### `GET /api/v1/regles-gereration-leases/planifications/`
Liste paginée de l'état de planification (prochaine exécution, dernière exécution, succès/échec) de toutes les règles accessibles.

### `GET /api/v1/regles-gereration-leases/{id}/planification/`
Idem pour une seule règle, avec les 10 dernières exécutions détaillées.

### `POST /api/v1/regles-gereration-leases/{id}/executer-maintenant/`
Pas de body. Déclenche immédiatement la génération pour cette règle (jusqu'à l'instant présent) sans modifier sa planification normale. Nécessite de pouvoir consulter **et** modifier les règles de génération.
```json
{"message": "La génération a été placée dans la file d'exécution.", "regle_id": 3, "task_id": "...", "jusqu_a": "...", "next_run": "...", "planification_modifiee": false}
```

---

## 16. Statistiques — lecture seule

### `GET /api/v1/statistiques/`
Filtre : `?date=2026-09-08`. Triées par date décroissante par défaut.

### `GET /api/v1/statistiques/jour/`
Raccourci pour le tableau de bord du jour même. Renvoie une structure à zéro si le calcul du jour n'a pas encore tourné, plutôt qu'un 404 :
```json
{
  "date": "2026-09-08", "montant_attendu": "0", "montant_collecte": "0", "montant_echec": "0",
  "total_attendus": 0, "ayant_verse": 0, "n_ayant_pas_verse": 0, "updated_at": null
}
```

---

## 17. Notifications temps réel (SSE)

### `GET /api/v1/events/?token=<ACCESS_TOKEN>`
Flux Server-Sent Events **personnel** (le token passe en query param, pas en header, car `EventSource` du navigateur ne permet pas de header custom). Heartbeat toutes les 30s. Exemple :
```javascript
const source = new EventSource(`${API_URL}/events/?token=${encodeURIComponent(accessToken)}`);
source.onmessage = (event) => console.log(JSON.parse(event.data));
```
Utile pour voir en direct le résultat final (`SUCCESS`/`FAILED`/`CANCELED`) d'un paiement Mobile Money ou USSD initié par l'utilisateur connecté.
