from django.db.models import Sum
from django.utils import timezone

from recouvrement.models import Contrat, Lease
from statistiques.models import StatistiqueJournaliere


def statistiques_du_jour(date_cible=None):
    """
    Recalcule les statistiques globales pour une date donnée en se basant
    uniquement sur la table Lease, sans table Compte externe.
    """
    if not date_cible:
        date_cible = timezone.now().date()

    # 1. 🚀 L'ASTUCE ICI : On récupère tous les compte_id distincts
    # à partir des contrats actifs.
    comptes_actifs_ids = Contrat.objects.filter(
        statut=Contrat.STATUT_ACTIF
    ).values_list('compte_id', flat=True).distinct()

    # 2. On boucle directement sur ces IDs entiers
    for compte_id in comptes_actifs_ids:
        # On cible TOUTES les échéances (Leases) du jour pour ce compte_id précis.
        # (Grâce à ton BaseModel, Lease a déjà la colonne compte_id !)
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
        montant_echec = montant_attendu - montant_collecte

        # --- COMPORTEMENT CHAUFFEURS ---
        total_attendus = leases_du_jour.values('contrat__chauffeur').distinct().count()

        ayant_verse = leases_du_jour.filter(
            montant_paye__gt=0
        ).values('contrat__chauffeur').distinct().count()

        n_ayant_pas_verse = max(0, total_attendus - ayant_verse)

        # --- SAUVEGARDE ---
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