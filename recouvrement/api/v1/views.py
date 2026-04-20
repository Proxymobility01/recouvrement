# Create your views here.
import hashlib
import hmac
import json
import logging
import os
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, settings, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated, DjangoModelPermissions, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.views import TenantModelViewSet
from core.exceptions import CustomAPIException
from core.errors import ErrorCodes
from .serializers import ContratSerializer, LeaseSerializer, InitiationPaiementSerializer, PaiementSerializer
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

        # On récupère les données nettoyées
        lease = serializer.validated_data['lease_id']
        montant = serializer.validated_data['montant']
        phone_number = serializer.validated_data.get('phone_number')

        # 2. Génération de la référence d'audit interne
        reference_interne = MobilePaymentService.generer_reference_paiement()

        # 3. Appel au fournisseur Mobile Money
        pay_response = MobilePaymentService.initier_checkout(
            montant=montant,
            external_reference=reference_interne,
            phone_number=phone_number
        )

        # 4. Enregistrement de l'intention (Statut: EN ATTENTE)
        paiement = Paiement.objects.create(
            contrat=lease.contrat,
            lease=lease,
            utilisateur=request.user,
            compte_id=request.user.compte_id,
            montant=montant,
            methode=Paiement.METHODE_MOBILE_MONEY,
            reference=reference_interne,
            transaction_id=pay_response.get('transaction_id'),
            statut=Paiement.STATUT_EN_ATTENTE
        )

        # 5. Réponse au Front-End
        return Response({
            "success": True,
            "message": "Session de paiement créée avec succès.",
            "paiement_id": paiement.id,
            "reference": paiement.reference,
            "redirect_url": pay_response.get('redirect_url'),
            "session_token": pay_response.get('session_token')
        }, status=status.HTTP_201_CREATED)


class MobilePaymentWebhookView(APIView):
    """
    Endpoint public destiné à recevoir les notifications du fournisseur de paiement.
    URL: POST /api/v1/webhook/paiement/
    """
    # Désactive l'authentification Keycloak pour cet endpoint (communication Server-to-Server)
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        # 1. Récupération de la signature et des données brutes
        signature_recue = request.headers.get('X-Signature')
        body_brut = request.body

        if not signature_recue:
            logger.warning("[Webhook] Requête rejetée : Signature manquante.")
            return Response({"error": "Missing signature"}, status=400)

        # 2. Vérification cryptographique de la signature (HMAC SHA-1)
        secret = str(os.getenv("PAYMENT_WEBHOOK_SECRET") or getattr(settings, "PAYMENT_WEBHOOK_SECRET", ""))

        signature_calculee = hmac.new(
            secret.encode('utf-8'),
            body_brut,
            hashlib.sha1
        ).hexdigest()

        if not hmac.compare_digest(signature_recue, signature_calculee):
            logger.warning("[Webhook] Requête rejetée : Signature invalide.")
            return Response({"error": "Invalid signature"}, status=403)

        # 3. Extraction des données
        try:
            payload = json.loads(body_brut)
        except json.JSONDecodeError:
            logger.warning("[Webhook] Requête rejetée : Format JSON invalide.")
            return Response({"error": "Invalid JSON"}, status=400)

        transaction_id = payload.get('transaction_id')
        external_reference = payload.get('external_reference')
        statut_gateway = payload.get('status')

        # 4. Recherche de l'intention de paiement
        try:
            paiement = Paiement.objects.get(reference=external_reference)
        except Paiement.DoesNotExist:
            logger.error(f"[Webhook] TX {transaction_id} : Référence inconnue ({external_reference}).")
            return Response({"error": "Payment not found"}, status=404)

        # Idempotence : ignore le traitement si déjà effectué
        if paiement.statut in [Paiement.STATUT_VALIDE, Paiement.STATUT_ECHEC]:
            logger.info(f"[Webhook] TX {transaction_id} ignorée : Déjà traitée ({paiement.statut}).")
            return Response({"status": "Already processed"}, status=200)

        # 5. Traitement transactionnel atomique
        with transaction.atomic():
            # Sauvegarde de la trace d'audit
            paiement.webhook_payload = payload

            if statut_gateway == "SUCCESS":
                # A. Validation du paiement
                paiement.statut = Paiement.STATUT_VALIDE
                paiement.date_paiement = timezone.now()
                paiement.transaction_id = transaction_id
                paiement.save()

                # B. Mise à jour de l'échéance (Lease)
                lease = paiement.lease
                lease.montant_paye += paiement.montant

                if lease.montant_paye >= lease.montant_attendu:
                    lease.statut = Lease.STATUT_PAYE
                else:
                    lease.statut = Lease.STATUT_PARTIEL
                lease.save()

                # C. Mise à jour du Contrat
                contrat = paiement.contrat
                contrat.montant_restant -= paiement.montant

                # Décalage de l'échéance uniquement si la journée est soldée
                if lease.statut == Lease.STATUT_PAYE:
                    contrat.prochaine_echeance = lease.date_echeance + timedelta(days=1)

                contrat.save()
                logger.info(f"[Webhook] SUCCÈS - Paiement {external_reference} validé. Lease: {lease.statut}.")

            else:
                # Gestion des échecs (FAILED, CANCELLED, etc.)
                paiement.statut = Paiement.STATUT_ECHEC
                paiement.save()
                logger.warning(f"[Webhook] ÉCHEC - Paiement {external_reference} refusé (Code: {payload.get('errorCode')}).")

        # 6. Acquittement (Code 200 pour stopper les retrys du fournisseur)
        return Response({"status": "Webhook acknowledged"}, status=200)


class PaiementViewSet(TenantModelViewSet):
    """
    API dédiée à l'enregistrement des paiements en ESPÈCES par les partenaires.
    """
    serializer_class = PaiementSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """
        On ne liste que les paiements en espèces de l'entreprise de l'utilisateur.
        """
        user = self.request.user
        # On filtre par compte_id (Multi-tenant) et par méthode ESPECES
        queryset = Paiement.objects.filter(
            compte_id=user.compte_id,
        ).select_related('utilisateur', 'contrat', 'lease')

        # Si ce n'est pas un admin ou un partenaire avec vue globale,
        # (sécurité supplémentaire au cas où un chauffeur accède à cette route)
        if not (user.is_staff or user.has_perm('recouvrement.view_all_paiements')):
            queryset = queryset.filter(contrat__chauffeur=user)

        return queryset

    def create(self, request, *args, **kwargs):
        """
        Création simplifiée : le Serializer s'occupe de tout le travail lourd.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Le .save() déclenche le create() du serializer qui :
        # 1. Force la méthode ESPECES
        # 2. Valide immédiatement le paiement
        # 3. Met à jour le montant restant du contrat et du lease
        serializer.save()

        # Réponse de succès standardisée pour les espèces
        headers = self.get_success_headers(serializer.data)
        return Response({
            "success": True,
            "message": "Le paiement en espèces a été enregistré et validé avec succès.",
            "data": serializer.data
        }, status=status.HTTP_201_CREATED, headers=headers)

    def perform_destroy(self, instance):
        """
        Optionnel : Si tu veux interdire la suppression pure et simple.
        """
        # On peut imaginer une règle métier qui interdit de supprimer un paiement validé
        raise PermissionDenied("Un paiement validé ne peut pas être supprimé. Utilisez une annulation.")