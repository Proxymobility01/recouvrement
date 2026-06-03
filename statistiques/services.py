from django.db.models import Sum
from django.utils import timezone

from recouvrement.models import Lease
from statistiques.models import StatistiqueJournaliere


def statistiques_du_jour(date_cible=None):
    """
    Recalcule les statistiques globales pour une date donnée.
    """
    if not date_cible:
        date_cible = timezone.now().date()

    # 1. 🚀 CORRECTION : On récupère les compte_id directement depuis les Leases du jour.
    # Ainsi, même si un contrat a été suspendu ou soldé dans la journée,
    # ses échéances d'aujourd'hui seront bien prises en compte.
    comptes_concernes = Lease.objects.filter(
        date_echeance=date_cible
    ).values_list('compte_id', flat=True).distinct()

    for compte_id in comptes_concernes:
        # Cible les échéances du compte pour la journée
        leases_du_jour = Lease.objects.filter(
            compte_id=compte_id,
            date_echeance=date_cible
        )

        # --- FINANCES ---
        stats_finances = leases_du_jour.aggregate(
            total_attendu=Sum('montant_attendu'),
            total_paye=Sum('montant_paye')
        )

        montant_attendu = stats_finances['total_attendu'] or 0
        montant_collecte = stats_finances['total_paye'] or 0

        # Le manque à gagner (déficit) du jour
        montant_echec = montant_attendu - montant_collecte

        # --- COMPORTEMENT CHAUFFEURS ---
        total_attendus = leases_du_jour.values('contrat__chauffeur').distinct().count()

        # On considère qu'un chauffeur a versé s'il a payé plus de 0 XAF aujourd'hui
        ayant_verse = leases_du_jour.filter(
            montant_paye__gt=0
        ).values('contrat__chauffeur').distinct().count()

        n_ayant_pas_verse = max(0, total_attendus - ayant_verse)

        # --- SAUVEGARDE (Idempotence garantie) ---
        StatistiqueJournaliere.objects.update_or_create(
            compte_id=compte_id,
            date=date_cible,
            defaults={
                'montant_attendu': montant_attendu,
                'montant_collecte': montant_collecte,
                'montant_echec': montant_echec,
                'total_attendus': total_attendus,
                'ayant_verse': ayant_verse,
                'n_ayant_pas_verse': n_ayant_pas_verse,
            }
        )