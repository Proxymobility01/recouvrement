import re
from collections import defaultdict
from croniter import croniter
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from decimal import Decimal
from django_q.models import Schedule
from core.utils import format_phone_cm
from recouvrement.models import Agence, CompteReceptionProprietaire, Contrat, Lease, Paiement, TypeContrat, Parametre, \
    PreuvePaiementUSSD, Proprietaire, ReglePenalite, Penalite, SessionPaiement, RegleGenerationLease


class DateSeulementEnLectureMixin:
    """
    Expose temporairement certains DateTimeField au format YYYY-MM-DD.

    La désérialisation reste celle du DateTimeField d'origine : seuls les
    payloads de réponse sont adaptés pour l'ancienne application mobile.
    """
    champs_datetime_en_date = ()

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        for champ in self.champs_datetime_en_date:
            valeur = getattr(instance, champ, None)
            if valeur is None:
                representation[champ] = None
                continue
            if timezone.is_aware(valeur):
                valeur = timezone.localtime(valeur)
            representation[champ] = valeur.date().isoformat()
        return representation


class ValidationRegleGenerationContratMixin:
    """Valide qu'une règle de génération appartient au compte du contrat."""

    def _compte_id_cible_regle_generation(self):
        compte_id = self.context.get('compte_id')
        if compte_id is None:
            compte_id = getattr(self.instance, 'compte_id', None)

        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None:
            return compte_id

        if user.is_superuser:
            return request.data.get('compte_id', compte_id)
        return user.compte_id

    def validate_regle_generation(self, value):
        if value is None:
            return value

        compte_id = self._compte_id_cible_regle_generation()
        if compte_id is not None and value.compte_id != int(compte_id):
            raise serializers.ValidationError(
                "La règle de génération doit appartenir au même compte "
                "que le contrat."
            )
        return value


class AgenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Agence
        fields = [
            'id',
            'compte_id',
            'nom',
            'zone',
            'code',
            'adresse',
            'telephone',
            'email',
            'actif',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'compte_id',
            'created_at',
            'updated_at',
        ]

    def _compte_id_cible(self):
        if self.instance is not None:
            return self.instance.compte_id

        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None:
            return None
        if user.is_superuser:
            return request.data.get('compte_id')
        return user.compte_id

    def validate_nom(self, value):
        nom = value.strip()
        if not nom:
            raise serializers.ValidationError(
                "Le nom de l'agence est obligatoire."
            )
        return nom

    def validate_code(self, value):
        code = value.strip().upper()
        if not code:
            raise serializers.ValidationError(
                "Le code de l'agence est obligatoire."
            )

        compte_id = self._compte_id_cible()
        if compte_id is not None:
            agences = Agence.objects.filter(
                compte_id=compte_id,
                code__iexact=code,
            )
            if self.instance is not None:
                agences = agences.exclude(pk=self.instance.pk)
            if agences.exists():
                raise serializers.ValidationError(
                    "Ce code d'agence existe déjà pour ce compte."
                )
        return code


class CompteReceptionProprietaireNesteeSerializer(serializers.ModelSerializer):
    """Vue allégée utilisée en lecture seule dans le détail d'un propriétaire."""

    operateur_display = serializers.CharField(
        source='get_operateur_display',
        read_only=True,
    )

    class Meta:
        model = CompteReceptionProprietaire
        fields = [
            'id', 'operateur', 'operateur_display', 'numero',
            'nom_titulaire', 'actif',
        ]
        read_only_fields = fields


class ProprietaireSerializer(serializers.ModelSerializer):
    comptes_reception = CompteReceptionProprietaireNesteeSerializer(
        many=True,
        read_only=True,
    )

    class Meta:
        model = Proprietaire
        fields = [
            'id', 'compte_id', 'nom_complet', 'actif',
            'comptes_reception', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'compte_id', 'comptes_reception',
            'created_at', 'updated_at',
        ]

    def validate_nom_complet(self, value):
        nom = (value or '').strip()
        if not nom:
            raise serializers.ValidationError(
                "Le nom du propriétaire est obligatoire."
            )
        return nom


class CompteReceptionProprietaireSerializer(serializers.ModelSerializer):
    proprietaire = serializers.PrimaryKeyRelatedField(
        queryset=Proprietaire.objects.all(),
        error_messages={
            'does_not_exist': "Le propriétaire sélectionné n'existe pas.",
            'incorrect_type': (
                "L'identifiant du propriétaire doit être un nombre entier."
            ),
        },
    )
    proprietaire_nom_complet = serializers.CharField(
        source='proprietaire.nom_complet',
        read_only=True,
        default=None,
    )
    # CharField explicite : le ChoiceField auto-généré par le ModelSerializer
    # rejetterait "orange"/"mtn" en minuscule AVANT que validate_operateur()
    # ait la moindre chance de normaliser la casse.
    operateur = serializers.CharField(max_length=10)
    operateur_display = serializers.CharField(
        source='get_operateur_display',
        read_only=True,
    )

    class Meta:
        model = CompteReceptionProprietaire
        fields = [
            'id', 'compte_id', 'proprietaire', 'proprietaire_nom_complet',
            'operateur', 'operateur_display', 'numero', 'nom_titulaire',
            'actif', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'compte_id', 'proprietaire_nom_complet',
            'operateur_display', 'created_at', 'updated_at',
        ]

    def _compte_id_cible(self):
        if self.instance is not None:
            return self.instance.compte_id

        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None:
            return None
        if user.is_superuser:
            return request.data.get('compte_id')
        return user.compte_id

    def validate_proprietaire(self, value):
        compte_id = self._compte_id_cible()
        if compte_id is not None:
            try:
                compte_id_int = int(compte_id)
            except (TypeError, ValueError):
                raise serializers.ValidationError(
                    "Le compte_id fourni n'est pas un nombre entier valide."
                )
            if value.compte_id != compte_id_int:
                # Message volontairement identique à celui d'un identifiant
                # inexistant : ne pas laisser deviner qu'un propriétaire
                # existe chez un autre partenaire.
                raise serializers.ValidationError(
                    "Le propriétaire sélectionné n'existe pas."
                )
        return value

    def validate_operateur(self, value):
        operateur = (value or '').strip().upper()
        operateurs_valides = {
            choix[0]
            for choix in CompteReceptionProprietaire.OPERATEUR_CHOICES
        }
        if operateur not in operateurs_valides:
            raise serializers.ValidationError(
                "L'opérateur doit être ORANGE ou MTN."
            )
        return operateur

    def validate_numero(self, value):
        numero = format_phone_cm(value)
        # format_phone_cm ne fait que nettoyer/préfixer : elle ne rejette
        # jamais un texte invalide. On vérifie ici le format final attendu
        # (indicatif 237 + 9 chiffres), sans quoi ce compte ne pourra
        # jamais être reconnu lors du rapprochement d'une preuve USSD.
        if not re.match(r'^237\d{9}$', numero):
            raise serializers.ValidationError(
                "Le numéro doit être un numéro camerounais valide (9 "
                "chiffres, avec ou sans l'indicatif 237)."
            )
        return numero


class TypeContratSerializer(serializers.ModelSerializer):
    class Meta:
        model = TypeContrat
        fields = [
            'id', 'libelle', 'code', 'est_principal',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate_code(self, value):
        code_formate = value.strip().upper()
        request = self.context.get('request')

        if request and request.user:
            qs = TypeContrat.objects.filter(code=code_formate, compte_id=request.user.compte_id)
            if self.instance:
                qs = qs.exclude(id=self.instance.id)

            if qs.exists():
                raise serializers.ValidationError("Ce code existe déjà.")

        return code_formate

    def validate_libelle(self, value):
        libelle_formate = value.strip()
        request = self.context.get('request')

        if request and request.user:
            qs = TypeContrat.objects.filter(libelle__iexact=libelle_formate, compte_id=request.user.compte_id)
            if self.instance:
                qs = qs.exclude(id=self.instance.id)

            if qs.exists():
                raise serializers.ValidationError("Ce type de contrat existe déjà.")

        return libelle_formate


class SousContratSerializer(
    DateSeulementEnLectureMixin,
    ValidationRegleGenerationContratMixin,
    serializers.ModelSerializer,
):
    """
    Serializer utilisé UNIQUEMENT en lecture pour afficher les enfants
    dans le détail du parent, OU lors de la création groupée.
    """
    specificites = serializers.JSONField(required=False, allow_null=True)
    montant_paye = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    proprietaire_nom_complet = serializers.CharField(
        source='proprietaire.nom_complet',
        read_only=True,
        default=None,
    )
    champs_datetime_en_date = ('prochaine_echeance',)

    class Meta:
        model = Contrat
        fields = [
            'id','reference', 'type_contrat', 'montant_total','montant_restant','montant_paye', 'montant_par_paiement',
            'frequence', 'date_debut', 'date_fin', 'prochaine_echeance',
             'statut', 'specificites', 'regle_generation',
            'proprietaire', 'proprietaire_nom_complet', 'config_paiement'
        ]
        read_only_fields = [
            'id', 'reference', 'statut', 'montant_restant',
            'proprietaire', 'proprietaire_nom_complet', 'config_paiement',
        ]
        extra_kwargs = {
            'montant_total': {'required': True},
            'montant_par_paiement': {'required': True},
            'frequence': {'required': True},
            'date_debut': {'required': True},
            'prochaine_echeance': {'required': True},
            'date_fin': {'required': True},
        }

    def validate_type_contrat(self, value):
        if value.est_principal:
            raise serializers.ValidationError("Type principal non autorisé ici.")
        return value

    def validate(self, attrs):
        montant_total = attrs.get('montant_total')
        montant_paye = attrs.get('montant_paye', 0)

        if montant_paye and montant_total and montant_paye > montant_total:
            raise serializers.ValidationError({
                "montant_paye": "L'avance dépasse le total."
            })
        return attrs


class ContratSerializer(
    DateSeulementEnLectureMixin,
    ValidationRegleGenerationContratMixin,
    serializers.ModelSerializer,
):
    enregistre_par_nom_complet = serializers.CharField(source='enregistre_par.nom_complet', read_only=True)
    chauffeur_nom_complet = serializers.CharField(source='chauffeur.nom_complet', read_only=True)
    specificites = serializers.JSONField(required=False, allow_null=True)
    type_contrat_libelle = serializers.CharField(source='type_contrat.libelle', read_only=True)
    agence_nom = serializers.CharField(
        source='agence.nom',
        read_only=True,
        default=None,
    )
    montant_paye = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    agence = serializers.PrimaryKeyRelatedField(
        queryset=Agence.objects.all(),
        required=False,
        allow_null=True,
        error_messages={
            'does_not_exist': "L'agence sélectionnée n'existe pas.",
            'incorrect_type': "L'identifiant de l'agence doit être un nombre entier."
        }
    )
    proprietaire = serializers.PrimaryKeyRelatedField(
        queryset=Proprietaire.objects.all(),
        required=True,
        allow_null=False,
        error_messages={
            'does_not_exist': "Le propriétaire sélectionné n'existe pas.",
            'incorrect_type': (
                "L'identifiant du propriétaire doit être un nombre entier."
            ),
            'null': "Le propriétaire du contrat est obligatoire.",
            'required': "Le propriétaire du contrat est obligatoire.",
        },
    )
    proprietaire_nom_complet = serializers.CharField(
        source='proprietaire.nom_complet',
        read_only=True,
        default=None,
    )
    champs_datetime_en_date = ('prochaine_echeance',)

    class Meta:
        model = Contrat
        fields = [
            'id', 'reference', 'compte_id', 'chauffeur', 'immatriculation', 'vin', 'nom_complet',
            'type_contrat', 'type_contrat_libelle', 'parent','agence', 'agence_nom',
            'proprietaire', 'proprietaire_nom_complet',
            'enregistre_par', 'enregistre_par_nom_complet', 'chauffeur_nom_complet',
            'montant_total', 'montant_restant', 'montant_paye', 'montant_par_paiement',
            'frequence', 'date_debut', 'date_fin', 'prochaine_echeance',
            'statut', 'specificites', 'regle_generation',
            'config_paiement', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'reference', 'montant_restant', 'enregistre_par',
            'created_at', 'updated_at', 'nom_complet', 'compte_id',
            'config_paiement',
        ]
        extra_kwargs = {
            'montant_total': {'required': True},
            'montant_par_paiement': {'required': True},
            'frequence': {'required': True},
            'date_debut': {'required': True},
            'prochaine_echeance': {'required': True},
            'date_fin': {'required': True},
            'vin': {'required': True, 'allow_blank': False, 'allow_null': False},
            'immatriculation': {'required': True, 'allow_blank': False, 'allow_null': False},
        }

    def validate_type_contrat(self, value):
        parent_id = self.initial_data.get('parent')
        is_updating_sub_contract = self.instance and self.instance.parent is not None

        if not parent_id and not is_updating_sub_contract:
            if not value.est_principal:
                raise serializers.ValidationError("Sous contrat non autorisé comme parent.")
        return value

    def validate_parent(self, value):
        if value is not None and value.parent is not None:
            raise serializers.ValidationError("Le parent ne peut pas être un sous contrat.")

        if self.instance is None and value is not None:
            raise serializers.ValidationError("Création de sous-contrat non autorisée ici.")

        return value

    def validate_agence(self, value):
        """
        S'assure que l'agence appartient à l'entreprise et empêche la modification
        si le contrat a déjà propagé son agence sur des données financières.
        """
        request = self.context.get('request')
        compte_id = getattr(self.instance, 'compte_id', None)

        if request and getattr(request, 'user', None):
            if request.user.is_superuser:
                compte_id = request.data.get('compte_id', compte_id)
            else:
                compte_id = request.user.compte_id

        if value is not None and compte_id is not None and value.compte_id != int(compte_id):
            raise serializers.ValidationError(
                "L'agence spécifiée n'appartient pas à votre entreprise."
            )

        # 🔒 MUR DE SÉCURITÉ : Blocage de modification si propagation existante
        if self.instance and self.instance.agence != value:

            # 1. Vérification sur le contrat parent
            a_des_leases = self.instance.leases.exists()
            a_des_paiements = self.instance.paiements.exists()

            if a_des_leases or a_des_paiements:
                raise serializers.ValidationError(
                    "Modification impossible : Ce contrat a déjà généré des données "
                    "financières (échéances ou paiements) rattachées à l'agence actuelle."
                )

            # 2. Vérification sur tous les sous-contrats
            if self.instance.sous_contrats.exists():
                has_sub_leases = self.instance.sous_contrats.filter(leases__isnull=False).exists()
                has_sub_payments = self.instance.sous_contrats.filter(paiements__isnull=False).exists()

                if has_sub_leases or has_sub_payments:
                    raise serializers.ValidationError(
                        "Modification impossible : Un des sous-contrats a déjà généré des données "
                        "financières rattachées à l'agence actuelle."
                    )

        return value

    def validate_proprietaire(self, value):
        request = self.context.get('request')
        compte_id = getattr(self.instance, 'compte_id', None)

        if request and getattr(request, 'user', None):
            if request.user.is_superuser:
                compte_id = request.data.get('compte_id', compte_id)
            else:
                compte_id = request.user.compte_id

        if compte_id is not None and value.compte_id != int(compte_id):
            raise serializers.ValidationError(
                "Le propriétaire spécifié n'appartient pas à votre "
                "entreprise."
            )

        proprietaire_actuel_id = getattr(
            self.instance,
            'proprietaire_id',
            None,
        )
        if not value.actif and proprietaire_actuel_id != value.id:
            raise serializers.ValidationError(
                "Un propriétaire inactif ne peut pas recevoir un contrat."
            )

        if (
            self.instance is not None
            and self.instance.parent_id is None
            and proprietaire_actuel_id != value.id
            and self.instance.possede_historique_financier()
        ):
            raise serializers.ValidationError(
                "Le propriétaire ne peut plus être modifié car ce contrat "
                "ou l'un de ses sous-contrats possède déjà un historique "
                "financier."
            )

        return value

    def get_fields(self):
        fields = super().get_fields()
        if self.instance is not None and self.instance.parent_id is not None:
            fields['proprietaire'].read_only = True
        return fields

    def validate(self, attrs):
        montant_total = attrs.get('montant_total', getattr(self.instance, 'montant_total', None))
        montant_par_paiement = attrs.get('montant_par_paiement', getattr(self.instance, 'montant_par_paiement', None))
        montant_paye = attrs.get('montant_paye', getattr(self.instance, 'montant_paye', 0))

        parent = attrs.get('parent', getattr(self.instance, 'parent', None))
        chauffeur = attrs.get('chauffeur', getattr(self.instance, 'chauffeur', None))
        agence = attrs.get('agence', getattr(self.instance, 'agence', None))
        proprietaire = attrs.get(
            'proprietaire',
            getattr(self.instance, 'proprietaire', None),
        )

        if (
            self.instance is None
            and parent is None
            and proprietaire is None
        ):
            raise serializers.ValidationError({
                'proprietaire': "Le propriétaire du contrat est obligatoire."
            })

        if parent is not None and getattr(agence, 'id', None) != parent.agence_id:
            raise serializers.ValidationError({
                'agence': (
                    "Un sous-contrat doit toujours appartenir à la même "
                    "agence que son contrat parent."
                )
            })

        if (
            parent is not None
            and getattr(proprietaire, 'id', None) != parent.proprietaire_id
        ):
            raise serializers.ValidationError({
                'proprietaire': (
                    "Un sous-contrat doit toujours appartenir au même "
                    "propriétaire que son contrat parent."
                )
            })

        if not parent:
            # On cherche s'il existe déjà un contrat parent actif pour ce chauffeur
            existing_active = Contrat.objects.filter(
                chauffeur=chauffeur,
                parent__isnull=True
            ).exclude(
                statut__in=['SOLDE', 'ANNULE']
            )

            # Si on modifie un contrat existant, on s'exclut soi-même de la recherche
            if self.instance:
                existing_active = existing_active.exclude(pk=self.instance.pk)

            # 3. L'interception !
            if existing_active.exists():
                # Cela va lever automatiquement une erreur HTTP 400 Bad Request
                raise serializers.ValidationError({
                    "chauffeur": "Ce chauffeur possède déjà un contrat parent actif. Vous ne pouvez pas en créer un autre."
                })

        if montant_par_paiement and montant_total and montant_par_paiement > montant_total:
            raise serializers.ValidationError({
                "montant_par_paiement": "L'échéance dépasse le total."
            })

        if not self.instance and montant_paye > montant_total:
            raise serializers.ValidationError({
                "montant_paye": "L'avance dépasse le total."
            })

        date_debut = attrs.get('date_debut', getattr(self.instance, 'date_debut', None))
        date_fin = attrs.get('date_fin', getattr(self.instance, 'date_fin', None))

        if date_debut and date_fin and date_fin < date_debut:
            raise serializers.ValidationError({
                "date_fin": "Date de fin antérieure au début."
            })

        return attrs

    def create(self, validated_data):
        sous_contrats_data = self.initial_data.pop('sous_contrats', [])
        chauffeur = validated_data.get('chauffeur')
        if chauffeur:
            validated_data['nom_complet'] = chauffeur.nom_complet or "Nom pas défini"

        montant_total = validated_data.get('montant_total', Decimal('0.00'))
        avance_payee = validated_data.get('montant_paye', Decimal('0.00'))

        validated_data['montant_restant'] = max(Decimal('0.00'), montant_total - avance_payee)
        validated_data['statut'] = Contrat.STATUT_SOLDE if validated_data[
                                                               'montant_restant'] == 0 else Contrat.STATUT_ACTIF

        with transaction.atomic():
            parent_contrat = super().create(validated_data)
            for sc_data in sous_contrats_data:
                sc_serializer = SousContratSerializer(
                    data=sc_data,
                    context={
                        **self.context,
                        'compte_id': parent_contrat.compte_id,
                    },
                )
                sc_serializer.is_valid(raise_exception=True)

                sc_instance_data = sc_serializer.validated_data
                sc_instance_data['parent'] = parent_contrat
                sc_instance_data['agence'] = parent_contrat.agence
                sc_instance_data['proprietaire'] = parent_contrat.proprietaire
                sc_instance_data['chauffeur'] = parent_contrat.chauffeur
                sc_instance_data['compte_id'] = parent_contrat.compte_id
                sc_instance_data['nom_complet'] = parent_contrat.nom_complet
                sc_instance_data['enregistre_par'] = parent_contrat.enregistre_par
                sc_instance_data['config_paiement'] = parent_contrat.config_paiement

                sc_total = sc_instance_data.get('montant_total', Decimal('0.00'))
                sc_avance = sc_instance_data.get('montant_paye', Decimal('0.00'))
                sc_instance_data['montant_restant'] = max(Decimal('0.00'), sc_total - sc_avance)
                sc_instance_data['statut'] = Contrat.STATUT_SOLDE if sc_instance_data[
                                                                         'montant_restant'] == 0 else Contrat.STATUT_ACTIF
                Contrat.objects.create(**sc_instance_data)

        return parent_contrat

    def update(self, instance, validated_data):


        chauffeur = validated_data.get('chauffeur')
        if chauffeur:
            validated_data['nom_complet'] = chauffeur.nom_complet
        ancienne_agence = instance.agence
        # 🚀 LOGIQUE FINANCIÈRE SÉCURISÉE (Mise à jour)
        new_total = validated_data.get('montant_total', instance.montant_total)
        new_paye = validated_data.get('montant_paye', instance.montant_paye)

        if new_total != instance.montant_total or new_paye != instance.montant_paye:
            validated_data['montant_restant'] = max(Decimal('0.00'), new_total - new_paye)

            # Auto-solde si le montant restant tombe à 0
            if validated_data['montant_restant'] == 0:
                validated_data['statut'] = Contrat.STATUT_SOLDE

        with transaction.atomic():
            updated_instance = super().update(instance, validated_data)

            # 🏢 PROPAGATION DE LA MODIFICATION DE L'AGENCE AUX SOUS-CONTRATS
            # (Cette étape n'est atteinte que si la validation de sécurité a réussi plus haut)
            nouvelle_agence = updated_instance.agence

            if ancienne_agence != nouvelle_agence:
                # Si l'agence a changé, on met à jour tous les sous-contrats en une seule requête SQL
                updated_instance.sous_contrats.update(
                    agence=nouvelle_agence,
                    updated_at=timezone.now(),
                )

        return updated_instance


class LeaseSerializer(DateSeulementEnLectureMixin, serializers.ModelSerializer):
    chauffeur_nom_complet = serializers.CharField(source='contrat.nom_complet', read_only=True)
    contrat_id = serializers.IntegerField(source='contrat.id', read_only=True)
    compte_id = serializers.IntegerField(source='contrat.compte_id', read_only=True)
    type_contrat_libelle = serializers.CharField(source='contrat.type_contrat.libelle', read_only=True)
    agence_nom = serializers.CharField(
        source='agence.nom',
        read_only=True,
        default=None,
    )
    reste_a_payer = serializers.SerializerMethodField()
    paiement_en_verification = serializers.SerializerMethodField()
    champs_datetime_en_date = ('date_echeance',)

    class Meta:
        model = Lease
        fields = [
            'id',
            'compte_id',
            'contrat_id',
            'agence',
            'agence_nom',
            'chauffeur_nom_complet',
            'type_contrat_libelle',
            'date_echeance',
            'montant_attendu',
            'montant_paye',
            'reste_a_payer',
            'paiement_en_verification',
            'statut',
            'created_at'
        ]
        read_only_fields = fields

    def get_reste_a_payer(self, obj):
        return obj.montant_attendu - obj.montant_paye

    def get_paiement_en_verification(self, obj):
        valeur_annotee = getattr(
            obj,
            'paiement_ussd_en_verification',
            None,
        )
        if valeur_annotee is not None:
            return valeur_annotee

        return obj.paiements.filter(
            methode=Paiement.METHODE_USSD_ASSISTE,
            statut=Paiement.STATUT_EN_ATTENTE,
            session__canal=SessionPaiement.CANAL_USSD_ASSISTE,
            session__statut=SessionPaiement.STATUT_EN_VERIFICATION,
        ).exists()


class CalendrierSerializer(DateSeulementEnLectureMixin, serializers.ModelSerializer):
    chauffeur_nom = serializers.CharField(source='contrat.chauffeur.nom_complet', read_only=True, default="Inconnu")
    paiement_en_verification = serializers.SerializerMethodField()
    champs_datetime_en_date = ('date_echeance',)


    class Meta:
        model = Lease
        fields = [
            'id', 'date_echeance',
            'chauffeur_nom', 'statut', 'paiement_en_verification',
        ]

    def get_paiement_en_verification(self, obj):
        valeur_annotee = getattr(
            obj,
            'paiement_ussd_en_verification',
            None,
        )
        if valeur_annotee is not None:
            return valeur_annotee

        return obj.paiements.filter(
            methode=Paiement.METHODE_USSD_ASSISTE,
            statut=Paiement.STATUT_EN_ATTENTE,
            session__canal=SessionPaiement.CANAL_USSD_ASSISTE,
            session__statut=SessionPaiement.STATUT_EN_VERIFICATION,
        ).exists()


class LignePaiementSerializer(serializers.Serializer):
    lease_id = serializers.IntegerField()
    montant = serializers.DecimalField(max_digits=12, decimal_places=2)

    def validate_lease_id(self, value):
        user = self.context['request'].user
        try:
            lease_query = Lease.objects.select_related(
                'contrat',
                'contrat__config_paiement',
                'contrat__agence',
                'contrat__proprietaire',
            ).filter(
                id=value,
                contrat__compte_id=user.compte_id,
            )
            if not user.is_staff and not user.is_superuser:
                lease_query = lease_query.filter(contrat__chauffeur=user)

            lease = lease_query.get()
        except Lease.DoesNotExist:
            raise serializers.ValidationError("Échéance introuvable ou accès refusé.")

        if lease.statut == Lease.STATUT_PAYE:
            raise serializers.ValidationError("Échéance déjà soldée.")

        return lease

    def validate(self, attrs):
        lease = attrs.get('lease_id')
        montant = attrs.get('montant')

        if montant <= 0:
            raise serializers.ValidationError({"montant": "Doit être strictement positif."})

        reste_a_payer = lease.montant_attendu - lease.montant_paye
        if montant > reste_a_payer:
            raise serializers.ValidationError({
                "montant": "Dépasse le reste à payer."
            })

        return attrs


class InitiationPaiementSerializer(serializers.Serializer):
    """
    Le serializer principal qui reçoit la requête globale d'initiation de paiement.
    """
    verifier_meme_vehicule = True
    lignes = LignePaiementSerializer(many=True, allow_empty=False)
    phone_number = serializers.CharField(max_length=20, required=True, allow_blank=False)

    def validate(self, attrs):
        lignes = attrs.get('lignes', [])

        if not lignes:
            return attrs

        LeaseModel = lignes[0]['lease_id'].__class__

        # 1. Anti-Doublons
        lease_ids = [ligne['lease_id'].id for ligne in lignes]
        if len(lease_ids) != len(set(lease_ids)):
            raise serializers.ValidationError({"lignes": "Doublons détectés dans les échéances."})

        # 1.5 🚀 SÉCURITÉ MAXIMALE : Interdiction de payer une échéance annulée ou déjà payée
        leases_invalides = [
            str(ligne['lease_id'].id)
            for ligne in lignes
            if ligne['lease_id'].statut in [LeaseModel.STATUT_ANNULE, LeaseModel.STATUT_PAYE]
        ]
        if leases_invalides:
            raise serializers.ValidationError({
                "lignes": f"Impossible de payer des échéances déjà payées ou annulées (IDs concernés : {', '.join(leases_invalides)})."
            })

        # 2. Vérification de l'intégrité de la famille (Même Véhicule / Contrat parent)
        root_parent_ids = set()
        for ligne in lignes:
            contrat = ligne['lease_id'].contrat
            root_id = contrat.parent_id if contrat.parent_id else contrat.id
            root_parent_ids.add(root_id)

        if self.verifier_meme_vehicule and len(root_parent_ids) > 1:
            raise serializers.ValidationError({
                "lignes": "Mélange de contrats appartenant à des véhicules différents interdit."
            })

        # 3. LE VERROU INTELLIGENT (Par contrat)
        # A. On regroupe les leases du panier par contrat_id
        leases_par_contrat = defaultdict(list)
        for ligne in lignes:
            lease = ligne['lease_id']
            leases_par_contrat[lease.contrat_id].append(lease)

        erreurs = []

        # B. On vérifie l'historique de CHAQUE contrat indépendamment
        for contrat_id, leases_du_contrat in leases_par_contrat.items():

            # Date la plus lointaine qu'il essaie de payer pour CE contrat
            derniere_date = max(lease.date_echeance for lease in leases_du_contrat)

            # Récupération de la référence du contrat pour le message d'erreur
            reference_contrat = leases_du_contrat[0].contrat.reference

            # Y a-t-il un trou chronologique pour CE contrat précis ?
            arrieres_bloquants = LeaseModel.objects.filter(
                contrat_id=contrat_id,
                date_echeance__lt=derniere_date
            ).exclude(
                # On ignore les paiements effectués ET les échéances annulées
                statut__in=[LeaseModel.STATUT_PAYE, LeaseModel.STATUT_ANNULE]
            ).exclude(
                id__in=lease_ids  # On pardonne s'il paie cet arriéré dans le même panier
            )

            if arrieres_bloquants.exists():
                erreurs.append(f"Il existe des impayés pour le contrat {reference_contrat}.")

        # C. S'il y a des erreurs sur un ou plusieurs contrats, on bloque tout
        if erreurs:
            raise serializers.ValidationError({
                "lignes": " | ".join(erreurs)
            })

        return attrs


class InitiationPaiementUSSDSerializer(InitiationPaiementSerializer):
    """Valide la sélection avant de préparer un transfert USSD manuel."""

    verifier_meme_vehicule = False
    operateur = serializers.CharField(max_length=10)
    phone_number = serializers.CharField(
        max_length=20,
        required=False,
        allow_blank=True,
        default='',
    )

    def validate_operateur(self, value):
        operateur = (value or '').strip().upper()
        operateurs_valides = {
            choix[0]
            for choix in CompteReceptionProprietaire.OPERATEUR_CHOICES
        }
        if operateur not in operateurs_valides:
            raise serializers.ValidationError(
                "L'opérateur doit être ORANGE ou MTN."
            )
        return operateur


class SoumissionPreuvePaiementUSSDSerializer(serializers.Serializer):
    """Payload capturé sur le téléphone après le transfert USSD."""

    sessionReference = serializers.CharField(max_length=100)
    network = serializers.CharField(max_length=10)
    rawText = serializers.CharField(allow_blank=False)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    senderName = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        default='',
    )
    senderPhone = serializers.CharField(max_length=20)
    recipientName = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        default='',
    )
    recipientPhone = serializers.CharField(max_length=20)
    reference = serializers.CharField(max_length=100)
    timestamp = serializers.DateTimeField(required=False, allow_null=True)
    fee = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        default=Decimal('0.00'),
        min_value=Decimal('0.00'),
    )
    commission = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        default=Decimal('0.00'),
        min_value=Decimal('0.00'),
    )
    newBalance = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
    capturedAtDevice = serializers.DateTimeField()

    def validate_network(self, value):
        operateur = (value or '').strip().upper()
        operateurs_valides = {
            choix[0]
            for choix in CompteReceptionProprietaire.OPERATEUR_CHOICES
        }
        if operateur not in operateurs_valides:
            raise serializers.ValidationError(
                "L'opérateur doit être ORANGE ou MTN."
            )
        return operateur

    def validate_reference(self, value):
        reference = (value or '').strip().upper()
        if not reference:
            raise serializers.ValidationError(
                "La référence opérateur est obligatoire."
            )
        return reference

    def validate(self, attrs):
        request = self.context['request']
        session_query = SessionPaiement.objects.select_related(
            'proprietaire',
            'compte_reception',
        ).filter(reference=attrs['sessionReference'])

        if not request.user.is_superuser:
            session_query = session_query.filter(
                compte_id=request.user.compte_id,
            )
        if not request.user.is_staff and not request.user.is_superuser:
            session_query = session_query.filter(utilisateur=request.user)

        session = session_query.first()
        if session is None:
            raise serializers.ValidationError({
                'sessionReference': (
                    "Session USSD introuvable ou accès refusé."
                ),
            })
        if session.canal != SessionPaiement.CANAL_USSD_ASSISTE:
            raise serializers.ValidationError({
                'sessionReference': (
                    "Cette session n'est pas un paiement USSD assisté."
                ),
            })
        if session.statut != SessionPaiement.STATUT_EN_ATTENTE:
            raise serializers.ValidationError({
                'sessionReference': (
                    "Cette session n'accepte plus de nouvelle preuve."
                ),
            })
        if PreuvePaiementUSSD.objects.filter(session=session).exists():
            raise serializers.ValidationError({
                'sessionReference': (
                    "Une preuve a déjà été soumise pour cette session."
                ),
            })

        if attrs['network'] != session.operateur:
            raise serializers.ValidationError({
                'network': (
                    "L'opérateur ne correspond pas à celui de la session."
                ),
            })

        numero_destinataire = format_phone_cm(attrs['recipientPhone'])
        if numero_destinataire != session.numero_destinataire:
            raise serializers.ValidationError({
                'recipientPhone': (
                    "Le numéro destinataire ne correspond pas au compte de "
                    "réception du propriétaire."
                ),
            })

        numero_expediteur = format_phone_cm(attrs['senderPhone'])
        if session.telephone and numero_expediteur != session.telephone:
            raise serializers.ValidationError({
                'senderPhone': (
                    "Le numéro expéditeur ne correspond pas au numéro "
                    "déclaré lors de l'initiation."
                ),
            })

        if attrs['amount'] != session.montant_total:
            raise serializers.ValidationError({
                'amount': (
                    "Le montant transféré ne correspond pas au montant "
                    "total de la session."
                ),
            })

        if PreuvePaiementUSSD.objects.filter(
            compte_id=session.compte_id,
            operateur=attrs['network'],
            reference_operateur=attrs['reference'],
        ).exists():
            raise serializers.ValidationError({
                'reference': (
                    "Cette référence de transaction a déjà été utilisée."
                ),
            })

        attrs['session'] = session
        attrs['senderPhone'] = numero_expediteur
        attrs['recipientPhone'] = numero_destinataire
        return attrs


class PreuvePaiementUSSDSerializer(serializers.ModelSerializer):
    session_reference = serializers.CharField(
        source='session.reference',
        read_only=True,
    )
    proprietaire_nom_complet = serializers.CharField(
        source='session.proprietaire.nom_complet',
        read_only=True,
        default=None,
    )
    operateur_display = serializers.CharField(
        source='get_operateur_display',
        read_only=True,
    )
    verifie_par_nom_complet = serializers.CharField(
        source='verifie_par.nom_complet',
        read_only=True,
        default=None,
    )

    class Meta:
        model = PreuvePaiementUSSD
        fields = [
            'id',
            'compte_id',
            'session',
            'session_reference',
            'proprietaire_nom_complet',
            'operateur',
            'operateur_display',
            'reference_operateur',
            'texte_brut',
            'montant_transfere',
            'frais_operateur',
            'commission',
            'nouveau_solde',
            'nom_expediteur',
            'numero_expediteur',
            'nom_destinataire',
            'numero_destinataire',
            'date_transaction',
            'capture_appareil_le',
            'statut',
            'verifie_par',
            'verifie_par_nom_complet',
            'verifie_le',
            'motif_rejet',
            'created_at',
        ]
        # Lecture seule : la création se fait via /soumettre-preuve-paiement-ussd/
        # et les transitions de statut via les actions valider/rejeter.
        read_only_fields = fields


class RejeterPreuveUSSDSerializer(serializers.Serializer):
    """Payload minimal exigé pour rejeter une preuve de paiement USSD."""

    motif = serializers.CharField(
        allow_blank=False,
        max_length=255,
        trim_whitespace=True,
    )


class PaiementSerializer(serializers.ModelSerializer):
    lease_id = serializers.PrimaryKeyRelatedField(
        queryset=Lease.objects.all(),
        source='lease'
    )

    chauffeur_nom_complet = serializers.CharField(source='contrat.nom_complet', read_only=True)
    enregistre_par = serializers.CharField(source='enregistre_par.nom_complet', read_only=True)
    agence = serializers.PrimaryKeyRelatedField(read_only=True)
    agence_nom = serializers.CharField(
        source='agence.nom',
        read_only=True,
        default=None,
    )

    class Meta:
        model = Paiement
        fields = [
            'id', 'lease_id', 'agence', 'agence_nom', 'montant', 'methode',
            'transaction_id', 'statut', 'date_paiement',
            'chauffeur_nom_complet', 'enregistre_par'
        ]
        read_only_fields = [
            'methode', 'transaction_id', 'statut',
            'date_paiement', 'chauffeur_nom_complet', 'enregistre_par',
            'agence', 'agence_nom',
        ]

    def validate(self, attrs):
        user = self.context['request'].user
        lease = attrs.get('lease', getattr(self.instance, 'lease', None))
        montant = attrs.get('montant', getattr(self.instance, 'montant', None))

        if lease and lease.compte_id != user.compte_id:
            raise serializers.ValidationError({"lease_id": "Échéance introuvable."})

        if montant is not None and montant <= 0:
            raise serializers.ValidationError({"montant": "Doit être strictement positif."})

        if lease and montant is not None:
            if self.instance:
                deja_paye_autres = lease.montant_paye - self.instance.montant
                reste_a_payer = lease.montant_attendu - deja_paye_autres
            else:
                if lease.statut == Lease.STATUT_PAYE:
                    raise serializers.ValidationError({"lease_id": "Échéance déjà soldée."})
                reste_a_payer = lease.montant_attendu - lease.montant_paye

            if montant > reste_a_payer:
                raise serializers.ValidationError({"montant": "Dépasse le reste à payer."})

        return attrs

    def create(self, validated_data):
        user = self.context['request'].user

        lease = validated_data['lease']
        montant = validated_data['montant']

        validated_data['methode'] = Paiement.METHODE_ESPECES
        validated_data['contrat'] = lease.contrat
        validated_data['compte_id'] = user.compte_id
        validated_data['statut'] = Paiement.STATUT_VALIDE
        validated_data['date_paiement'] = timezone.now()

        with transaction.atomic():
            paiement = super().create(validated_data)

            lease.montant_paye += montant
            lease.statut = Lease.STATUT_PAYE if lease.montant_paye >= lease.montant_attendu else Lease.STATUT_PARTIEL
            lease.save()

            contrat = lease.contrat
            contrat.montant_restant = max(contrat.montant_restant - montant, Decimal('0.00'))
            contrat.montant_paye += montant
            if contrat.montant_restant == 0:
                contrat.statut = Contrat.STATUT_SOLDE
            contrat.save()

            return paiement

    def update(self, instance, validated_data):
        if instance.methode == Paiement.METHODE_MOBILE_MONEY:
            raise serializers.ValidationError({"methode": "Modification interdite (Mobile Money)."})

        validated_data.pop('methode', None)
        validated_data.pop('lease', None)

        nouveau_montant = validated_data.get('montant')

        if nouveau_montant is not None and nouveau_montant != instance.montant:
            difference = nouveau_montant - instance.montant
            lease = instance.lease
            contrat = lease.contrat

            with transaction.atomic():
                lease.montant_paye += difference
                lease.statut = Lease.STATUT_PAYE if lease.montant_paye >= lease.montant_attendu else Lease.STATUT_PARTIEL
                lease.save()

                contrat.montant_restant = max(contrat.montant_restant - difference, Decimal('0.00'))
                contrat.montant_paye += difference
                if contrat.montant_restant == 0:
                    contrat.statut = Contrat.STATUT_SOLDE
                elif contrat.statut == Contrat.STATUT_SOLDE and contrat.montant_restant > 0:
                    contrat.statut = Contrat.STATUT_ACTIF
                contrat.save()

        return super().update(instance, validated_data)


class ParametreSerializer(serializers.ModelSerializer):
    jours_repos = serializers.ListField(
        child=serializers.IntegerField(min_value=0, max_value=6),
        allow_empty=True,
        help_text="Liste des jours de repos (0=Lundi, ..., 6=Dimanche)"
    )

    class Meta:
        model = Parametre
        fields = ['id', 'jours_repos', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_jours_repos(self, value):
        """
        Nettoie la donnée :
        Si le Front envoie [6, 0, 6], on le transforme proprement en [0, 6]
        """
        if value:
            # set() enlève les doublons, sorted() les remet dans l'ordre (0 à 6)
            return sorted(list(set(value)))
        return []


class ReglePenaliteSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReglePenalite
        fields = [
            'id',
            'nom',
            'montant',
            'occurrences',
            'frequence',
            'cron_expression',
            'debut',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_nom(self, value):
        """
        Nettoie et valide le nom de la règle.
        - Minimum 2 caractères.
        - Uniquement lettres (avec accents), chiffres, espaces, tirets et underscores.
        """
        nom_nettoye = value.strip()

        if len(nom_nettoye) < 2:
            raise serializers.ValidationError("Le nom doit contenir au moins 2 caractères.")
        if not re.match(r"^[\w\s\-\u00C0-\u024F]+$", nom_nettoye, re.UNICODE):
            raise serializers.ValidationError(
                "Utilisez uniquement des lettres, chiffres, espaces et tirets."
            )

        return nom_nettoye

    def validate_occurrences(self, value):
        """
        S'assure que le plafond d'occurrences respecte la convention Django-Q (-1 pour infini).
        """
        if value < -1:
            raise serializers.ValidationError("Le nombre d'occurrences ne peut pas être inférieur à -1.")
        return value

    def validate_cron_expression(self, value):
        """
        Valide que l'expression Cron correspond au format standard (5 parties).
        Exemples valides : "* * * * *", "0 14,16 * * *", "*/15 * * * 1-5"
        """
        if value:
            # Cette Regex vérifie qu'il y a exactement 5 blocs séparés par des espaces.
            # Chaque bloc peut contenir des chiffres, *, /, - ou ,
            cron_regex = r'^(\*|[0-5]?\d)([\/\,\-][0-5]?\d)* (\*|[0-2]?\d)([\/\,\-][0-2]?\d)* (\*|[0-3]?\d)([\/\,\-][0-3]?\d)* (\*|[0-1]?\d)([\/\,\-][0-1]?\d)* (\*|[0-7])([\/\,\-][0-7])*$'

            if not re.match(cron_regex, value.strip()):
                raise serializers.ValidationError(
                    "Format Cron invalide. L'expression doit contenir 5 parties (ex: '0 14,16 * * *')."
                )
        return value

    def validate(self, data):
        """
        Validation croisée (Logique métier globale).
        """
        frequence = data.get('frequence', getattr(self.instance, 'frequence', None))
        cron_expression = data.get('cron_expression', getattr(self.instance, 'cron_expression', None))
        if frequence == Schedule.CRON:
            if not cron_expression or cron_expression.strip() == "":
                raise serializers.ValidationError({
                    "cron_expression": "L'expression Cron est requise lorsque la fréquence est réglée sur 'Expression Cron'."
                })
        else:
            data['cron_expression'] = None

        return data


class PenaliteSerializer(serializers.ModelSerializer):
    agence_nom = serializers.CharField(
        source='agence.nom',
        read_only=True,
        default=None,
    )

    class Meta:
        model = Penalite
        fields = [
            'id',
            'lease',
            'agence',
            'agence_nom',
            'nom_complet',
            'montant',
            'date_application',
            'motif',
            'created_at'
        ]
        read_only_fields = [
            'id', 'lease', 'agence', 'agence_nom', 'nom_complet', 'montant',
            'date_application', 'motif', 'created_at'
        ]


class SessionPaiementSerializer(serializers.ModelSerializer):
    config_paiement_nom = serializers.CharField(
        source='config_paiement.nom',
        read_only=True,
        default=None,
    )
    agence_nom = serializers.CharField(
        source='agence.nom',
        read_only=True,
        default=None,
    )
    proprietaire_nom_complet = serializers.CharField(
        source='proprietaire.nom_complet',
        read_only=True,
        default=None,
    )
    preuve_ussd_statut = serializers.CharField(
        source='preuve_ussd.statut',
        read_only=True,
        default=None,
    )

    class Meta:
        model = SessionPaiement
        fields = [
            'id',
            'reference',
            'canal',
            'statut',
            'montant_total',
            'telephone',
            'agence',
            'agence_nom',
            'config_paiement',
            'config_paiement_nom',
            'proprietaire',
            'proprietaire_nom_complet',
            'compte_reception',
            'operateur',
            'numero_destinataire',
            'nom_destinataire',
            'preuve_ussd_statut',
            'date_validation',
            'created_at',
        ]
        read_only_fields = fields


class AssignerRegleSerializer(serializers.Serializer):
    contrat_ids = serializers.ListField(
        child=serializers.IntegerField(),
        allow_empty=False,
    )

    def validate_contrat_ids(self, value):
        compte_id_cible = self.context.get('compte_id_cible')
        if compte_id_cible is None:
            raise AssertionError(
                "Le compte cible de la règle doit être fourni au serializer."
            )

        # Déduplique sans modifier l'ordre reçu afin de produire des erreurs
        # prévisibles pour le client.
        ids_uniques = list(dict.fromkeys(value))

        # Une seule requête qui récupère les IDs valides
        ids_valides = set(
            Contrat.objects.filter(
                id__in=ids_uniques,
                compte_id=compte_id_cible,
                statut__in=Contrat.STATUTS_ASSIGNABLES_REGLE_GENERATION,
            ).values_list('id', flat=True)
        )

        ids_invalides = [
            contrat_id
            for contrat_id in ids_uniques
            if contrat_id not in ids_valides
        ]
        if ids_invalides:
            raise serializers.ValidationError(
                "Contrats introuvables, non actifs ou appartenant à un autre "
                f"compte : {ids_invalides}"
            )

        return ids_uniques


class AnnulerLeasesSerializer(serializers.Serializer):
    lease_ids = serializers.ListField(
        child=serializers.IntegerField(),
        allow_empty=False,
        help_text="Liste des IDs des échéances à annuler."
    )
    jours_a_prolonger = serializers.IntegerField(
        min_value=0,
        default=0,
        help_text="Nombre de jours ouvrés à ajouter à la date de fin du contrat."
    )


class RegleGenerationLeaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = RegleGenerationLease
        fields = [
            'id',
            'nom',
            'frequence',
            'cron_expression',
            'debut',
            'actif',
            'defaut',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_nom(self, value):
        """
        Nettoie et valide le nom de la règle.
        - Minimum 2 caractères.
        - Uniquement lettres (avec accents), chiffres, espaces, tirets et underscores.
        """
        nom_nettoye = value.strip()

        if len(nom_nettoye) < 2:
            raise serializers.ValidationError("Le nom doit contenir au moins 2 caractères.")
        if not re.match(r"^[\w\s\-\u00C0-\u024F]+$", nom_nettoye, re.UNICODE):
            raise serializers.ValidationError(
                "Utilisez uniquement des lettres, chiffres, espaces et tirets."
            )

        request = self.context.get('request')
        compte_id = getattr(self.instance, 'compte_id', None)
        if request and getattr(request, 'user', None):
            if request.user.is_superuser:
                compte_id = request.data.get('compte_id', compte_id)
            else:
                compte_id = request.user.compte_id

        if compte_id is not None:
            regles_du_compte = RegleGenerationLease.objects.filter(
                compte_id=compte_id,
                nom__iexact=nom_nettoye,
            )
            if self.instance:
                regles_du_compte = regles_du_compte.exclude(
                    pk=self.instance.pk
                )
            if regles_du_compte.exists():
                raise serializers.ValidationError(
                    "Une règle portant ce nom existe déjà pour ce compte."
                )

        return nom_nettoye

    def validate_cron_expression(self, value):
        """
        Valide que l'expression Cron correspond au format standard (5 parties).
        Vérifie également que cette expression est unique pour l'entreprise.
        """
        if value:
            expression = value.strip()

            # 1. Validation du format
            if (
                    len(expression.split()) != 5
                    or not croniter.croniter.is_valid(expression)
            ):
                raise serializers.ValidationError(
                    "Format Cron invalide. L'expression doit contenir 5 parties (ex: '0 12,22 * * *')."
                )

            # 2. 🛡️ Validation d'unicité (Empêcher les doublons CRON)
            request = self.context.get('request')
            compte_id = getattr(self.instance, 'compte_id', None)

            if request and getattr(request, 'user', None):
                if request.user.is_superuser:
                    compte_id = request.data.get('compte_id', compte_id)
                else:
                    compte_id = request.user.compte_id

            if compte_id is not None:
                regles_existantes = RegleGenerationLease.objects.filter(
                    compte_id=compte_id,
                    cron_expression=expression
                )
                if self.instance:
                    regles_existantes = regles_existantes.exclude(pk=self.instance.pk)

                if regles_existantes.exists():
                    raise serializers.ValidationError(
                        "Une règle utilisant cette expression Cron exacte existe déjà."
                    )

            return expression
        return value

    def validate(self, data):
        """
        Validation croisée (Logique métier globale).
        """
        frequence = data.get('frequence', getattr(self.instance, 'frequence', None))
        cron_expression = data.get('cron_expression', getattr(self.instance, 'cron_expression', None))

        if frequence == Schedule.CRON:
            if not cron_expression or cron_expression.strip() == "":
                raise serializers.ValidationError({
                    "cron_expression": "L'expression Cron est requise lorsque la fréquence est réglée sur 'Expression Cron'."
                })
        else:
            # Si on change de fréquence (ex: on passe de CRON à DAILY), on vide l'expression Cron
            data['cron_expression'] = None

        return data
