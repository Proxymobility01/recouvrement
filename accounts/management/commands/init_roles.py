from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.contrib.auth.models import Permission
from django.apps import apps
from django.contrib.auth.management import create_permissions
from django.db import DEFAULT_DB_ALIAS

from accounts.models import Role


class Command(BaseCommand):
    help = "Initialise les rôles et permissions spécifiques au système de Recouvrement"

    def handle(self, *args, **kwargs):
        # ==========================================
        # 1. SYNCHRONISATION DES PERMISSIONS DJANGO
        # ==========================================
        for app_config in apps.get_app_configs():
            create_permissions(app_config, verbosity=0, using=DEFAULT_DB_ALIAS)

        ContentType.objects.clear_cache()

        self.stdout.write(self.style.WARNING("Synchronisation des permissions terminée.\n"))

        # ==========================================
        # 2. PRÉPARATION DES PACKS DE PERMISSIONS
        # ==========================================
        all_permissions = list(Permission.objects.all())

        # Permissions PARTNER_ADMIN :
        # Ils ont tous les droits sur les contrats, MAIS aucune modification/suppression
        # n'est autorisée sur les paiements. Ils doivent annuler et recréer.
        partner_perms = list(Permission.objects.filter(
            content_type__app_label__in=['recouvrement', 'accounts','statistiques']
        ).exclude(
            codename__in=['delete_sessionpaiement','change_sessionpaiement','change_paiement', 'delete_paiement','delete_lease','delete_statistique','change_statistique','add_statistique','add_penalite', 'change_penalite', 'delete_penalite']
        ))

        # Permissions DRIVER :
        # Lecture des contrats/paiements et initiation de paiements.

        if not partner_perms:
            self.stdout.write(self.style.ERROR(
                "🚨 ERREUR CRITIQUE : Aucune permission trouvée pour 'partner_perms' ! Vérifie le nom de tes applications (app_label)."))
            return

        driver_perms = list(Permission.objects.filter(
            content_type__app_label='recouvrement',
            codename__in=[
                'view_contrat',
                'view_paiement',
                'add_sessionpaiement',
                'view_sessionpaiement',
                'view_lease',
                'view_penalite'
            ]
        ))

        if not driver_perms:
            self.stdout.write(self.style.ERROR(
                "🚨 ERREUR CRITIQUE : Aucune permission trouvée pour 'driver_perms' ! Vérifie tes codenames (ex: view_contrat vs view_contrats)."))
            return

        # ==========================================
        # 3. DÉFINITION ET CRÉATION DES RÔLES
        # ==========================================
        roles_data = {
            "SUPER_ADMIN": {
                "libelle": "Super Administrateur",
                "niveau": 100,
                "permissions": all_permissions
            },
            "PARTNER_ADMIN": {
                "libelle": "Administrateur Partenaire",
                "niveau": 70,
                "permissions": partner_perms
            },
            "DRIVER": {
                "libelle": "Chauffeur",
                "niveau": 20,
                "permissions": driver_perms
            },
        }

        self.stdout.write(self.style.WARNING(f"Mise à jour des rôles et assignation des droits..."))

        for slug, config in roles_data.items():
            role, created = Role.objects.get_or_create(
                slug=slug,
                defaults={
                    'libelle': config['libelle'],
                    'niveau': config['niveau']
                }
            )

            # Mise à jour des attributs si le rôle existait déjà
            if not created:
                role.libelle = config['libelle']
                role.niveau = config['niveau']
                role.save()

            # Application stricte des permissions définies plus haut
            role.permissions.set(config["permissions"])

            action = "créé" if created else "mis à jour"
            nb_perms = len(config['permissions'])
            self.stdout.write(self.style.SUCCESS(f"✅ Rôle '{slug}' {action} avec {nb_perms} permissions."))

        self.stdout.write(self.style.SUCCESS("\n🎉 Initialisation des rôles Recouvrement terminée avec succès !"))