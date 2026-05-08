from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from core.errors import ErrorCodes
from core.exceptions import CustomAPIException
from recouvrement.models import Contrat, Lease, Paiement, TypeContrat



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

    def validate_code(self, value):
        """
        Force le formatage du code et vérifie l'unicité par compte.
        """
        code_formate = value.strip().upper()

        request = self.context.get('request')
        if request and request.user:
            compte_id = request.user.compte_id

            qs = TypeContrat.objects.filter(code=code_formate, compte_id=compte_id)

            if self.instance:
                qs = qs.exclude(id=self.instance.id)

            if qs.exists():
                raise serializers.ValidationError(f"Le code '{code_formate}' existe déjà dans votre espace.")

        return code_formate

    def validate_libelle(self, value):
        """
        Nettoie le libellé et vérifie qu'aucun autre type de contrat
        ne porte le même nom (insensible à la casse) pour ce compte.
        """
        # On supprime les espaces inutiles au début et à la fin
        libelle_formate = value.strip()

        request = self.context.get('request')
        if request and request.user:
            compte_id = request.user.compte_id

            # 🚀 Utilisation de __iexact pour éviter les doublons type "Moto" vs "moto"
            qs = TypeContrat.objects.filter(libelle__iexact=libelle_formate, compte_id=compte_id)

            # Si c'est une modification, on exclut la ligne actuelle
            if self.instance:
                qs = qs.exclude(id=self.instance.id)

            if qs.exists():
                raise serializers.ValidationError(f"Le type de contrat '{libelle_formate}' existe déjà dans votre espace.")

        # On retourne la valeur formatée (ex: "Traceur GPS" sans espaces superflus)
        # On pourrait aussi faire libelle_formate.capitalize() si tu veux forcer la majuscule !
        return libelle_formate


class SousContratSerializer(serializers.ModelSerializer):
    """
    Serializer utilisé UNIQUEMENT en lecture pour afficher les enfants
    dans le détail du parent, OU lors de la création groupée.
    """
    specificites = serializers.JSONField(required=False, allow_null=True)
    montant_verse = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        read_only=True
    )

    class Meta:
        model = Contrat
        fields = [
            'id','reference', 'type_contrat', 'montant_total','montant_restant','montant_verse', 'montant_par_paiement',
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
        """
        Vérifie qu'on n'essaie pas d'utiliser un type principal (ex: Véhicule)
        pour créer un sous-contrat (accessoire).
        """
        if value.est_principal:
            raise serializers.ValidationError(
                f"Le type '{value.libelle}' est un contrat principal. Il ne peut pas être utilisé comme sous-contrat."
            )
        return value



class ContratSerializer(serializers.ModelSerializer):
    enregistre_par_nom_complet = serializers.CharField(source='enregistre_par.nom_complet', read_only=True)
    chauffeur_nom_complet = serializers.CharField(source='chauffeur.nom_complet', read_only=True)
    specificites = serializers.JSONField(required=False, allow_null=True)
    type_contrat_libelle = serializers.CharField(source='type_contrat.nom', read_only=True)
    montant_verse = serializers.DecimalField(max_digits=12,decimal_places=2,read_only=True)

    class Meta:
        model = Contrat
        fields = [
            'id', 'reference', 'compte_id', 'chauffeur', 'immatriculation', 'vin', 'nom_complet',
            'type_contrat','type_contrat_libelle', 'parent',
            'enregistre_par', 'enregistre_par_nom_complet', 'chauffeur_nom_complet',
            'montant_total', 'montant_restant','montant_verse', 'montant_par_paiement',
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
        """
        Si ce Serializer est utilisé pour créer un contrat principal (pas de parent défini),
        le type de contrat DOIT être principal.
        """
        # On vérifie si c'est une création de contrat principal
        # (self.initial_data ne contient pas de 'parent' ou self.instance n'a pas de parent)
        parent_id = self.initial_data.get('parent')
        is_updating_sub_contract = self.instance and self.instance.parent is not None

        if not parent_id and not is_updating_sub_contract:
            if not value.est_principal:
                raise serializers.ValidationError(
                    f"Le type '{value.libelle}' est un accessoire. Il ne peut pas être utilisé comme contrat principal."
                )
        return value

    def validate(self, attrs):
        # 1. Validation Financière
        montant_total = attrs.get('montant_total', getattr(self.instance, 'montant_total', None))
        montant_par_paiement = attrs.get('montant_par_paiement', getattr(self.instance, 'montant_par_paiement', None))

        if montant_par_paiement and montant_total and montant_par_paiement > montant_total:
            raise serializers.ValidationError({
                "montant_par_paiement": "L'échéance ne peut pas être supérieure au montant total."
            })

        # 2. Validation Temporelle
        date_debut = attrs.get('date_debut', getattr(self.instance, 'date_debut', None))
        date_fin = attrs.get('date_fin', getattr(self.instance, 'date_fin', None))

        if date_debut and date_fin and date_fin < date_debut:
            raise serializers.ValidationError({
                "date_fin": "La date de fin ne peut pas précéder la date de début."
            })

        return attrs

    def create(self, validated_data):
        # 🚀 L'astuce magique : On accepte les sous-contrats lors de la création
        # "initial_data" contient le JSON brut envoyé par le Front-End avant la validation stricte.
        sous_contrats_data = self.initial_data.pop('sous_contrats', [])

        chauffeur = validated_data.get('chauffeur')
        if chauffeur:
            validated_data['nom_complet'] = chauffeur.nom_complet or "Nom pas défini"

        validated_data['montant_restant'] = validated_data.get('montant_total')
        validated_data['statut'] = Contrat.STATUT_ACTIF

        with transaction.atomic():
            # 1. Création du parent
            parent_contrat = super().create(validated_data)

            # 2. Création des enfants à partir des données brutes
            for sc_data in sous_contrats_data:
                # On réutilise le SousContratSerializer pour valider l'enfant !
                sc_serializer = SousContratSerializer(data=sc_data)
                sc_serializer.is_valid(raise_exception=True)

                # On injecte l'ADN du parent
                sc_instance_data = sc_serializer.validated_data
                sc_instance_data['parent'] = parent_contrat
                sc_instance_data['chauffeur'] = parent_contrat.chauffeur
                sc_instance_data['compte_id'] = parent_contrat.compte_id
                sc_instance_data['nom_complet'] = parent_contrat.nom_complet
                sc_instance_data['enregistre_par'] = parent_contrat.enregistre_par
                sc_instance_data['statut'] = Contrat.STATUT_ACTIF
                sc_instance_data['montant_restant'] = sc_instance_data.get('montant_total')

                Contrat.objects.create(**sc_instance_data)

        return parent_contrat

    def update(self, instance, validated_data):
        """
        Mise à jour UNITAIRE d'un contrat (Parent ou Enfant, peu importe)
        """
        # 1. On interdit de reculer l'échéance via une modification manuelle (Sécurité)
        validated_data.pop('prochaine_echeance', None)

        # 2. RÈGLE MÉTIER : Si c'est un sous-contrat, on ignore les données de véhicule
        # Si 'instance.parent' n'est pas None, c'est que c'est un sous-contrat.
        if instance.parent is not None:
            validated_data.pop('immatriculation', None)
            validated_data.pop('vin', None)

        # 3. Mise à jour du nom du chauffeur si modifié
        chauffeur = validated_data.get('chauffeur')
        if chauffeur:
            validated_data['nom_complet'] = chauffeur.nom_complet

        # 4. Ajustement automatique du montant restant si le prix global change
        new_total = validated_data.get('montant_total')
        if new_total is not None and new_total != instance.montant_total:
            deja_paye = instance.montant_total - instance.montant_restant
            validated_data['montant_restant'] = max(0, new_total - deja_paye)

        # 5. Sauvegarde simple et efficace !
        return super().update(instance, validated_data)




class LeaseSerializer(serializers.ModelSerializer):
    # Informations complémentaires du contrat pour l'affichage en liste
    chauffeur_nom_complet = serializers.CharField(source='contrat.nom_complet', read_only=True)
    contrat_id = serializers.IntegerField(source='contrat.id', read_only=True)
    compte_id = serializers.IntegerField(source='contrat.compte_id', read_only=True)
    reste_a_payer = serializers.SerializerMethodField()

    class Meta:
        model = Lease
        fields = [
            'id',
            'compte_id',
            'contrat_id',
            'chauffeur_nom_complet',
            'date_echeance',
            'montant_attendu',
            'montant_paye',
            'reste_a_payer',
            'statut',
            'created_at'
        ]
        # Tout est en lecture seule via cette API, car les baux sont générés par commande
        # et mis à jour par les paiements (Webhook/Service)
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
    """
    Ce sous-serializer s'occupe de valider UNE SEULE ligne de paiement.
    """
    lease_id = serializers.IntegerField()
    montant = serializers.DecimalField(max_digits=12, decimal_places=2)

    def validate_lease_id(self, value):
        user = self.context['request'].user
        try:
            # Sécurité Multi-Tenant et Rôle
            lease_query = Lease.objects.select_related('contrat').filter(id=value, contrat__compte_id=user.compte_id)
            if not user.is_staff and not user.is_superuser:
                lease_query = lease_query.filter(contrat__chauffeur=user)

            lease = lease_query.get()
        except Lease.DoesNotExist:
            raise CustomAPIException(
                resp_code=ErrorCodes.LEASE_NOT_FOUND_OR_DENIED,
                status_code=404,
                context=f"Échéance ID {value} introuvable ou accès refusé."
            )

        if lease.statut == Lease.STATUT_PAYE:
            raise CustomAPIException(
                resp_code=ErrorCodes.PAYMENT_ALREADY_PROCESSED,
                status_code=400,
                context=f"L'échéance {value} est déjà soldée."
            )

        return lease

    def validate(self, attrs):
        # 'lease_id' contient maintenant l'objet Lease lui-même grâce au validate_lease_id
        lease = attrs.get('lease_id')
        montant = attrs.get('montant')

        if montant <= 0:
            raise serializers.ValidationError({"montant": f"Le montant pour l'échéance #{lease.id} doit être positif."})

        reste_a_payer = lease.montant_attendu - lease.montant_paye
        if montant > reste_a_payer:
            raise serializers.ValidationError({
                "montant": f"Le montant ({montant}) dépasse le reste à payer ({reste_a_payer}) pour l'échéance #{lease.id}."
            })

        return attrs


class InitiationPaiementSerializer(serializers.Serializer):
    """
    Le serializer principal qui reçoit la requête globale d'initiation de paiement.
    """
    lignes = LignePaiementSerializer(many=True, allow_empty=False)
    phone_number = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate(self, attrs):
        lignes = attrs.get('lignes', [])

        # ==========================================
        # 1. PROTECTION CONTRE LES DOUBLONS
        # ==========================================
        lease_ids = [ligne['lease_id'].id for ligne in lignes]
        if len(lease_ids) != len(set(lease_ids)):
            raise CustomAPIException(
                resp_code=ErrorCodes.PAYMENT_DUPLICATE_LEASES,
                status_code=400,
                context="Doublons détectés dans le payload des échéances."
            )

        # ==========================================
        # 2. RÈGLE MÉTIER : MÊME GROUPE DE CONTRATS (Famille)
        # ==========================================
        root_parent_ids = set()
        for ligne in lignes:
            contrat = ligne['lease_id'].contrat
            root_id = contrat.parent_id if contrat.parent_id else contrat.id
            root_parent_ids.add(root_id)

        if len(root_parent_ids) > 1:
            raise CustomAPIException(
                resp_code=ErrorCodes.PAYMENT_MIXED_CONTRACTS,
                status_code=400,
                context=f"Mélange de contrats racines détecté: {root_parent_ids}"
            )

        # ==========================================
        # 3. RÈGLE MÉTIER : PAIEMENT CHRONOLOGIQUE GLOBAL
        # ==========================================
        if lignes:
            root_parent_id = list(root_parent_ids)[0]
            LeaseModel = lignes[0]['lease_id'].__class__
            derniere_date_panier = max(ligne['lease_id'].date_echeance for ligne in lignes)

            arrieres_impayes = LeaseModel.objects.filter(
                Q(contrat_id=root_parent_id) | Q(contrat__parent_id=root_parent_id),
                date_echeance__lt=derniere_date_panier
            ).exclude(
                statut=LeaseModel.STATUT_PAYE
            ).exclude(
                id__in=lease_ids
            ).select_related('contrat').order_by('date_echeance')

            if arrieres_impayes.exists():
                dates_bloquantes = [
                    f"{arriere.date_echeance.strftime('%d/%m/%Y')} ({arriere.contrat.nom_complet})"
                    for arriere in arrieres_impayes[:3]
                ]

                # On passe les dates formatées directement dans le message utilisateur si on veut,
                # ou on laisse un message générique. Ici on personnalise le message avec le contexte.
                raise CustomAPIException(
                    resp_code=ErrorCodes.PAYMENT_CHRONOLOGY_VIOLATION,
                    status_code=400,
                    context=f"Bloqué par: {', '.join(dates_bloquantes)}..."
                )

        return attrs


class PaiementSerializer(serializers.ModelSerializer):
    # 🚀 1. ALIASING : On expose "lease_id" au Front-End, mais ça tape dans "lease" en Python
    lease_id = serializers.PrimaryKeyRelatedField(
        queryset=Lease.objects.all(),
        source='lease'
    )

    chauffeur_nom_complet = serializers.CharField(source='contrat.nom_complet', read_only=True)
    enregistre_par = serializers.CharField(source='enregistre_par.nom_complet', read_only=True)

    class Meta:
        model = Paiement
        fields = [
            # 🚀 2. Remplacement dans les champs
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

        # 💡 Note : DRF a déjà traduit "lease_id" du JSON vers "lease" dans attrs grâce au 'source'
        lease = attrs.get('lease', getattr(self.instance, 'lease', None))
        montant = attrs.get('montant', getattr(self.instance, 'montant', None))

        if lease and lease.compte_id != user.compte_id:
            raise CustomAPIException(
                resp_code=ErrorCodes.CONTRACT_NOT_FOUND,
                status_code=404
            )

        if montant is not None and montant <= 0:
            raise serializers.ValidationError({
                "montant": "Le montant doit être strictement positif."
            })

        if lease and montant is not None:
            if self.instance:
                deja_paye_autres = lease.montant_paye - self.instance.montant
                reste_a_payer = lease.montant_attendu - deja_paye_autres
            else:
                if lease.statut == Lease.STATUT_PAYE:
                    raise CustomAPIException(
                        resp_code=ErrorCodes.PAYMENT_ALREADY_PROCESSED,
                        status_code=400,
                        context=f"Lease {lease.id} déjà soldé."
                    )
                reste_a_payer = lease.montant_attendu - lease.montant_paye

            if montant > reste_a_payer:
                raise serializers.ValidationError({
                    "montant": f"Le montant ({montant}) dépasse le reste à payer autorisé ({reste_a_payer})."
                })

        return attrs

    def create(self, validated_data):
        user = self.context['request'].user

        # 💡 La variable s'appelle toujours 'lease' ici
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
            if contrat.montant_restant == 0:
                contrat.statut = Contrat.STATUT_SOLDE
            contrat.save()

            return paiement

    def update(self, instance, validated_data):
        if instance.methode == Paiement.METHODE_MOBILE_MONEY:
            raise CustomAPIException(
                resp_code=ErrorCodes.PAYMENT_UPDATE_FORBIDDEN,
                status_code=403,
                context="Tentative de modification manuelle d'un paiement Mobile Money."
            )

        validated_data.pop('methode', None)
        validated_data.pop('lease', None)  # 💡 Toujours 'lease' en interne

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

                if contrat.montant_restant == 0:
                    contrat.statut = Contrat.STATUT_SOLDE
                elif contrat.statut == Contrat.STATUT_SOLDE and contrat.montant_restant > 0:
                    contrat.statut = Contrat.STATUT_ACTIF
                contrat.save()

        return super().update(instance, validated_data)