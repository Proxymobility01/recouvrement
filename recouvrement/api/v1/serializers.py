from rest_framework import serializers
from recouvrement.models import Contrat, Lease


class ContratSerializer(serializers.ModelSerializer):
    enregistre_par_nom_complet = serializers.CharField(source='enregistre_par.nom_complet', read_only=True)
    chauffeur_nom_complet = serializers.CharField(source='chauffeur.nom_complet', read_only=True)
    class Meta:
        model = Contrat
        fields = [
            'id',
            'compte_id',
            'chauffeur',
            'immatriculation',
            'enregistre_par',
            'enregistre_par_nom_complet',
            'chauffeur_nom_complet',
            'montant_total',
            'montant_restant',
            'montant_par_paiement',
            'frequence',
            'date_fin',
            'prochaine_echeance',
            'statut',
            'created_at'
        ]

        read_only_fields = [
            'statut',
            'montant_restant',
            'enregistre_par',
            'created_at',
            'updated_at',
            'nom_complet',
            'compte_id',
        ]

    def validate(self, attrs):
        """Validation métier avant sauvegarde."""
        montant_total = attrs.get('montant_total', getattr(self.instance, 'montant_total', None))
        montant_par_paiement = attrs.get('montant_par_paiement', getattr(self.instance, 'montant_par_paiement', None))

        if montant_par_paiement and montant_total and montant_par_paiement > montant_total:
            raise serializers.ValidationError({
                "montant_par_paiement": "L'échéance ne peut pas être supérieure au montant total."
            })

        date_debut = attrs.get('date_debut', getattr(self.instance, 'date_debut', None))
        date_fin = attrs.get('date_fin', getattr(self.instance, 'date_fin', None))

        if date_debut and date_fin and date_fin < date_debut:
            raise serializers.ValidationError({
                "date_fin": "La date de fin ne peut pas précéder la date de début."
            })

        return attrs

    def create(self, validated_data):
        """Initialisation automatique à la création."""
        # 1. On récupère le chauffeur sélectionné
        chauffeur = validated_data.get('chauffeur')

        # 2. On synchronise le nom complet du contrat avec celui du compte utilisateur
        if chauffeur:
            validated_data['nom_complet'] = chauffeur.nom_complet

        # 3. Calculs financiers de base
        validated_data['montant_restant'] = validated_data.get('montant_total')
        validated_data['statut'] = Contrat.STATUT_ACTIF

        return super().create(validated_data)


    def update(self, instance, validated_data):
        """Mise à jour automatique et protection des champs."""
        validated_data.pop('prochaine_echeance', None)

        chauffeur = validated_data.get('chauffeur')
        if chauffeur:
            validated_data['nom_complet'] = chauffeur.nom_complet

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


class InitiationPaiementSerializer(serializers.Serializer):
    lease_id = serializers.IntegerField()
    montant = serializers.DecimalField(max_digits=12, decimal_places=2)
    phone_number = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate_lease_id(self, value):
        # 1. On récupère l'utilisateur connecté via le contexte du serializer
        user = self.context['request'].user

        try:
            # Sécurité Multi-Tenant : On s'assure que le lease appartient bien
            # à l'entreprise de l'utilisateur qui fait la requête.
            lease = Lease.objects.get(id=value, contrat__compte_id=user.compte_id)
        except Lease.DoesNotExist:
            raise serializers.ValidationError("Cette échéance est introuvable ou ne vous appartient pas.")

        # 2. On vérifie que l'échéance n'est pas déjà soldée
        if lease.statut == Lease.STATUT_PAYE:
            raise serializers.ValidationError("Cette échéance a déjà été totalement payée.")

        return lease  # On retourne l'objet directement, c'est plus pratique pour la suite

    def validate(self, attrs):
        # 'lease_id' contient maintenant l'objet Lease grâce à validate_lease_id
        lease = attrs.get('lease_id')
        montant = attrs.get('montant')

        if montant <= 0:
            raise serializers.ValidationError({"montant": "Le montant doit être strictement positif."})

        # 3. Vérification financière stricte
        reste_a_payer = lease.montant_attendu - lease.montant_paye
        if montant > reste_a_payer:
            raise serializers.ValidationError({
                "montant": f"Le montant ({montant}) dépasse le reste à payer pour cette échéance ({reste_a_payer})."
            })

        return attrs