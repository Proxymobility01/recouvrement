from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from decimal import Decimal
from recouvrement.models import Contrat, Lease, Paiement, TypeContrat, Parametre


class TypeContratSerializer(serializers.ModelSerializer):
    class Meta:
        model = TypeContrat
        fields = [
            'id',
            'libelle',
            'code',
            'est_principal',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

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


class SousContratSerializer(serializers.ModelSerializer):
    """
    Serializer utilisé UNIQUEMENT en lecture pour afficher les enfants
    dans le détail du parent, OU lors de la création groupée.
    """
    specificites = serializers.JSONField(required=False, allow_null=True)
    montant_paye = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)

    class Meta:
        model = Contrat
        fields = [
            'id','reference', 'type_contrat', 'montant_total','montant_restant','montant_paye', 'montant_par_paiement',
            'frequence', 'date_debut', 'date_fin', 'prochaine_echeance',
             'statut', 'specificites'
        ]
        read_only_fields = ['id', 'reference', 'statut', 'montant_restant']
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


class ContratSerializer(serializers.ModelSerializer):
    enregistre_par_nom_complet = serializers.CharField(source='enregistre_par.nom_complet', read_only=True)
    chauffeur_nom_complet = serializers.CharField(source='chauffeur.nom_complet', read_only=True)
    specificites = serializers.JSONField(required=False, allow_null=True)
    type_contrat_libelle = serializers.CharField(source='type_contrat.libelle', read_only=True)
    montant_paye = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)

    class Meta:
        model = Contrat
        fields = [
            'id', 'reference', 'compte_id', 'chauffeur', 'immatriculation', 'vin', 'nom_complet',
            'type_contrat','type_contrat_libelle', 'parent',
            'enregistre_par', 'enregistre_par_nom_complet', 'chauffeur_nom_complet',
            'montant_total', 'montant_restant','montant_paye', 'montant_par_paiement',
            'frequence', 'date_debut', 'date_fin', 'prochaine_echeance',
            'statut','specificites', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'reference', 'statut', 'montant_restant', 'enregistre_par',
            'created_at', 'updated_at', 'nom_complet', 'compte_id'
        ]
        extra_kwargs = {
            'montant_total': {'required': True},
            'montant_par_paiement': {'required': True},
            'frequence': {'required': True},
            'date_debut': {'required': True},
            'prochaine_echeance': {'required': True},
            'date_fin': {'required': True},
            'vin':{'required': True,'allow_blank': False,'allow_null': False},
            'immatriculation': {'required': True,'allow_blank': False,'allow_null': False},
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

    def get_fields(self):
        fields = super().get_fields()
        if self.instance and 'montant_paye' in fields:
            fields['montant_paye'].read_only = True
        return fields

    def validate(self, attrs):
        montant_total = attrs.get('montant_total', getattr(self.instance, 'montant_total', None))
        montant_par_paiement = attrs.get('montant_par_paiement', getattr(self.instance, 'montant_par_paiement', None))
        montant_paye = attrs.get('montant_paye', getattr(self.instance, 'montant_paye', 0))

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
        validated_data['statut'] = Contrat.STATUT_SOLDE if validated_data['montant_restant'] == 0 else Contrat.STATUT_ACTIF

        with transaction.atomic():
            parent_contrat = super().create(validated_data)
            for sc_data in sous_contrats_data:
                sc_serializer = SousContratSerializer(data=sc_data)
                sc_serializer.is_valid(raise_exception=True)

                sc_instance_data = sc_serializer.validated_data
                sc_instance_data['parent'] = parent_contrat
                sc_instance_data['chauffeur'] = parent_contrat.chauffeur
                sc_instance_data['compte_id'] = parent_contrat.compte_id
                sc_instance_data['nom_complet'] = parent_contrat.nom_complet
                sc_instance_data['enregistre_par'] = parent_contrat.enregistre_par

                sc_total = sc_instance_data.get('montant_total', Decimal('0.00'))
                sc_avance = sc_instance_data.get('montant_paye', Decimal('0.00'))
                sc_instance_data['montant_restant'] = max(Decimal('0.00'), sc_total - sc_avance)
                sc_instance_data['statut'] = Contrat.STATUT_SOLDE if sc_instance_data['montant_restant'] == 0 else Contrat.STATUT_ACTIF
                Contrat.objects.create(**sc_instance_data)

        return parent_contrat

    def update(self, instance, validated_data):
        validated_data.pop('prochaine_echeance', None)

        if instance.parent is not None:
            validated_data.pop('immatriculation', None)
            validated_data.pop('vin', None)

        chauffeur = validated_data.get('chauffeur')
        if chauffeur:
            validated_data['nom_complet'] = chauffeur.nom_complet


        new_total = validated_data.get('montant_total')
        if new_total is not None and new_total != instance.montant_total:
            deja_paye = instance.montant_paye
            validated_data['montant_restant'] = max(0, new_total - deja_paye)

        return super().update(instance, validated_data)


class LeaseSerializer(serializers.ModelSerializer):
    chauffeur_nom_complet = serializers.CharField(source='contrat.nom_complet', read_only=True)
    contrat_id = serializers.IntegerField(source='contrat.id', read_only=True)
    compte_id = serializers.IntegerField(source='contrat.compte_id', read_only=True)
    type_contrat_libelle = serializers.CharField(source='contrat.type_contrat.libelle', read_only=True)
    reste_a_payer = serializers.SerializerMethodField()

    class Meta:
        model = Lease
        fields = [
            'id',
            'compte_id',
            'contrat_id',
            'chauffeur_nom_complet',
            'type_contrat_libelle',
            'date_echeance',
            'montant_attendu',
            'montant_paye',
            'reste_a_payer',
            'statut',
            'created_at'
        ]
        read_only_fields = fields

    def get_reste_a_payer(self, obj):
        return obj.montant_attendu - obj.montant_paye


class CalendrierSerializer(serializers.ModelSerializer):
    chauffeur_nom = serializers.CharField(source='contrat.chauffeur.nom_complet', read_only=True, default="Inconnu")


    class Meta:
        model = Lease
        fields = [
            'id', 'date_echeance',
            'chauffeur_nom','statut',
        ]


class LignePaiementSerializer(serializers.Serializer):
    lease_id = serializers.IntegerField()
    montant = serializers.DecimalField(max_digits=12, decimal_places=2)

    def validate_lease_id(self, value):
        user = self.context['request'].user
        try:
            lease_query = Lease.objects.select_related('contrat').filter(id=value, contrat__compte_id=user.compte_id)
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
    lignes = LignePaiementSerializer(many=True, allow_empty=False)
    phone_number = serializers.CharField(max_length=20, required=True, allow_blank=False)

    def validate(self, attrs):
        lignes = attrs.get('lignes', [])

        lease_ids = [ligne['lease_id'].id for ligne in lignes]
        if len(lease_ids) != len(set(lease_ids)):
            raise serializers.ValidationError({"lignes": "Doublons détectés."})

        root_parent_ids = set()
        for ligne in lignes:
            contrat = ligne['lease_id'].contrat
            root_id = contrat.parent_id if contrat.parent_id else contrat.id
            root_parent_ids.add(root_id)

        if len(root_parent_ids) > 1:
            raise serializers.ValidationError({"lignes": "Mélange de contrats racines interdit."})

        if lignes:
            root_parent_id = list(root_parent_ids)[0]
            LeaseModel = lignes[0]['lease_id'].__class__
            derniere_date_panier = max(ligne['lease_id'].date_echeance for ligne in lignes)

            arrieres_impayes = LeaseModel.objects.filter(
                Q(contrat_id=root_parent_id) | Q(contrat__parent_id=root_parent_id),
                date_echeance__lt=derniere_date_panier
            ).exclude(statut=LeaseModel.STATUT_PAYE).exclude(id__in=lease_ids)

            if arrieres_impayes.exists():
                raise serializers.ValidationError({"lignes": "Des arriérés plus anciens bloquent ce paiement."})

        return attrs


class PaiementSerializer(serializers.ModelSerializer):
    lease_id = serializers.PrimaryKeyRelatedField(
        queryset=Lease.objects.all(),
        source='lease'
    )

    chauffeur_nom_complet = serializers.CharField(source='contrat.nom_complet', read_only=True)
    enregistre_par = serializers.CharField(source='enregistre_par.nom_complet', read_only=True)

    class Meta:
        model = Paiement
        fields = [
            'id', 'lease_id', 'montant', 'methode', 'reference',
            'transaction_id', 'statut', 'date_paiement',
            'chauffeur_nom_complet', 'enregistre_par'
        ]
        read_only_fields = [
            'methode', 'reference', 'transaction_id', 'statut',
            'date_paiement', 'chauffeur_nom_complet', 'enregistre_par'
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

        validated_data['reference'] = Paiement.generer_reference_paiement(Paiement.METHODE_ESPECES)

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