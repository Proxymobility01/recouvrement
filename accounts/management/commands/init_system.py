from django.core.management.base import BaseCommand
from django.db import transaction
from django.contrib.auth.hashers import make_password

from accounts.models import CustomUser, Role, CustomUserRole


class Command(BaseCommand):
    help = "Initialise le super-administrateur du système"

    def add_arguments(self, parser):
        parser.add_argument('--keycloak_id', type=str, required=True)
        parser.add_argument('--compte_id', type=int, required=True)
        parser.add_argument('--email', type=str, required=True)
        parser.add_argument('--tel', type=str, required=True)
        parser.add_argument('--nom', type=str, required=True)
        parser.add_argument('--prenom', type=str, required=True)
        parser.add_argument('--password', type=str, required=True)

    def handle(self, *args, **options):
        keycloak_id = options['keycloak_id']
        compte_id = options['compte_id']
        email = options['email']
        tel = options['tel']  # Récupéré mais non utilisé dans ce modèle, prêt pour l'avenir
        nom = options['nom']
        prenom = options['prenom']
        password = options['password']

        nom_complet = f"{nom} {prenom}".strip()

        try:
            with transaction.atomic():
                # ==========================================
                # 1. CRÉATION DU SUPER-ADMINISTRATEUR
                # ==========================================
                user, created_user = CustomUser.objects.get_or_create(
                    keycloak_id=keycloak_id,
                    defaults={
                        'email': email,
                        'nom_complet': nom_complet,
                        'is_staff': True,
                        'is_superuser': True,
                        'is_active': True,
                        'compte_id': compte_id,
                        'password': make_password(password)
                    }
                )

                if not created_user:
                    user.email = email
                    user.nom_complet = nom_complet
                    user.is_staff = True
                    user.is_superuser = True
                    user.compte_id = compte_id
                    user.set_password(password)
                    user.save()
                    self.stdout.write(self.style.WARNING(f"👤 SuperAdmin '{keycloak_id}' mis à jour."))
                else:
                    self.stdout.write(self.style.SUCCESS(f"👤 SuperAdmin '{keycloak_id}' créé avec succès."))

                # ==========================================
                # 2. ASSIGNATION DU RÔLE SUPER_ADMIN
                # ==========================================
                try:
                    role_super_admin = Role.objects.get(slug="SUPER_ADMIN")

                    assignation, created_role = CustomUserRole.objects.get_or_create(
                        user=user,
                        role=role_super_admin,
                        defaults={
                            'actif': True,
                            'principal': True,
                            'compte_id': compte_id
                        }
                    )

                    if created_role:
                        self.stdout.write(self.style.SUCCESS(f"🛡️ Rôle SUPER_ADMIN assigné à {keycloak_id}."))

                except Role.DoesNotExist:
                    self.stdout.write(self.style.ERROR("❌ Le rôle SUPER_ADMIN n'existe pas."))
                    raise Exception("Rôle SUPER_ADMIN introuvable.")

            self.stdout.write(self.style.SUCCESS("\n🚀 INITIALISATION DU SYSTÈME TERMINÉE AVEC SUCCÈS !"))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"\n❌ ERREUR FATALE LORS DE L'INITIALISATION : {str(e)}"))
            raise e