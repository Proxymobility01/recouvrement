# Create your views here.
import hashlib
import hmac
import json
import logging
from collections import defaultdict
from decimal import Decimal

from django_q.tasks import async_task
from django.utils.dateparse import parse_datetime
from django.db import transaction, DatabaseError, IntegrityError
from django.db.models import Q
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from accounts.models import ConfigPaiement
from core.filters import LeaseFilter, ContratFilter, PaiementFilter
from core.pagination import StandardResultsSetPagination
from core.permissions import StrictDjangoModelPermissions
from core.utils import format_phone_cm
from core.api.v1.views import TenantModelViewSet
from core.exceptions import CustomAPIException
from core.errors import ErrorCodes
from .serializers import ContratSerializer, LeaseSerializer, InitiationPaiementSerializer, PaiementSerializer, \
    CalendrierSerializer, TypeContratSerializer, SousContratSerializer, ParametreSerializer
from ...models import Contrat, Paiement, Lease, SessionPaiement, TypeContrat, Parametre
from ...services import PaymentService
from core.tasks import _schedule_next_verification

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

        try:
            self.perform_create(serializer)

        # 🛡️ 1. LE FILET DE SÉCURITÉ POUR LE 400 BAD REQUEST
        except IntegrityError as e:
            if 'uniq_contrat_parent_actif_par_chauffeur' in str(e):
                raise CustomAPIException(
                    resp_code=ErrorCodes.BAD_REQUEST,
                    status_code=400,
                    dev_message="Ce chauffeur possède déjà un contrat parent actif (Interception DB en accès concurrent)."
                )
            # Si c'est une autre erreur d'intégrité (ex: un champ null interdit en DB), on log et on renvoie une 500
            logger.exception("Erreur d'intégrité inattendue lors de la création d'un contrat")
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,
                status_code=500,
                dev_message=f"Erreur de contrainte DB : {str(e)}"
            )

        # 🚨 2. LE RESTE DES ERREURS GRAVES (500)
        except DatabaseError as e:
            logger.exception("Erreur DB globale lors de la création d'un contrat")
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,
                status_code=500,
                dev_message=f"Échec de l'insertion en base : {str(e)}"
            )

        return Response({
            "message": "Création réussie.",
            "id": serializer.instance.id,
            "reference": serializer.instance.reference
        }, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        # On vérifie s'il y a la moindre dépendance
        a_des_dependances = (
                instance.sous_contrats.exists() or
                (hasattr(instance, 'leases') and instance.leases.exists()) or
                (hasattr(instance, 'paiements') and instance.paiements.exists())
        )

        if a_des_dependances:
            raise CustomAPIException(
                resp_code=ErrorCodes.FORBIDDEN,
                status_code=403,
                dev_message="Hard delete impossible : L'instance possède des enfants (leases, paiements ou sous-contrats)."
            )

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
                resp_code=ErrorCodes.FORBIDDEN,
                status_code=403,
                dev_message="L'utilisateur n'est pas superuser."
            )

        contrats_impayes = Contrat.objects.filter(
            compte_id=user.compte_id,
            parent__isnull=True,
            statut=Contrat.STATUT_ACTIF
        ).filter(
            (Q(leases__date_echeance=aujourdhui) & ~Q(leases__statut=Lease.STATUT_PAYE)) |
            (Q(sous_contrats__leases__date_echeance=aujourdhui) & ~Q(sous_contrats__leases__statut=Lease.STATUT_PAYE))
        ).distinct()

        resultats = list(contrats_impayes.values(
            'id',
            'reference',
            'vin',
            'immatriculation',
            'chauffeur__nom_complet'
        ))

        return Response({
            "total": len(resultats),
            "vehicules": resultats
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='sous-contrats')
    def sous_contrats(self, request, pk=None):
        parent_contrat = self.get_object()

        if parent_contrat.parent is not None:
            raise CustomAPIException(
                resp_code=ErrorCodes.BAD_REQUEST,
                status_code=400,
                dev_message="Le contrat cible (parent_contrat) possède déjà un parent_id. Pas de sous-sous-contrats."
            )

        # 1. Validation des données envoyées par le Front-End
        serializer = SousContratSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        sc_instance_data = serializer.validated_data

        # 2. 🧮 LOGIQUE FINANCIÈRE : Extraction et calcul des montants
        sc_total = sc_instance_data.get('montant_total', Decimal('0.00'))
        sc_avance = sc_instance_data.get('montant_paye', Decimal('0.00'))
        montant_restant_calcule = max(Decimal('0.00'), sc_total - sc_avance)

        # 3. Héritage automatique de l'ADN du parent et injection des finances
        sc_instance_data['parent'] = parent_contrat
        sc_instance_data['chauffeur'] = parent_contrat.chauffeur
        sc_instance_data['compte_id'] = parent_contrat.compte_id
        sc_instance_data['nom_complet'] = parent_contrat.nom_complet
        sc_instance_data['enregistre_par'] = request.user

        # Injection des valeurs calculées
        sc_instance_data['montant_paye'] = sc_avance
        sc_instance_data['montant_restant'] = montant_restant_calcule

        # 4. Le statut s'adapte automatiquement (si l'avance couvre déjà tout)
        if montant_restant_calcule == 0:
            sc_instance_data['statut'] = Contrat.STATUT_SOLDE
        else:
            sc_instance_data['statut'] = Contrat.STATUT_ACTIF

        # 5. Création en base de données
        try:
            sous_contrat = Contrat.objects.create(**sc_instance_data)
        except DatabaseError as e:
            logger.exception("Erreur DB lors de l'ajout d'un sous-contrat")
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,
                status_code=500,
                dev_message=f"Échec de création du sous-contrat : {str(e)}"
            )

        # 6. Réponse Front-End
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
            resp_code=ErrorCodes.FORBIDDEN,
            status_code=403,
            dev_message=f"Hard-delete bloqué sur l'échéance {instance.id} (Règle d'intégrité financière)."
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
            mois_int = int(mois)
            annee_int = int(annee)
            qs = qs.filter(date_echeance__year=annee_int, date_echeance__month=mois_int)
        except ValueError:
            logger.warning(f"Paramètres de date invalides pour le calendrier : mois={mois}, annee={annee}")
            raise CustomAPIException(
                resp_code=ErrorCodes.BAD_REQUEST,
                status_code=400,
                dev_message="Les paramètres 'mois' et 'annee' doivent être des nombres entiers valides."
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
    permission_classes = [IsAuthenticated, StrictDjangoModelPermissions]
    queryset = SessionPaiement.objects.all()
    serializer_class = InitiationPaiementSerializer

    def post(self, request, *args, **kwargs):
        # 1. Validation des données d'entrée
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        lignes = serializer.validated_data['lignes']
        phone_number = format_phone_cm(serializer.validated_data.get('phone_number'))
        total_montant = sum(ligne['montant'] for ligne in lignes)

        # 2. Génération de la référence UNIQUE (En mémoire)
        reference_session = Paiement.generer_reference_paiement("SESSION")

        # ========================================================
        # 🛡️ ÉTAPE 1 : ENREGISTREMENT LOCAL PRÉVENTIF IMMEUDIAT
        # ========================================================
        try:
            with transaction.atomic():
                # On crée le Parent avec gateway_reference à None pour le moment
                session_locale = SessionPaiement.objects.create(
                    reference=reference_session,
                    gateway_reference=None,
                    montant_total=total_montant,
                    telephone=phone_number,
                    utilisateur=request.user,
                    compte_id=request.user.compte_id,
                    statut=SessionPaiement.STATUT_EN_ATTENTE
                )

                # On crée les Enfants (Les reçus comptables)
                for ligne in lignes:
                    Paiement.objects.create(
                        session=session_locale,
                        contrat=ligne['lease_id'].contrat,
                        lease=ligne['lease_id'],
                        enregistre_par=request.user,
                        compte_id=request.user.compte_id,
                        montant=ligne['montant'],
                        methode=Paiement.METHODE_MOBILE_MONEY,
                        reference=Paiement.generer_reference_paiement(Paiement.METHODE_MOBILE_MONEY)
                    )
        except DatabaseError as e:
            logger.exception("Erreur interne (DB) lors de la pré-sauvegarde du panier de paiement.")
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,
                status_code=500,
                dev_message=f"Échec de l'insertion transactionnelle initiale : {str(e)}"
            )

        # ========================================================
        # 🚀 ÉTAPE 2 : APPEL DE LA PASSERELLE (HORS BLOCK ATOMIC)
        # ========================================================
        try:
            resultat_paiement = PaymentService.traiter_paiement_complet(
                compte_id=request.user.compte_id,
                montant=total_montant,
                external_reference=reference_session,
                phone_number=phone_number
            )

            # Si l'appel réussit : On complète la gateway_reference reçue
            session_locale.gateway_reference = resultat_paiement.get('paygate_reference')
            session_locale.save(update_fields=['gateway_reference'])

        except CustomAPIException as exc:
            # 🚨 INTERCEPTION DES ERREURS SERVEUR PASSERELLE / TIMEOUT (Codes 500 à 599)
            if 500 <= exc.status_code <= 599:
                logger.warning(
                    f"[Incertitude Réseau] Erreur {exc.status_code} reçue de la passerelle pour {reference_session}. "
                    "La ligne est conservée localement pour alignement asynchrone."
                )

                # On enrichit le payload pour le diagnostic mais on NE CHANGE PAS le statut EN_ATTENTE
                session_locale.webhook_payload = {"status_code_initial": exc.status_code, "erreur": exc.dev_message}
                session_locale.save(update_fields=['webhook_payload'])

                # On déclenche le veilleur (Polling) plus tôt (20s au lieu de 60s) car le push USSD est peut-être parti
                try:
                    _schedule_next_verification(session_locale, 20)
                except Exception:
                    logger.exception("Impossible de planifier le polling d'urgence.")

                # On lève l'erreur standardisée demandée pour informer le Front-End
                raise CustomAPIException(
                    resp_code=ErrorCodes.MOBILE_MONEY_FAILED,
                    status_code=exc.status_code,
                    dev_message=f"La passerelle externe est instable ({exc.status_code}). Sauvegarde préservée."
                )

            # Si c'est une vraie erreur fonctionnelle 400 (ex: numéro banni ou format invalide par l'opérateur)
            session_locale.statut = SessionPaiement.STATUT_ECHEC
            session_locale.webhook_payload = {"erreur_directe": exc.dev_message}
            session_locale.save(update_fields=['statut', 'webhook_payload'])
            raise exc

        except Exception as e:
            # Pour tout crash réseau imprévu ou timeout HTTP brut (non intercepté par le service)
            logger.exception("Crash réseau imprévu ou Timeout lors du traitement du flux.")

            session_locale.webhook_payload = {"erreur_brute": str(e)}
            session_locale.save(update_fields=['webhook_payload'])

            try:
                _schedule_next_verification(session_locale, 20)
            except Exception:
                pass

            raise CustomAPIException(
                resp_code=ErrorCodes.MOBILE_MONEY_FAILED,
                status_code=503,
                dev_message=f"Incertitude totale sur la Gateway externe : {str(e)}"
            )

        # ========================================================
        # 📈 ÉTAPE 3 : TOUT EST OK — PLANIFICATION COMMUNE
        # ========================================================
        try:
            _schedule_next_verification(session_locale, 60)
            logger.info(f"[Polling] Veilleur standard activé pour la session {session_locale.reference}")
        except Exception as e:
            logger.exception(f"Impossible de planifier la vérification standard pour {session_locale.reference}.")

        # 5. Réponse de succès standard
        return Response({
            "success": True,
            "message": "Demande de paiement envoyée avec succès.",
            "reference_interne": session_locale.reference,
            "gateway_reference": session_locale.gateway_reference,
            "montant_total": total_montant,
        }, status=status.HTTP_201_CREATED)


class WebhookView(APIView):
    """
    Endpoint public de réception des notifications (Webhooks) de PayGate.
    Valide la signature et délègue le traitement lourd à Django Q2.
    """
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        # 1. Récupération des données brutes
        signature_recue = request.headers.get('X-Signature')
        body_brut = request.body

        if not signature_recue:
            logger.warning("[Webhook] Requête rejetée : X-Signature manquante.")
            return Response({"error": "Missing signature"}, status=400)

        # 2. Parsing du payload
        try:
            payload = json.loads(body_brut)
        except json.JSONDecodeError as e:
            raise CustomAPIException(
                resp_code=ErrorCodes.INVALID_PAYLOAD,
                status_code=400,
                dev_message=f"Payload JSON malformé : {str(e)}"
            )

        external_reference = payload.get('external_reference')
        gateway_reference = payload.get('reference')
        statut_gateway = str(payload.get('status', '')).upper()

        if not external_reference:
            return Response({"error": "Missing external_reference"}, status=400)

        # 3. Recherche du panier (Session)
        try:
            session = SessionPaiement.objects.get(reference=external_reference)
        except SessionPaiement.DoesNotExist:
            logger.warning(f"[Webhook] Référence introuvable : {external_reference}")
            raise CustomAPIException(
                resp_code=ErrorCodes.NOT_FOUND,
                status_code=404,
                dev_message=f"SessionPaiement introuvable pour la référence : {external_reference}"
            )

        # 4. SÉCURITÉ : Validation HMAC SHA-256 Multi-Tenant
        config = ConfigPaiement.objects.filter(compte_id=session.compte_id).first()

        if not config or not getattr(config, 'webhook_secret', None):
            logger.error(f"[Webhook] Configuration manquante pour le compte {session.compte_id}.")
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,
                status_code=500,
                dev_message=f"Le webhook_secret est manquant ou non configuré en BDD pour le compte_id {session.compte_id}."
            )

        secret = config.webhook_secret.strip()

        signature_calculee = hmac.new(
            secret.encode('utf-8'),
            body_brut,
            hashlib.sha256  # 🚀 Sécurité moderne
        ).hexdigest()

        if not hmac.compare_digest(signature_recue, signature_calculee):
            logger.critical(f"[Webhook] FRAUDE DÉTECTÉE sur la session {external_reference}.")
            raise CustomAPIException(
                resp_code=ErrorCodes.FORBIDDEN,
                status_code=403,
                dev_message="La signature reçue ne correspond pas à la signature calculée (HMAC SHA256 Mismatch)."
            )

        # 5. IDEMPOTENCE : Protection contre les retries de la passerelle
        if session.statut in [
            SessionPaiement.STATUT_VALIDE,
            SessionPaiement.STATUT_ECHEC,
            SessionPaiement.STATUT_ANNULE
        ]:
            logger.info(f"[Webhook] Session {external_reference} ignorée (Déjà {session.statut}).")
            return Response({"status": "Already processed"}, status=200)

        # Sauvegarde des traces d'audit
        session.webhook_payload = payload
        if gateway_reference and not session.gateway_reference:
            session.gateway_reference = gateway_reference

        # ==========================================
        # 6. ROUTAGE ET DÉLÉGATION À DJANGO Q2
        # ==========================================
        try:
            # A. Mise à jour de l'état global du panier
            if statut_gateway == "SUCCESS":
                session.statut = SessionPaiement.STATUT_VALIDE
                confirmed_at_str = payload.get('confirmed_at')
                if confirmed_at_str:
                    parsed_date = parse_datetime(confirmed_at_str)
                    session.date_validation = parsed_date if parsed_date else timezone.now()
                else:
                    session.date_validation = timezone.now()
                session.save()

            elif statut_gateway == "FAILED":
                session.statut = SessionPaiement.STATUT_ECHEC
                session.save()

            elif statut_gateway == "CANCELED":
                session.statut = SessionPaiement.STATUT_ANNULE
                session.save()

            else:
                # Si PayGate envoie PENDING ou un statut inconnu
                logger.info(f"[Webhook] Statut {statut_gateway} reçu, attente de l'état final.")
                session.save(update_fields=['webhook_payload', 'gateway_reference'])
                return Response({"status": "Transitional status saved"}, status=200)

            # B. Lancement de la tâche asynchrone (Le worker fera le gros du travail)
            async_task(
                'core.tasks.paiement_task',
                session.id,
                statut_gateway
            )

            logger.info(f"[Webhook] Session {external_reference} traitée en base. Tâche Q2 lancée.")

            # C. Réponse éclair à l'agrégateur
            logger.info(f"[Webhook] Session {external_reference} mise à jour. Tâche Q2 lancée.")
            return Response({"status": "Webhook processed successfully"}, status=200)

        except DatabaseError as e:
            logger.exception(f"[Webhook] Erreur SQL lors du traitement de la session {external_reference}")
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,
                status_code=500,
                dev_message=f"Échec de mise à jour de la session en BDD : {str(e)}"
            )
        except Exception as e:
            logger.exception(f"[Webhook] Erreur critique inattendue sur la session {external_reference}")
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,
                status_code=500,
                dev_message=f"Erreur interne lors de la délégation Q2 : {str(e)}"
            )

class PaiementViewSet(TenantModelViewSet):
    queryset = Paiement.objects.all()
    serializer_class = PaiementSerializer
    permission_classes = [IsAuthenticated, StrictDjangoModelPermissions]
    pagination_class = StandardResultsSetPagination
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

        try:
            self.perform_create(serializer)
        except DatabaseError as e:
            logger.exception("Erreur DB lors de l'enregistrement d'un paiement comptant")
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,
                status_code=500,
                dev_message=f"Échec d'insertion du paiement en base : {str(e)}"
            )

        return Response({
            "message": "Création réussie.",
            "id": serializer.instance.id,
            "reference": serializer.instance.reference
        }, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        """
        Interdiction absolue de supprimer un paiement de la base de données.
        """
        raise CustomAPIException(
            resp_code=ErrorCodes.FORBIDDEN,
            status_code=403,
            dev_message=f"Hard-delete refusé sur l'entité Paiement ID: {instance.id} (Règle d'audit financier)."
        )

class TypeContratViewSet(TenantModelViewSet):
    """
    API pour la gestion du dictionnaire des types de contrats (Véhicules, Accessoires...).
    """
    queryset = TypeContrat.objects.all()
    serializer_class = TypeContratSerializer
    permission_classes = [IsAuthenticated, StrictDjangoModelPermissions]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
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

        try:
            self.perform_create(serializer)
        except DatabaseError as e:
            logger.exception("Erreur DB lors de la création d'un type de contrat")
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,
                status_code=500,
                dev_message=f"Échec d'insertion du type de contrat : {str(e)}"
            )

        return Response({
            "message": "Création réussie.",
            "id": serializer.instance.id,
            "code": serializer.instance.code
        }, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        """
        Protection de l'intégrité référentielle de la base de données.
        """
        if instance.contrats.exists():
            raise CustomAPIException(
                resp_code=ErrorCodes.FORBIDDEN,
                status_code=403,
                dev_message=f"Suppression interdite : le type '{instance.libelle}' est référencé par {instance.contrats.count()} contrat(s)."
            )
        super().perform_destroy(instance)

class ParametreViewSet(TenantModelViewSet):
    """
    Gestion de la configuration de l'entreprise (Singleton par compte_id).
    """
    queryset = Parametre.objects.all()
    serializer_class = ParametreSerializer
    permission_classes = [IsAuthenticated, StrictDjangoModelPermissions]

    def get_queryset(self):
        return super().get_queryset().filter(compte_id=self.request.user.compte_id)

    def perform_create(self, serializer):
        compte_id = self.request.user.compte_id

        # Sécurité structurelle : Empêcher les doublons de configuration
        if Parametre.objects.filter(compte_id=compte_id).exists():
            raise CustomAPIException(
                resp_code=ErrorCodes.BAD_REQUEST,
                status_code=400,
                dev_message="Singleton Violation : Une configuration existe déjà pour ce compte. Utilisez PUT ou PATCH."
            )

        try:
            serializer.save(compte_id=compte_id)
        except DatabaseError as e:
            logger.exception("Erreur DB lors de l'initialisation des paramètres")
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,
                status_code=500,
                dev_message=f"Échec d'enregistrement des paramètres : {str(e)}"
            )

    def perform_destroy(self, instance):
        """
        Interdiction d'effacer la ligne de configuration globale.
        """
        raise CustomAPIException(
            resp_code=ErrorCodes.FORBIDDEN,
            status_code=403,
            dev_message=f"Suppression interdite des paramètres de l'entreprise (ID: {instance.id})."
        )