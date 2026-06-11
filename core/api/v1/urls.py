
from django.urls import path

from core.api.v1.sse import sse_notifications

urlpatterns = [
    # ... tes autres routes existantes (admin, api/v1/contrats, etc.) ...

    # 🚀 LA ROUTE POUR LE FLUX TEMPS RÉEL SSE
    path('events/', sse_notifications, name='sse-notifications'),
]