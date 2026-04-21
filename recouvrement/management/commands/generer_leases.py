from django.core.management.base import BaseCommand
from django.db import transaction
from datetime import date, timedelta, datetime
from dateutil.relativedelta import relativedelta
import logging

from recouvrement.models import Contrat, Lease

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Génère les échéances (Leases) pour tous les contrats actifs avec logique de rattrapage."

    def add_arguments(self, parser):
        # Ajout d'un argument optionnel nommé --date
        parser.add_argument(
            '--date',
            type=str,
            help='Spécifiez une date cible au format YYYY-MM-DD. Si ignoré, utilise la date du jour.',
        )

    def handle(self, *args, **options):
        # 1. GESTION DE LA DATE CIBLE
        date_param = options.get('date')

        if date_param:
            try:
                # On convertit la chaîne fournie en objet date
                date_cible = datetime.strptime(date_param, '%Y-%m-%d').date()
            except ValueError:
                self.stdout.write(
                    self.style.ERROR("Erreur : Le format de la date doit être YYYY-MM-DD (ex: 2026-04-25)"))
                return  # On arrête le script si la date est mal formatée
        else:
            # Par défaut, on prend la date du jour
            date_cible = date.today()

        self.stdout.write(self.style.WARNING(f"--- Début de la génération des Leases pour le {date_cible} ---"))

        # On récupère les contrats dont l'échéance est arrivée ou dépassée
        contrats_actifs = Contrat.objects.filter(
            statut=Contrat.STATUT_ACTIF,
            prochaine_echeance__lte=date_cible
        )

        total_contrats = contrats_actifs.count()
        self.stdout.write(f"{total_contrats} contrats à traiter trouvés.")

        crees = 0
        erreurs_ou_doublons = 0

        for contrat in contrats_actifs:
            try:
                with transaction.atomic():
                    # 🔄 LA BOUCLE DE RATTRAPAGE
                    while contrat.prochaine_echeance and contrat.prochaine_echeance <= date_cible:

                        lease, created = Lease.objects.get_or_create(
                            contrat=contrat,
                            date_echeance=contrat.prochaine_echeance,
                            compte_id=contrat.compte_id,
                            defaults={
                                'montant_attendu': contrat.montant_par_paiement,
                                'statut': Lease.STATUT_NON_PAYE
                            }
                        )

                        if created:
                            crees += 1
                        else:
                            erreurs_ou_doublons += 1

                        # AVANCEMENT DE L'HORLOGE DU CONTRAT
                        if contrat.frequence == 'JOURNALIER':
                            contrat.prochaine_echeance += timedelta(days=1)
                        elif contrat.frequence == 'HEBDOMADAIRE':
                            contrat.prochaine_echeance += timedelta(weeks=1)
                        elif contrat.frequence == 'MENSUEL':
                            contrat.prochaine_echeance += relativedelta(months=1)
                        else:
                            contrat.prochaine_echeance += timedelta(days=1)

                    # SAUVEGARDE DE LA NOUVELLE DATE
                    contrat.save(update_fields=['prochaine_echeance'])

            except Exception as e:
                erreurs_ou_doublons += 1
                logger.error(f"Erreur lors de la génération du lease pour le contrat {contrat.id}: {str(e)}")

        self.stdout.write(self.style.SUCCESS(
            f"--- Terminé ! {crees} Leases créés, {erreurs_ou_doublons} ignorés (doublons/erreurs). ---"
        ))