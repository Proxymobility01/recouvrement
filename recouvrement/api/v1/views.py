# Create your views here.
import hashlib
import hmac
import json
import logging
from collections import defaultdict


from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, DjangoModelPermissions, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.filters import LeaseFilter
from core.pagination import StandardResultsSetPagination
from core.views import TenantModelViewSet
from core.exceptions import CustomAPIException
from core.errors import ErrorCodes
from .serializers import ContratSerializer, LeaseSerializer, InitiationPaiementSerializer, PaiementSerializer, \
    CalendrierSerializer, TypeContratSerializer
from ...models import Contrat, Paiement, Lease, SessionPaiement, TypeContrat
from ...services import MobilePaymentService

logger = logging.getLogger(__name__)


class ContratViewSet(TenantModelViewSet):
    queryset = Contrat.objects.all()
    serializer_class = ContratSerializer
    permission_classes = [IsAuthenticated, DjangoModelPermissions]
    def get_queryset(self):
        qs = super().get_queryset().select_related('chauffeur', 'enregistre_par','type_contrat')
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
        serializer.validated_data['enregistre_par'] = self.request.user
        super().perform_create(serializer)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        data = {
            "message": "Contrats créés avec succès.",
            "id": serializer.instance.id,
            "reference": serializer.instance.reference
        }

        # On retourne le statut 201 (Created)
        return Response(data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        """
        Interdiction absolue de supprimer un contrat de la base de données.
        """
        raise CustomAPIException(
            resp_code=ErrorCodes.CONTRACT_DELETE_FORBIDDEN,
            status_code=403,
            context=f"ID Contrat={instance.id}"
        )

    @action(detail=False, methods=['get'], url_path='impayes-du-jour')
    def impayes_du_jour(self, request):
        """
        Retourne TOUS les contrats parents (véhicules) ayant un impayé aujourd'hui,
        que ce soit sur le contrat lui-même ou sur l'un de ses sous-contrats.
        Bypass la pagination pour un traitement de masse par le Front-End.
        """
        aujourdhui = timezone.now().date()
        user = request.user

        # 1. LA REQUÊTE CIBLÉE (Ultra-rapide)
        # On cherche uniquement les contrats PARENTS actifs
        contrats_impayes = Contrat.objects.filter(
            compte_id=user.compte_id,
            parent__isnull=True,  # Uniquement la moto (parent)
            statut=Contrat.STATUT_ACTIF  # Exclure les contrats soldés ou annulés
        ).filter(
            # Condition A : Le contrat parent a un impayé aujourd'hui
            (Q(leases__date_echeance=aujourdhui) & ~Q(leases__statut=Lease.STATUT_PAYE)) |

            # Condition B : Un des sous-contrats a un impayé aujourd'hui
            (Q(sous_contrats__leases__date_echeance=aujourdhui) & ~Q(sous_contrats__leases__statut=Lease.STATUT_PAYE))
        ).distinct()

        resultats = list(contrats_impayes.values(
            'id',
            'reference',
            'vin',
            'immatriculation',
            'chauffeur__nom_complet'
        ))

        # 3. RÉPONSE DIRECTE
        return Response({
            "total": len(resultats),
            "vehicules": resultats
        }, status=status.HTTP_200_OK)


class LeaseViewSet(TenantModelViewSet):
    """
    API pour lister et consulter le détail des échéances (Leases).
    Supporte le filtrage par statut et par date.
    """
    http_method_names = ['get', 'head', 'options']
    queryset = Lease.objects.all()
    serializer_class = LeaseSerializer
    pagination_class = StandardResultsSetPagination
    permission_classes = [IsAuthenticated, DjangoModelPermissions]
    filterset_class = LeaseFilter

    # Configuration des filtres pour le tableau de bord
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter, filters.SearchFilter]
    search_fields = ['nom_complet_search']
    ordering_fields = ['date_echeance', 'created_at']
    ordering = ['-date_echeance']

    def get_queryset(self):
        # 1. Isolation par compte_id (Automatique via TenantModelViewSet)
        qs = super().get_queryset().select_related('contrat')
        user = self.request.user

        if user.is_superuser:
            return qs

        # 2. Gestion des droits d'accès
        if user.is_superuser or user.has_perm('recouvrement.view_all_leases'):
            return qs

        # 3. Un chauffeur ne voit que ses propres échéances
        return qs.filter(contrat__chauffeur=user)

    def perform_destroy(self, instance):
        """
        Interdiction absolue de supprimer une échéance (Lease) générée.
        """
        raise CustomAPIException(
            resp_code=ErrorCodes.LEASE_DELETE_FORBIDDEN,
            status_code=403,
            context=f"ID Lease={instance.id}"
        )

    @action(detail=False, methods=['get'], url_path='calendrier')
    def calendrier(self, request):
        # 1. Requête optimisée
        qs = self.get_queryset().select_related('contrat', 'contrat__chauffeur')

        mois = request.query_params.get('mois')
        annee = request.query_params.get('annee')
        chauffeur_id = request.query_params.get('chauffeur_id')

        # 🚀 SÉCURITÉ : Forcer le mois en cours si non fourni pour éviter de crasher le serveur
        if not mois or not annee:
            aujourdhui = timezone.now().date()
            mois = aujourdhui.month
            annee = aujourdhui.year

        try:
            qs = qs.filter(date_echeance__year=int(annee), date_echeance__month=int(mois))
        except ValueError:
            # Remplacer par une CustomAPIException pour garder la cohérence de tes erreurs
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,  # Ou un code d'erreur dédié aux paramètres invalides
                status_code=400,
                context="Le format du mois ou de l'année est invalide (nombres attendus)."
            )

        if chauffeur_id:
            qs = qs.filter(contrat__chauffeur_id=chauffeur_id)

        # 2. On trie tout par date chronologique
        leases = qs.order_by('date_echeance')

        # 3. Sérialisation
        leases_data = CalendrierSerializer(leases, many=True).data

        # 4. Regroupement intelligent en Python
        groupement = defaultdict(lambda: {"payees": [], "impayees": []})

        for item in leases_data:
            nom_chauffeur = item.pop('chauffeur_nom', 'Inconnu')

            if item['statut'] == Lease.STATUT_PAYE:
                groupement[nom_chauffeur]["payees"].append(item)
            else:
                groupement[nom_chauffeur]["impayees"].append(item)

        # 5. Transformation pour le Front-End
        resultat_final = [
            {
                "chauffeur_nom": nom,
                "payees": donnees["payees"],
                "impayees": donnees["impayees"]
            }
            for nom, donnees in groupement.items()
        ]

        resultat_final.sort(key=lambda x: x["chauffeur_nom"])

        return Response(resultat_final)




class InitiationPaiementView(APIView):
    """
    Vue dédiée à l'initiation d'un paiement Mobile Money (Supporte le paiement par Lot/Batch).
    Endpoint: POST /api/v1/initier-paiement/
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        # 1. Validation des données d'entrée (Le Serializer valide le tableau de lignes)
        serializer = InitiationPaiementSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        lignes = serializer.validated_data['lignes']
        phone_number = serializer.validated_data.get('phone_number')

        # 2. Calcul du montant TOTAL à facturer via Mobile Money
        total_montant = sum(ligne['montant'] for ligne in lignes)

        # 3. Génération d'une référence UNIQUE pour le Ticket de caisse global (Parent)
        # En passant "SESSION", la méthode va extraire "SES" comme préfixe
        reference_session = Paiement.generer_reference_paiement("SESSION")

        # 4. 🛡️ SAUVEGARDE LOCALE : Création du Parent puis des Enfants
        try:
            with transaction.atomic():
                # A. On crée le Parent (La transaction MTN/Orange)
                session = SessionPaiement.objects.create(
                    reference=reference_session,
                    montant_total=total_montant,
                    telephone=phone_number,
                    utilisateur=request.user,
                    compte_id=request.user.compte_id,
                    statut=SessionPaiement.STATUT_EN_ATTENTE
                )

                # B. On crée les Enfants (Les reçus comptables internes)
                for ligne in lignes:
                    Paiement.objects.create(
                        session=session,  # 🔗 LE LIEN MAGIQUE EST ICI
                        contrat=ligne['lease_id'].contrat,
                        lease=ligne['lease_id'],
                        utilisateur=request.user,
                        compte_id=request.user.compte_id,
                        montant=ligne['montant'],
                        methode=Paiement.METHODE_MOBILE_MONEY,

                        # Chaque ligne garde sa propre référence unique !
                        reference=Paiement.generer_reference_paiement(Paiement.METHODE_MOBILE_MONEY)


                    )
        except Exception as e:
            return Response(
                {"error": "Erreur interne lors de la préparation du panier.", "details": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # 5. APPEL AU FOURNISSEUR MOBILE MONEY
        try:
            # 🚀 On envoie la Session (Le Parent) au fournisseur, pas les lignes !
            pay_response = MobilePaymentService.initier_checkout(
                montant=total_montant,
                external_reference=session.reference,
                phone_number=phone_number
            )

            # Succès : On met à jour UNIQUEMENT le Parent avec l'ID du fournisseur
            session.transaction_id = pay_response.get('transaction_id')
            session.save(update_fields=['transaction_id'])

            return Response({
                "success": True,
                "message": f"Session de paiement créée pour {len(lignes)} échéance(s).",
                "reference_session": session.reference,
                "montant_total": total_montant,
                "redirect_url": pay_response.get('redirect_url'),
                "session_token": pay_response.get('session_token')
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            # 🚨 ECHEC : Le fournisseur est injoignable, on passe UNIQUEMENT le Parent en ECHEC
            # Les enfants (Paiements) renverront "ECHEC" automatiquement grâce à la @property
            session.statut = SessionPaiement.STATUT_ECHEC
            session.save(update_fields=['statut'])

            return Response(
                {"error": "Le service de paiement mobile est temporairement indisponible.", "details": str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )



class WebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        signature_recue = request.headers.get('X-Signature')
        body_brut = request.body

        if not signature_recue:
            return Response({"error": "Missing signature"}, status=400)

        secret = getattr(settings, 'PAYMENT_WEBHOOK_SECRET', '').strip()

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

        # 🚀 On s'assure de mettre en majuscule pour éviter les bugs (ex: "Failed" au lieu de "FAILED")
        statut_gateway = str(payload.get('status', '')).upper()

        # Recherche de la session
        try:
            session = SessionPaiement.objects.get(reference=external_reference)
        except SessionPaiement.DoesNotExist:
            return Response({"error": "Session not found"}, status=404)

        # Idempotence : Si déjà traité, on arrête ici
        if session.statut in [SessionPaiement.STATUT_VALIDE, SessionPaiement.STATUT_ECHEC]:
            return Response({"status": "Already processed"}, status=200)

        # On garde toujours une trace de ce que PayGate a envoyé
        session.webhook_payload = payload

        # 🚀 GESTION STRICTE DES STATUTS PAYGATE
        if statut_gateway == "SUCCESS":
            session.statut = SessionPaiement.STATUT_VALIDE
            session.date_validation = timezone.now()
            session.transaction_id = transaction_id
            logger.info(f"[Webhook] SUCCÈS - Session {external_reference} validée.")
            session.save()  # Déclenche le signal de validation

        elif statut_gateway == "FAILED":
            session.statut = SessionPaiement.STATUT_ECHEC
            logger.warning(f"[Webhook] ÉCHEC - Session {external_reference} refusée par PayGate.")
            session.save()  # Déclenche le signal d'échec

        else:
            # Sécurité : Si PayGate envoie "PENDING" ou un nouveau statut inconnu
            logger.warning(
                f"[Webhook] STATUT IGNORE - Session {external_reference} a reçu un statut inattendu : {statut_gateway}")
            # On sauvegarde juste le payload, mais on ne change pas le statut "EN_ATTENTE"
            session.save(update_fields=['webhook_payload'])

        return Response({"status": "Webhook acknowledged"}, status=200)


class PaiementViewSet(TenantModelViewSet):
    queryset = Paiement.objects.all()
    serializer_class = PaiementSerializer
    permission_classes = [IsAuthenticated, DjangoModelPermissions]

    filter_backends = [DjangoFilterBackend, filters.OrderingFilter, filters.SearchFilter]
    filterset_fields = ['statut', 'methode', 'lease']
    search_fields = ['reference', 'transaction_id', 'contrat__nom_complet']
    ordering_fields = ['date_paiement', 'created_at']
    ordering = ['-date_paiement']

    def get_queryset(self):
        qs = super().get_queryset().select_related('utilisateur', 'contrat', 'lease')
        user = self.request.user

        if user.is_superuser:
            return qs
        if user.has_perm('recouvrement.view_all_paiements'):
            return qs

        return qs.filter(Q(contrat__chauffeur=user) | Q(utilisateur=user))

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        data = {
            "message": "Paiement enregistré avec succès.",
            "id": serializer.instance.id,
            "reference": serializer.instance.reference
        }

        # On retourne le statut 201 (Created)
        return Response(data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        """
        Interdiction absolue de supprimer un paiement de la base de données.
        """
        raise CustomAPIException(
            resp_code=ErrorCodes.PAYMENT_DELETE_FORBIDDEN,
            status_code=403,
            context=f"ID Paiement={instance.id}"
        )


class TypeContratViewSet(TenantModelViewSet):
    """
    API pour la gestion du dictionnaire des types de contrats (Véhicules, Accessoires...).
    """
    queryset = TypeContrat.objects.all()
    serializer_class = TypeContratSerializer
    permission_classes = [IsAuthenticated, DjangoModelPermissions]

    # Configuration des filtres et recherches
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]

    # Utile pour afficher une liste déroulante "Sélectionnez l'accessoire" (est_principal=False)
    filterset_fields = ['est_principal']

    search_fields = ['libelle', 'code']
    ordering_fields = ['libelle', 'created_at']
    ordering = ['libelle']

    def get_queryset(self):
        # Isolation multi-tenant gérée par le parent
        return super().get_queryset()

    def perform_destroy(self, instance):
        """
        Protection pour éviter de casser la base de données.
        Si un 'TypeContrat' est lié à au moins un 'Contrat', on interdit la suppression.
        """
        # "contrats" correspond au related_name='contrats' dans ton modèle Contrat
        if instance.contrats.exists():
            raise CustomAPIException(
                resp_code=ErrorCodes.TYPE_CONTRAT_DELETE_FORBIDDEN,
                status_code=403,
                context=f"Impossible de supprimer le type '{instance.libelle}' car il est utilisé par {instance.contrats.count()} contrat(s)."
            )
        super().perform_destroy(instance)