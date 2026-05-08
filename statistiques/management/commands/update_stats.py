from django.core.management.base import BaseCommand
from django.utils import timezone
import datetime

from statistiques.services import statistiques_du_jour


class Command(BaseCommand):
    help = 'Rafraîchit la table StatistiqueJournaliere. Utilise --date pour cibler un jour précis.'

    def add_arguments(self, parser):
        # On ajoute l'argument optionnel --date
        parser.add_argument(
            '--date',
            type=str,
            help='Date cible au format YYYY-MM-DD (ex: 2026-05-01). Par défaut: date du jour.',
        )

    def handle(self, *args, **options):
        heure_actuelle = timezone.now().strftime('%H:%M:%S')

        # 1. Vérification et formatage de la date passée en paramètre
        date_str = options.get('date')
        if date_str:
            try:
                # On transforme la chaîne "YYYY-MM-DD" en objet Date
                date_cible = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                self.stdout.write(self.style.ERROR(
                    f"❌ Erreur : Format de date invalide '{date_str}'. Veuillez utiliser le format YYYY-MM-DD."))
                return  # On stoppe l'exécution de la commande
        else:
            # Si aucun argument n'est passé, on prend la date du jour
            date_cible = timezone.now().date()

        self.stdout.write(f"[{heure_actuelle}] ⏳ Démarrage de l'agrégation pour la date : {date_cible}...")

        try:
            # 2. Appel du service en lui passant explicitement la date
            statistiques_du_jour(date_cible=date_cible)

            self.stdout.write(
                self.style.SUCCESS(f"[{heure_actuelle}] ✅ Statistiques mises à jour avec succès pour le {date_cible}."))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"[{heure_actuelle}] ❌ Erreur critique : {str(e)}"))