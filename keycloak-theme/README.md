# Thème de connexion Keycloak — recouvrement

Personnalise la page de connexion Keycloak (identifiant/mot de passe) aux
couleurs de la marque (orange `#FF5A1F` / noir / blanc), pour qu'elle
s'accorde avec le back-office partenaire. Ne touche à rien côté Django.

## Ce que ça change

- Logo remplacé par le pictogramme de l'app.
- Couleurs, police (Poppins), bouton "Se connecter", champs de formulaire
  recolorés.
- Aucune modification de la logique du formulaire, des messages, ni de la
  sécurité — uniquement du CSS par-dessus le thème officiel `keycloak.v2`.

## Déploiement

1. **Copier le dossier `recouvrement/`** (celui contenant `login/theme.properties`)
   dans le dossier des thèmes du serveur Keycloak :
   - Installation classique : `<KEYCLOAK_HOME>/themes/recouvrement/`
   - Image Docker officielle (`quay.io/keycloak/keycloak`) : `/opt/keycloak/themes/recouvrement/`
     (monter le dossier en volume, ou le copier dans une image Docker
     personnalisée via `COPY keycloak-theme/recouvrement /opt/keycloak/themes/recouvrement`)

2. **Redémarrer Keycloak** (nécessaire pour qu'il détecte le nouveau thème).
   En mode production (`kc.sh start`, pas `start-dev`), les ressources de
   thème sont mises en cache — un redémarrage du conteneur/service suffit à
   les recharger.

3. **Activer le thème** dans la console d'administration :
   `Realm settings` → onglet `Themes` → `Login theme` → sélectionner
   `recouvrement` → `Save`.

4. **Vérifier** : ouvrir l'URL de connexion réelle (`/realms/<realm>/protocol/openid-connect/auth?...`,
   ou simplement se connecter depuis l'app). Si un élément ne prend pas le
   style (variation mineure selon la version exacte de Keycloak) : clic
   droit → Inspecter sur l'élément, relever sa classe/id réel, ajouter une
   règle ciblée dans `login/resources/css/styles.css`.

## Fichiers

```
recouvrement/
  login/
    theme.properties        # déclare le thème, hérite de keycloak.v2
    resources/
      css/styles.css        # toute la personnalisation visuelle
      img/logo.svg           # pictogramme de l'app
```
