from django.core.management.base import BaseCommand
from django.db import transaction
from datetime import date
import logging

from recouvrement.models import Contrat, Lease

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Génère les échéances journalières (Leases) pour tous les contrats actifs."

    def handle(self, *args, **options):
        aujourdhui = date.today()

        self.stdout.write(self.style.WARNING(f"--- Début de la génération des Leases pour le {aujourdhui} ---"))

        # On récupère les contrats dont l'échéance est arrivée
        contrats_actifs = Contrat.objects.filter(
            statut=Contrat.STATUT_ACTIF,
            prochaine_echeance__lte=aujourdhui
        )

        total_contrats = contrats_actifs.count()
        self.stdout.write(f"{total_contrats} contrats à traiter trouvés.")

        crees = 0
        erreurs_ou_doublons = 0

        for contrat in contrats_actifs:
            try:
                # Utilisation d'une transaction pour chaque contrat pour éviter
                # qu'une erreur sur le contrat A ne fasse planter le contrat B
                with transaction.atomic():
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
                        # Optionnel : Mettre à jour la date de prochaine échéance du contrat ici
                        # contrat.prochaine_echeance = ...
                        # contrat.save(update_fields=['prochaine_echeance'])
                    else:
                        # Le lease existait déjà (évité grâce au unique_together)
                        erreurs_ou_doublons += 1

            except Exception as e:
                erreurs_ou_doublons += 1
                logger.error(f"Erreur lors de la génération du lease pour le contrat {contrat.id}: {str(e)}")

        # Affichage du bilan dans la console du serveur
        self.stdout.write(self.style.SUCCESS(
            f"--- Terminé ! {crees} Leases créés, {erreurs_ou_doublons} ignorés (doublons/erreurs). ---"
        ))