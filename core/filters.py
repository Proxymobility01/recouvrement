import django_filters
from django_filters import rest_framework as filters

from recouvrement.models import Lease


class CharInFilter(django_filters.BaseInFilter, django_filters.CharFilter):
    pass

class LeaseFilter(filters.FilterSet):


    class Meta:
        model = Lease
