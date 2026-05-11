from django.core.management.base import BaseCommand
from django.db import transaction
from datetime import date, timedelta, datetime
from dateutil.relativedelta import relativedelta
import logging

from recouvrement.models import Contrat, Lease, Parametre

logger = logging.getLogger(__name__)

# --- Place la fonction calculer_prochaine_date_valide ICI ---
def calculer_prochaine_date_valide(date_actuelle, frequence, jours_repos):
    if frequence == 'JOURNALIER':
        nouvelle_date = date_actuelle + timedelta(days=1)
    elif frequence == 'HEBDOMADAIRE':
        nouvelle_date = date_actuelle + timedelta(weeks=1)
    elif frequence == 'MENSUEL':
        nouvelle_date = date_actuelle + relativedelta(months=1)
    else:
        nouvelle_date = date_actuelle + timedelta(days=1)

    if len(jours_repos) >= 7:
        return nouvelle_date

    while nouvelle_date.weekday() in jours_repos:
        nouvelle_date += timedelta(days=1)

    return nouvelle_date

class Command(BaseCommand):
    help = "Génère les échéances (Leases) pour tous les contrats actifs avec logique de rattrapage et sauts de jours de repos."

    def add_arguments(self, parser):
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
                date_cible = datetime.strptime(date_param, '%Y-%m-%d').date()
            except ValueError:
                self.stdout.write(self.style.ERROR("Erreur : Le format de la date doit être YYYY-MM-DD"))
                return
        else:
            date_cible = date.today()

        self.stdout.write(self.style.WARNING(f"--- Début de la génération des Leases pour le {date_cible} ---"))

        # 🚀 OPTIMISATION : On charge tous les paramètres de toutes les agences en UNE FOIS
        tous_les_parametres = Parametre.objects.all()
        config_par_compte = {param.compte_id: param.jours_repos for param in tous_les_parametres}

        # 2. RECHERCHE DES CONTRATS
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
                    # On récupère la config du compte. Par défaut, si l'entreprise
                    # n'a pas configuré ses paramètres, la liste est vide [] (on ne saute aucun jour)
                    jours_repos = config_par_compte.get(contrat.compte_id, [])

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

                        # 🚀 AVANCEMENT INTELLIGENT DE L'HORLOGE
                        contrat.prochaine_echeance = calculer_prochaine_date_valide(
                            date_actuelle=contrat.prochaine_echeance,
                            frequence=contrat.frequence,
                            jours_repos=jours_repos
                        )

                    # SAUVEGARDE DE LA NOUVELLE DATE
                    contrat.save(update_fields=['prochaine_echeance'])

            except Exception as e:
                erreurs_ou_doublons += 1
                logger.error(f"Erreur lors de la génération du lease pour le contrat {contrat.id}: {str(e)}")

        self.stdout.write(self.style.SUCCESS(
            f"--- Terminé ! {crees} Leases créés, {erreurs_ou_doublons} ignorés (doublons/erreurs). ---"
        ))