#!/bin/bash

# ─────────────────────────────────────────
# Attente de la base de données
# ─────────────────────────────────────────
if [ "$DB_HOST" = "pgbouncer" ] || [ "$DB_HOST" = "db" ]; then
    echo "⏳ En attente de PostgreSQL ($DB_HOST:$DB_PORT)..."
    while ! python -c "import socket; s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(1); s.connect(('$DB_HOST', int('$DB_PORT')))" 2>/dev/null; do
        sleep 1
    done
    echo "✓ PostgreSQL disponible"
fi

# ─────────────────────────────────────────
# 1. Migrations
# ─────────────────────────────────────────
if [ "$RUN_MIGRATIONS" = "True" ]; then
    echo "📦 Application des migrations..."
    python manage.py migrate
    echo "✓ Migrations appliquées"
else
    echo "→ Migrations désactivées pour ce conteneur"
fi

# ─────────────────────────────────────────
# 2. Fichiers statiques
# ─────────────────────────────────────────
if [ "$COLLECT_STATIC" = "True" ]; then
    echo "Génération des fichiers statiques..."
    python manage.py collectstatic --no-input --clear
    echo "✓ Fichiers statiques générés"
else
    echo "→ Collectstatic désactivé pour ce conteneur"
fi

# ─────────────────────────────────────────
# 3. Initialisation des rôles
# ─────────────────────────────────────────
if [ "$INIT_ROLES" = "True" ]; then
    echo "Initialisation des rôles et permissions..."
    python manage.py init_roles
    echo "✓ Rôles initialisés"
else
    echo "→ Initialisation des rôles désactivée pour ce conteneur"
fi

# ─────────────────────────────────────────
# 4. Initialisation du système (superadmin)
# ─────────────────────────────────────────
if [ "$INIT_SYSTEM" = "True" ]; then
    echo "Initialisation du compte système et du superadmin..."
    python manage.py init_system \
        --keycloak_id "${SUPER_ADMIN_KEYCLOAK_ID}" \
        --compte_id   "${SYSTEM_COMPTE_ID}" \
        --email       "${SUPER_ADMIN_EMAIL}" \
        --tel         "${SUPER_ADMIN_TEL}" \
        --nom         "${SUPER_ADMIN_NOM}" \
        --prenom      "${SUPER_ADMIN_PRENOM}" \
        --password    "${SUPER_ADMIN_PASSWORD}"

    if [ $? -ne 0 ]; then
        echo "❌ Échec de init_system — arrêt du démarrage"
        exit 1
    fi

    echo "✓ Système initialisé"
else
    echo "→ Initialisation système désactivée pour ce conteneur"
fi

# ─────────────────────────────────────────
# Démarrage de l'application
# ─────────────────────────────────────────
exec "$@"