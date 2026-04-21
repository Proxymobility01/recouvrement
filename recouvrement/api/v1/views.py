# Create your views here.
import hashlib
import hmac
import json
import logging
from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated, DjangoModelPermissions, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


from core.views import TenantModelViewSet
from core.exceptions import CustomAPIException
from core.errors import ErrorCodes
from .serializers import ContratSerializer, LeaseSerializer, InitiationPaiementSerializer, PaiementSerializer, \
    CalendrierSerializer
from ...models import Contrat, Paiement, Lease
from ...services import MobilePaymentService

logger = logging.getLogger(__name__)


class ContratViewSet(TenantModelViewSet):
    queryset = Contrat.objects.all()
    serializer_class = ContratSerializer
    permission_classes = [IsAuthenticated, DjangoModelPermissions]
    def get_queryset(self):
        qs = super().get_queryset().select_related('chauffeur', 'enregistre_par')
        user = self.request.user
        if user.is_superuser:
            return qs
        if user.has_perm('recouvrement.view_all_contrats'):
            return qs
        return qs.filter(Q(chauffeur=user) | Q(enregistre_par=user))

    def perform_create(self, serializer):
        """
        Logique exécutée juste avant l'insertion en base de données.
        """
        # On injecte silencieusement l'utilisateur connecté comme auteur (Traçabilité)
        # On le met dans validated_data avant d'appeler la sauvegarde finale.
        serializer.validated_data['enregistre_par'] = self.request.user

        # On appelle le parent (TenantModelViewSet) qui va se charger
        # de faire le vrai '.save()' en injectant le 'compte_id' de manière sécurisée.
        super().perform_create(serializer)

    def perform_destroy(self, instance):
        """
        Logique exécutée lors d'une requête DELETE.
        """
        # Sécurité Financière : On vérifie s'il y a des paiements valides attachés.
        # (On utilise la constante STATUT_VALIDE de ton modèle Paiement)
        if instance.paiements.filter(statut=Paiement.STATUT_VALIDE).exists():
            raise CustomAPIException(
                dev_msg=f"Tentative de suppression du contrat {instance.id} bloquée (paiements existants).",
                resp_code=ErrorCodes.ACCESS_DENIED,  # Ou un code comme CONTRACT_HAS_PAYMENTS
                status_code=400,
                usr_msg="Suppression impossible : ce contrat contient déjà des paiements validés. Veuillez utiliser le statut 'Annulé' ou 'Soldé'."
            )

        instance.delete()


class LeaseViewSet(TenantModelViewSet):
    """
    API pour lister et consulter le détail des échéances (Leases).
    Supporte le filtrage par statut et par date.
    """
    queryset = Lease.objects.all()
    serializer_class = LeaseSerializer
    permission_classes = [IsAuthenticated, DjangoModelPermissions]

    # Configuration des filtres pour le tableau de bord
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter, filters.SearchFilter]
    filterset_fields = ['statut', 'date_echeance', 'contrat']
    search_fields = ['contrat__nom_complet']
    ordering_fields = ['date_echeance', 'created_at']
    ordering = ['-date_echeance']

    def get_queryset(self):
        # 1. Isolation par compte_id (Automatique via TenantModelViewSet)
        qs = super().get_queryset().select_related('contrat')
        user = self.request.user

        # 2. Gestion des droits d'accès
        if user.is_superuser or user.has_perm('recouvrement.view_all_leases'):
            return qs

        # 3. Un chauffeur ne voit que ses propres échéances
        return qs.filter(contrat__chauffeur=user)

    @action(detail=False, methods=['get'], url_path='calendrier')
    def calendrier(self, request):
        # 1. Requête optimisée
        qs = self.get_queryset().select_related('contrat', 'contrat__chauffeur')

        mois = request.query_params.get('mois')
        annee = request.query_params.get('annee')
        chauffeur_id = request.query_params.get('chauffeur_id')

        if mois and annee:
            try:
                qs = qs.filter(date_echeance__year=int(annee), date_echeance__month=int(mois))
            except ValueError:
                return Response({"error": "Date invalide."}, status=400)

        if chauffeur_id:
            qs = qs.filter(contrat__chauffeur_id=chauffeur_id)

        # 2. On trie tout par date chronologique pour que les tableaux finaux soient dans le bon ordre
        leases = qs.order_by('date_echeance')

        # 3. On sérialise toutes les données d'un seul coup
        leases_data = CalendrierSerializer(leases, many=True).data

        # 4. Regroupement intelligent en Python
        # defaultdict crée automatiquement la structure si le chauffeur n'existe pas encore dans le dict
        groupement = defaultdict(lambda: {"payees": [], "impayees": []})

        for item in leases_data:
            # On retire (pop) le nom du chauffeur de l'objet, car on va l'utiliser comme titre du groupe
            nom_chauffeur = item.pop('chauffeur_nom', 'Inconnu')

            # On range l'échéance dans le bon tiroir
            if item['statut'] == Lease.STATUT_PAYE:
                groupement[nom_chauffeur]["payees"].append(item)
            else:
                groupement[nom_chauffeur]["impayees"].append(item)

        # 5. Transformation en tableau propre pour le Front-End
        resultat_final = [
            {
                "chauffeur_nom": nom,
                "payees": donnees["payees"],
                "impayees": donnees["impayees"]
            }
            for nom, donnees in groupement.items()
        ]

        # Optionnel : Trier alphabétiquement par nom de chauffeur pour un affichage plus joli
        resultat_final.sort(key=lambda x: x["chauffeur_nom"])

        return Response(resultat_final)


class InitiationPaiementView(APIView):
    """
    Vue dédiée EXCLUSIVEMENT à l'initiation d'un paiement Mobile Money.
    Endpoint: POST /api/v1/initier-paiement/
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        # 1. Validation des données d'entrée
        serializer = InitiationPaiementSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        lease = serializer.validated_data['lease_id']
        montant = serializer.validated_data['montant']
        phone_number = serializer.validated_data.get('phone_number')

        # 2. Génération de la référence d'audit interne
        reference_interne = Paiement.generer_reference_paiement(Paiement.METHODE_MOBILE_MONEY)

        # 3. 🛡️ SAUVEGARDE LOCALE D'ABORD (Garantit qu'on garde une trace quoi qu'il arrive)
        paiement = Paiement.objects.create(
            contrat=lease.contrat,
            lease=lease,
            utilisateur=request.user,
            compte_id=request.user.compte_id,
            montant=montant,
            methode=Paiement.METHODE_MOBILE_MONEY,
            reference=reference_interne,
            statut=Paiement.STATUT_EN_ATTENTE  # Reste en attente
        )

        # 4. APPEL AU FOURNISSEUR MOBILE MONEY
        try:
            pay_response = MobilePaymentService.initier_checkout(
                montant=montant,
                external_reference=reference_interne,
                phone_number=phone_number
            )

            # Mise à jour avec les infos de l'opérateur
            paiement.transaction_id = pay_response.get('transaction_id')
            paiement.save(update_fields=['transaction_id'])

            # 5. Réponse de succès au Front-End
            return Response({
                "success": True,
                "message": "Session de paiement créée avec succès.",
                "paiement_id": paiement.id,
                "reference": paiement.reference,
                "redirect_url": pay_response.get('redirect_url'),
                "session_token": pay_response.get('session_token')
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            # 🚨 SI L'API MOBILE MONEY PLANTE (ex: Orange/MTN est hors ligne)
            # On passe notre trace locale en ECHEC pour que la compta soit propre
            paiement.statut = Paiement.STATUT_ECHEC
            paiement.save(update_fields=['statut'])

            return Response(
                {"error": "Le service de paiement mobile est temporairement indisponible.", "details": str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )


class MobilePaymentWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        signature_recue = request.headers.get('X-Signature')
        body_brut = request.body

        if not signature_recue:
            return Response({"error": "Missing signature"}, status=400)

        # 🚀 PROPRETÉ : Lecture sécurisée des settings sans os.getenv
        secret = getattr(settings, 'PAYMENT_WEBHOOK_SECRET', '')

        signature_calculee = hmac.new(
            secret.encode('utf-8'),
            body_brut,
            hashlib.sha1
        ).hexdigest()

        if not hmac.compare_digest(signature_recue, signature_calculee):
            return Response({"error": "Invalid signature"}, status=403)

        try:
            payload = json.loads(body_brut)
        except json.JSONDecodeError:
            return Response({"error": "Invalid JSON"}, status=400)

        transaction_id = payload.get('transaction_id')
        external_reference = payload.get('external_reference')
        statut_gateway = payload.get('status')

        try:
            # 🚀 PERFORMANCE : select_related évite 2 requêtes SQL supplémentaires plus bas
            paiement = Paiement.objects.select_related('contrat', 'lease').get(reference=external_reference)
        except Paiement.DoesNotExist:
            return Response({"error": "Payment not found"}, status=404)

        if paiement.statut in [Paiement.STATUT_VALIDE, Paiement.STATUT_ECHEC]:
            return Response({"status": "Already processed"}, status=200)

        with transaction.atomic():
            paiement.webhook_payload = payload

            if statut_gateway == "SUCCESS":
                paiement.statut = Paiement.STATUT_VALIDE
                paiement.date_paiement = timezone.now()
                paiement.transaction_id = transaction_id
                paiement.save()

                lease = paiement.lease
                lease.montant_paye += paiement.montant
                lease.statut = Lease.STATUT_PAYE if lease.montant_paye >= lease.montant_attendu else Lease.STATUT_PARTIEL
                lease.save()

                contrat = paiement.contrat
                # 🚀 INTÉGRITÉ FINANCIÈRE : Bloque à zéro et solde automatiquement
                contrat.montant_restant = max(contrat.montant_restant - paiement.montant, Decimal('0.00'))

                if contrat.montant_restant == 0:
                    contrat.statut = Contrat.STATUT_SOLDE  # Adapte selon tes constantes

                contrat.save()

            else:
                paiement.statut = Paiement.STATUT_ECHEC
                paiement.save()

        return Response({"status": "Webhook acknowledged"}, status=200)


class PaiementViewSet(TenantModelViewSet):
    """
    API unifiée de consultation et de caisse :
    - GET : Liste TOUS les paiements (Espèces + Mobile Money)
    - POST : Enregistre uniquement des paiements en ESPÈCES
    - PUT/PATCH : Permet la correction comptable des paiements en ESPÈCES
    """
    serializer_class = PaiementSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user

        # On récupère tous les paiements (plus de filtre sur la méthode)
        queryset = Paiement.objects.filter(
            compte_id=user.compte_id,
        ).select_related('utilisateur', 'contrat', 'lease')

        # Isolation de sécurité pour les chauffeurs
        if not (user.is_staff or user.has_perm('recouvrement.view_all_paiements')):
            queryset = queryset.filter(contrat__chauffeur=user)

        # On trie du plus récent au plus ancien (très important pour un journal de caisse)
        # Utilise 'date_paiement' ou 'created_at' selon les champs de ton BaseModel
        return queryset.order_by('-created_at')

    def create(self, request, *args, **kwargs):
        """Création d'un paiement en espèces."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Le serializer injecte METHODE_ESPECES et s'occupe des transactions
        serializer.save()

        headers = self.get_success_headers(serializer.data)
        return Response({
            "success": True,
            "message": "Le paiement en espèces a été enregistré et validé avec succès.",
            "data": serializer.data
        }, status=status.HTTP_201_CREATED, headers=headers)

    def update(self, request, *args, **kwargs):
        """
        Surcharge de la mise à jour pour garantir que la réponse JSON
        a exactement la même structure que la création.
        """
        partial = kwargs.pop('partial', False)
        instance = self.get_object()

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)

        # Le serializer s'occupe du blocage si c'est du Mobile Money
        # et du recalcul des dettes.
        self.perform_update(serializer)

        return Response({
            "success": True,
            "message": "Le paiement a été corrigé avec succès.",
            "data": serializer.data
        }, status=status.HTTP_200_OK)

    def perform_destroy(self, instance):
        """Protection absolue contre la suppression des traces financières."""
        raise PermissionDenied(
            "Un paiement ne peut pas être supprimé de la base de données. "
            "En cas d'erreur grave, veuillez utiliser la procédure d'annulation."
        )