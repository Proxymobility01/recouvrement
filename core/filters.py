import django_filters
from django_filters import rest_framework as filters

from recouvrement.models import Lease, Contrat, Paiement, ReglePenalite, Penalite, SessionPaiement, \
    PreuvePaiementUSSD, CompteReceptionProprietaire


class CharInFilter(django_filters.BaseInFilter, django_filters.CharFilter):
    pass

class NumberInFilter(filters.BaseInFilter, filters.NumberFilter):
    """Permet de filtrer sur une liste d'IDs (ex: ?agence_id__in=1,2,3)"""
    pass

class LeaseFilter(filters.FilterSet):
    # Filtre sur le statut (Multiple)
    statut__in = CharInFilter(field_name='statut', lookup_expr='in')

    agence_id = filters.NumberFilter(field_name="agence_id")
    agence_id__in = NumberInFilter(field_name='agence_id', lookup_expr='in')
    proprietaire_id = filters.NumberFilter(
        field_name='contrat__proprietaire_id'
    )

    # ==========================================
    # FILTRES SUR LA DATE D'ÉCHÉANCE (DateField)
    # ==========================================
    date_echeance_start = filters.DateFilter(field_name="date_echeance__date", lookup_expr='gte', distinct=True)
    date_echeance_end = filters.DateFilter(field_name="date_echeance__date", lookup_expr='lte', distinct=True)
    date_echeance = filters.DateFilter(field_name="date_echeance__date", lookup_expr='exact', distinct=True)

    # ==========================================
    # FILTRES SUR LA DATE DE CRÉATION (DateTimeField -> Date)
    # ==========================================
    start_date = filters.DateFilter(field_name="created_at__date", lookup_expr='gte', distinct=True)
    end_date = filters.DateFilter(field_name="created_at__date", lookup_expr='lte', distinct=True)
    created_at = filters.DateFilter(field_name="created_at__date", lookup_expr='exact', distinct=True)


    class Meta:
        model = Lease
        fields = ['statut']


class ContratFilter(filters.FilterSet):
    # ==========================================
    # 1. FILTRES MULTIPLES (IN)
    # ==========================================
    statut__in = CharInFilter(field_name='statut', lookup_expr='in')
    frequence__in = CharInFilter(field_name='frequence', lookup_expr='in')

    agence_id = filters.NumberFilter(field_name="agence_id")
    agence_id__in = NumberInFilter(field_name='agence_id', lookup_expr='in')
    proprietaire_id = filters.NumberFilter(field_name='proprietaire_id')

    # ==========================================
    # 2. RANGES DE DATES (_start et _end)
    # ==========================================
    # Date de début
    date_debut_start = filters.DateFilter(field_name="date_debut", lookup_expr='gte')
    date_debut_end = filters.DateFilter(field_name="date_debut", lookup_expr='lte')

    # Date de fin
    date_fin_start = filters.DateFilter(field_name="date_fin", lookup_expr='gte')
    date_fin_end = filters.DateFilter(field_name="date_fin", lookup_expr='lte')

    # Prochaine échéance
    prochaine_echeance_start = filters.DateFilter(field_name="prochaine_echeance__date", lookup_expr='gte')
    prochaine_echeance_end = filters.DateFilter(field_name="prochaine_echeance__date", lookup_expr='lte')

    # ==========================================
    # 3. FILTRES FINANCIERS (Bonus recommandé)
    # ==========================================
    # Très utile pour chercher les contrats qui ont presque fini de payer, ou les gros contrats
    montant_restant_min = filters.NumberFilter(field_name="montant_restant", lookup_expr='gte')
    montant_restant_max = filters.NumberFilter(field_name="montant_restant", lookup_expr='lte')

    # ==========================================
    # 4. RELATIONNELS
    # ==========================================
    type_contrat_id = filters.NumberFilter(field_name="type_contrat_id")

    class Meta:
        model = Contrat
        # La liste "fields" permet de générer automatiquement les filtres d'égalité stricte
        # Ex: ?statut=ACTIF ou ?frequence=JOURNALIER
        fields = [
            'statut',
            'frequence',
            'date_debut',
            'date_fin',
            'prochaine_echeance',
            'montant_total',
            'montant_par_paiement',
            'montant_restant',
        ]


class PaiementFilter(filters.FilterSet):
    # ==========================================
    # 1. FILTRES MULTIPLES (IN)
    # ==========================================
    statut__in = CharInFilter(field_name='statut', lookup_expr='in')
    methode__in = CharInFilter(field_name='methode', lookup_expr='in')

    agence_id = filters.NumberFilter(field_name="agence_id")
    agence_id__in = NumberInFilter(field_name='agence_id', lookup_expr='in')

    # ==========================================
    # 2. RANGES DE DATES (_start et _end)
    # ==========================================
    # Filtrer par date effective du paiement (utilise __date car c'est un DateTimeField)
    date_paiement_start = filters.DateFilter(field_name="date_paiement__date", lookup_expr='gte')
    date_paiement_end = filters.DateFilter(field_name="date_paiement__date", lookup_expr='lte')
    date_paiement = filters.DateFilter(field_name="date_paiement__date", lookup_expr='exact')

    # Filtrer par date de création dans le système
    created_at_start = filters.DateFilter(field_name="created_at__date", lookup_expr='gte')
    created_at_end = filters.DateFilter(field_name="created_at__date", lookup_expr='lte')

    # ==========================================
    # 3. FILTRES FINANCIERS (Montants)
    # ==========================================
    montant_min = filters.NumberFilter(field_name="montant", lookup_expr='gte')
    montant_max = filters.NumberFilter(field_name="montant", lookup_expr='lte')

    # ==========================================
    # 4. RELATIONNELS ET JOIN
    # ==========================================
    contrat_id = filters.NumberFilter(field_name="contrat_id")
    lease_id = filters.NumberFilter(field_name="lease_id")
    session_id = filters.NumberFilter(field_name="session_id")
    enregistre_par_id = filters.NumberFilter(field_name="enregistre_par_id")
    chauffeur_id = filters.NumberFilter(field_name="contrat__chauffeur_id")

    class Meta:
        model = Paiement
        # Les filtres stricts générés automatiquement
        fields = [
            'statut',
            'methode',
            'est_annule',
        ]


class SessionPaiementFilter(filters.FilterSet):
    # ==========================================
    # 1. FILTRES MULTIPLES (IN) ET RECHERCHE PARTIELLE
    # ==========================================
    statut__in = CharInFilter(field_name='statut', lookup_expr='in')
    reference__icontains = filters.CharFilter(field_name='reference', lookup_expr='icontains')
    telephone__icontains = filters.CharFilter(field_name='telephone', lookup_expr='icontains')
    montant_total_min = filters.NumberFilter(field_name="montant_total", lookup_expr='gte')
    montant_total_max = filters.NumberFilter(field_name="montant_total", lookup_expr='lte')

    agence_id = filters.NumberFilter(field_name="agence_id")
    agence_id__in = NumberInFilter(field_name='agence_id', lookup_expr='in')
    proprietaire_id = filters.NumberFilter(field_name='proprietaire_id')
    canal__in = CharInFilter(field_name='canal', lookup_expr='in')
    operateur__in = CharInFilter(field_name='operateur', lookup_expr='in')


    date_validation_start = filters.DateFilter(field_name="date_validation__date", lookup_expr='gte')
    date_validation_end = filters.DateFilter(field_name="date_validation__date", lookup_expr='lte')
    date_validation = filters.DateFilter(field_name="date_validation__date", lookup_expr='exact')



    class Meta:
        model = SessionPaiement
        fields = [
            'statut',
            'canal',
            'operateur',
            'reference',
            'gateway_reference',
            'telephone',
        ]


class ReglePenaliteFilter(filters.FilterSet):
    # ==========================================
    # 1. FILTRES MULTIPLES (IN)
    # ==========================================
    frequence__in = CharInFilter(field_name='frequence', lookup_expr='in')

    # ==========================================
    # 2. FILTRES FINANCIERS
    # ==========================================
    montant_min = filters.NumberFilter(field_name="montant", lookup_expr='gte')
    montant_max = filters.NumberFilter(field_name="montant", lookup_expr='lte')

    # ==========================================
    # 3. RANGES DE DATES (debut est un DateTimeField)
    # ==========================================
    debut_start = filters.DateFilter(field_name="debut__date", lookup_expr='gte')
    debut_end = filters.DateFilter(field_name="debut__date", lookup_expr='lte')
    debut = filters.DateFilter(field_name="debut__date", lookup_expr='exact')

    class Meta:
        model = ReglePenalite
        fields = [
            'frequence',
            'occurrences',
        ]


class PenaliteFilter(filters.FilterSet):
    # ==========================================
    # 1. FILTRES MULTIPLES (IN)
    # ==========================================
    statut__in = CharInFilter(field_name='statut', lookup_expr='in')

    # ==========================================
    # 2. FILTRES FINANCIERS
    # ==========================================
    montant_min = filters.NumberFilter(field_name="montant", lookup_expr='gte')
    montant_max = filters.NumberFilter(field_name="montant", lookup_expr='lte')

    # ==========================================
    # 3. RANGES DE DATES (date_application = DateTimeField)
    # ==========================================
    date_application_start = filters.DateFilter(field_name="date_application__date", lookup_expr='gte')
    date_application_end = filters.DateFilter(field_name="date_application__date", lookup_expr='lte')
    date_application = filters.DateFilter(field_name="date_application__date", lookup_expr='exact')

    created_at_start = filters.DateFilter(field_name="created_at__date", lookup_expr='gte')
    created_at_end = filters.DateFilter(field_name="created_at__date", lookup_expr='lte')


    # 🚀 ASTUCE : Permet au Front-End de récupérer toutes les pénalités d'un contrat
    # entier en traversant la relation : Penalite -> Lease -> Contrat
    contrat_id = filters.NumberFilter(field_name="lease__contrat_id")

    class Meta:
        model = Penalite
        fields = [
            'statut',
        ]


class CompteReceptionProprietaireFilter(filters.FilterSet):
    # NumberFilter explicite : un ModelChoiceFilter auto-généré depuis le
    # FK 'proprietaire' validerait contre Proprietaire.objects.all() (tous
    # comptes confondus), ce qui permettrait de deviner par la différence
    # 400/200 qu'un identifiant existe chez un autre partenaire.
    proprietaire_id = filters.NumberFilter(field_name='proprietaire_id')

    class Meta:
        model = CompteReceptionProprietaire
        fields = [
            'operateur',
            'actif',
        ]


class PreuvePaiementUSSDFilter(filters.FilterSet):
    # ==========================================
    # 1. FILTRES MULTIPLES (IN)
    # ==========================================
    statut__in = CharInFilter(field_name='statut', lookup_expr='in')
    operateur__in = CharInFilter(field_name='operateur', lookup_expr='in')

    session_reference = filters.CharFilter(
        field_name='session__reference', lookup_expr='icontains'
    )
    proprietaire_id = filters.NumberFilter(
        field_name='session__proprietaire_id'
    )

    # ==========================================
    # 2. RANGES DE DATES
    # ==========================================
    created_at_start = filters.DateFilter(field_name="created_at__date", lookup_expr='gte')
    created_at_end = filters.DateFilter(field_name="created_at__date", lookup_expr='lte')

    verifie_le_start = filters.DateFilter(field_name="verifie_le__date", lookup_expr='gte')
    verifie_le_end = filters.DateFilter(field_name="verifie_le__date", lookup_expr='lte')

    class Meta:
        model = PreuvePaiementUSSD
        fields = [
            'statut',
            'operateur',
        ]
