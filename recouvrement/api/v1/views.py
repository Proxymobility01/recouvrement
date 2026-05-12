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
from rest_framework.exceptions import ValidationError
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.filters import LeaseFilter, ContratFilter, PaiementFilter
from core.pagination import StandardResultsSetPagination
from core.permissions import StrictDjangoModelPermissions
from core.views import TenantModelViewSet
from core.exceptions import CustomAPIException
from core.errors import ErrorCodes
from .serializers import ContratSerializer, LeaseSerializer, InitiationPaiementSerializer, PaiementSerializer, \
    CalendrierSerializer, TypeContratSerializer, SousContratSerializer, ParametreSerializer
from ...models import Contrat, Paiement, Lease, SessionPaiement, TypeContrat, Parametre
from ...services import MobilePaymentService

logger = logging.getLogger(__name__)


class ContratViewSet(TenantModelViewSet):
    queryset = Contrat.objects.all()
    serializer_class = ContratSerializer
    permission_classes = [IsAuthenticated, StrictDjangoModelPermissions]
    pagination_class = StandardResultsSetPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter, DjangoFilterBackend]
    filterset_class = ContratFilter
    search_fields = ['reference', 'vin', 'immatriculation', 'chauffeur__nom_complet','enregistre_par__nom_complet']
    ordering_fields = ['created_at', 'statut',]
    ordering = ['-created_at']
    def get_queryset(self):
        qs = super().get_queryset().select_related('chauffeur', 'enregistre_par','type_contrat')
        user = self.request.user

        if user.is_superuser or user.has_perm('recouvrement.view_all_contrats'):
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
        Suppression autorisée UNIQUEMENT si le contrat est "vierge" (aucune dépendance).
        """
        raisons_blocage = []

        # 1. Vérification des sous-contrats (S'il est parent, il protège ses enfants)
        if instance.sous_contrats.exists():
            raisons_blocage.append(f"{instance.sous_contrats.count()} sous-contrat(s) rattaché(s)")

        # 2. Vérification des échéances (Leases)
        if hasattr(instance, 'leases') and instance.leases.exists():
            raisons_blocage.append(f"{instance.leases.count()} échéance(s) générée(s)")

        # 3. Vérification des paiements (grâce à ton related_name="paiements")
        if hasattr(instance, 'paiements') and instance.paiements.exists():
            raisons_blocage.append(f"{instance.paiements.count()} paiement(s) effectué(s)")

        # S'il y a la moindre dépendance, on déclenche l'erreur avec le détail précis
        if raisons_blocage:
            details = " et ".join(raisons_blocage)
            raise CustomAPIException(
                resp_code=ErrorCodes.CONTRACT_DELETE_FORBIDDEN,
                status_code=403,
                context=f"Impossible de supprimer le contrat {instance.reference}. Il est déjà lié à : {details}."
            )

        # Si la liste est vide, c'est que le contrat est "vierge". On peut le supprimer !
        super().perform_destroy(instance)

    @action(detail=False, methods=['get'], url_path='impayes-du-jour')
    def impayes_du_jour(self, request):
        """
        Retourne TOUS les contrats parents (véhicules) ayant un impayé aujourd'hui,
        que ce soit sur le contrat lui-même ou sur l'un de ses sous-contrats.
        Bypass la pagination pour un traitement de masse par le Front-End.
        """
        aujourdhui = timezone.now().date()
        user = request.user
        if not user.is_superuser:
            raise CustomAPIException(
                resp_code=ErrorCodes.PERMISSION_DENIED,  # Assure-toi d'avoir ce code dans tes erreurs
                status_code=403,
                context="Accès refusé. Cette action est strictement réservée au Super-Administrateur."
            )

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

    @action(detail=True, methods=['post'], url_path='sous-contrats')
    def sous_contrats(self, request, pk=None):
        """
        Ajoute un nouveau sous-contrat (accessoire) à un véhicule existant.
        Endpoint: POST /api/v1/contrats/{id}/sous-contrats/
        """
        # 1. Récupération du contrat parent (la moto)
        parent_contrat = self.get_object()

        # 2. SÉCURITÉ : Empêcher d'ajouter un sous-contrat à un autre sous-contrat
        if parent_contrat.parent is not None:
            raise CustomAPIException(
                resp_code=ErrorCodes.INVALID_REQUEST,
                status_code=400,
                context="Vous ne pouvez ajouter un sous-contrat qu'à un contrat principal."
            )

        # 3. Validation des données envoyées par le Front-End
        serializer = SousContratSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        sc_instance_data = serializer.validated_data

        # 4. Héritage automatique des données du parent (ADN du contrat)
        sc_instance_data['parent'] = parent_contrat
        sc_instance_data['chauffeur'] = parent_contrat.chauffeur
        sc_instance_data['compte_id'] = parent_contrat.compte_id
        sc_instance_data['nom_complet'] = parent_contrat.nom_complet

        # Traçabilité
        sc_instance_data['enregistre_par'] = request.user

        # Initialisation
        sc_instance_data['statut'] = Contrat.STATUT_ACTIF
        sc_instance_data['montant_restant'] = sc_instance_data.get('montant_total')

        # 5. Enregistrement en base de données
        sous_contrat = Contrat.objects.create(**sc_instance_data)

        # 6. Réponse pour le Front-End
        return Response({
            "message": "Sous-contrat ajouté avec succès.",
            "id": sous_contrat.id,
            "reference": sous_contrat.reference,
            "parent_id": parent_contrat.id
        }, status=status.HTTP_201_CREATED)


class LeaseViewSet(TenantModelViewSet):
    """
    API pour lister et consulter le détail des échéances (Leases).
    Supporte le filtrage par statut et par date.
    """
    http_method_names = ['get', 'head', 'options']
    queryset = Lease.objects.all()
    serializer_class = LeaseSerializer
    pagination_class = StandardResultsSetPagination
    permission_classes = [IsAuthenticated, StrictDjangoModelPermissions]
    filterset_class = LeaseFilter

    # Configuration des filtres pour le tableau de bord
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter, filters.SearchFilter]
    search_fields = ['nom_complet_search']
    ordering_fields = ['date_echeance', 'created_at']
    ordering = ['-date_echeance']

    def get_queryset(self):
        # 1. Isolation par compte_id (Automatique via TenantModelViewSet)
        qs = super().get_queryset().select_related('contrat__type_contrat')
        user = self.request.user
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


class InitiationPaiementView(GenericAPIView):
    """
    Vue dédiée à l'initiation d'un paiement Mobile Money (Supporte le paiement par Lot/Batch).
    Endpoint: POST /api/v1/initier-paiement/
    """
    permission_classes = [IsAuthenticated,StrictDjangoModelPermissions]
    queryset = SessionPaiement.objects.all()

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
                        session=session,
                        contrat=ligne['lease_id'].contrat,
                        lease=ligne['lease_id'],
                        enregistre_par=request.user,
                        compte_id=request.user.compte_id,
                        montant=ligne['montant'],
                        methode=Paiement.METHODE_MOBILE_MONEY,
                        reference=Paiement.generer_reference_paiement(Paiement.METHODE_MOBILE_MONEY)
                    )
        except Exception as e:
            logger.error("Erreur préparation panier", exc_info=True)
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
    permission_classes = [IsAuthenticated, StrictDjangoModelPermissions]

    filter_backends = [DjangoFilterBackend, filters.OrderingFilter, filters.SearchFilter]
    filter_class = PaiementFilter
    search_fields = ['reference', 'nom_complet_search', '^session__telephone',]
    ordering_fields = ['date_paiement', 'created_at']
    ordering = ['-date_paiement']

    def get_queryset(self):
        qs = super().get_queryset().select_related('enregistre_par', 'contrat', 'lease')
        user = self.request.user

        if user.is_superuser or  user.has_perm('recouvrement.view_all_paiements'):
            return qs

        return qs.filter(Q(contrat__chauffeur=user) | Q(enregistre_par=user))

    def perform_create(self, serializer):
        serializer.save(enregistre_par=self.request.user)

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
    permission_classes = [IsAuthenticated, StrictDjangoModelPermissions]

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

    def perform_create(self, serializer):
        """
        Injecte automatiquement le compte_id de l'utilisateur connecté
        lors de la création du type de contrat.
        """
        # On récupère le compte_id depuis l'utilisateur qui fait la requête
        serializer.save(compte_id=self.request.user.compte_id)
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        return Response({
            "message": "Type de contrat créé avec succès.",
            "id": serializer.instance.id,
            "code": serializer.instance.code
        }, status=status.HTTP_201_CREATED)

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


class ParametreViewSet(TenantModelViewSet):
    """
    Gestion de la configuration de l'entreprise (ex: Jours de repos).
    Une seule instance autorisée par entreprise (compte_id).
    """
    queryset = Parametre.objects.all()
    serializer_class = ParametreSerializer
    permission_classes = [IsAuthenticated,StrictDjangoModelPermissions]

    def get_queryset(self):
        # 💡 Optionnel : Si ton TenantModelViewSet ne le fait pas déjà,
        # on s'assure que l'utilisateur ne voit que les params de son compte.
        return super().get_queryset().filter(compte_id=self.request.user.compte_id)

    def perform_create(self, serializer):
        compte_id = self.request.user.compte_id

        # 🚀 SÉCURITÉ SINGLETON : On empêche de créer une 2ème ligne de paramètre
        if Parametre.objects.filter(compte_id=compte_id).exists():
            raise ValidationError({
                "detail": "Les paramètres existent déjà pour cette entreprise. Veuillez faire une mise à jour (PATCH/PUT) sur l'ID existant."
            })

        # Enregistrement avec le compte de l'utilisateur
        serializer.save(compte_id=compte_id)

    def perform_destroy(self, instance):
        # 💡 Optionnel : Tu peux interdire la suppression si tu veux forcer
        # l'entreprise à juste vider la liste [] plutôt que de supprimer la ligne.
        # super().perform_destroy(instance)

        # Si tu préfères interdire la suppression :
        raise ValidationError({
            "detail": "La suppression des paramètres globaux est interdite. Vous pouvez simplement vider les jours de repos."
        })