import django_filters
from django_filters import rest_framework as filters

from recouvrement.models import Lease


class CharInFilter(django_filters.BaseInFilter, django_filters.CharFilter):
    pass


class LeaseFilter(filters.FilterSet):
    # Filtre sur le statut (Multiple)
    statut__in = CharInFilter(field_name='statut', lookup_expr='in')

    # ==========================================
    # FILTRES SUR LA DATE D'ÉCHÉANCE (DateField)
    # ==========================================
    date_echeance_start = filters.DateFilter(field_name="date_echeance", lookup_expr='gte', distinct=True)
    date_echeance_end = filters.DateFilter(field_name="date_echeance", lookup_expr='lte', distinct=True)
    date_echeance = filters.DateFilter(field_name="date_echeance", lookup_expr='exact', distinct=True)

    # ==========================================
    # FILTRES SUR LA DATE DE CRÉATION (DateTimeField -> Date)
    # ==========================================
    start_date = filters.DateFilter(field_name="created_at__date", lookup_expr='gte', distinct=True)
    end_date = filters.DateFilter(field_name="created_at__date", lookup_expr='lte', distinct=True)
    created_at = filters.DateFilter(field_name="created_at__date", lookup_expr='exact', distinct=True)


    class Meta:
        model = Lease
        fields = ['statut']

