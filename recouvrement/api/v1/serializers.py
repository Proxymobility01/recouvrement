from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from recouvrement.models import Contrat, Lease, Paiement


class ContratSerializer(serializers.ModelSerializer):
    enregistre_par_nom_complet = serializers.CharField(source='enregistre_par.nom_complet', read_only=True)
    chauffeur_nom_complet = serializers.CharField(source='chauffeur.nom_complet', read_only=True)

    class Meta:
        model = Contrat
        fields = [
            'id', 'compte_id', 'chauffeur', 'immatriculation', 'nom_complet',
            'enregistre_par', 'enregistre_par_nom_complet', 'chauffeur_nom_complet',
            'montant_total', 'montant_restant', 'montant_par_paiement',
            'frequence', 'date_debut', 'date_fin', 'prochaine_echeance',
            'statut', 'created_at', 'updated_at'
        ]

        read_only_fields = [
            'statut', 'montant_restant', 'enregistre_par',
            'created_at', 'updated_at', 'nom_complet',
            'compte_id'

        ]

    def validate(self, attrs):
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
        chauffeur = validated_data.get('chauffeur')

        # 1. Synchronisation du nom
        if chauffeur:
            validated_data['nom_complet'] = chauffeur.nom_complet

        # 2. Initialisations automatiques de base
        validated_data['montant_restant'] = validated_data.get('montant_total')
        validated_data['statut'] = Contrat.STATUT_ACTIF

        # date_fin et prochaine_echeance sont gérés nativement par ModelSerializer
        # puisqu'ils sont fournis dans le body de la requête.

        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop('prochaine_echeance', None)
        chauffeur = validated_data.get('chauffeur')
        if chauffeur:
            validated_data['nom_complet'] = chauffeur.nom_complet

        # Si le montant total change, on ajuste intelligemment le montant restant
        new_total = validated_data.get('montant_total')
        if new_total is not None and new_total != instance.montant_total:
            deja_paye = instance.montant_total - instance.montant_restant
            validated_data['montant_restant'] = max(0, new_total - deja_paye)

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
            # On commence par construire la requête avec la sécurité Multi-Tenant
            lease_query = Lease.objects.filter(id=value, contrat__compte_id=user.compte_id)

            # 🛡️ LE CORRECTIF DE SÉCURITÉ EST ICI :
            # Si l'utilisateur n'est pas un administrateur du système,
            # on exige formellement qu'il soit le chauffeur titulaire du contrat.
            if not user.is_staff and not user.is_superuser:
                lease_query = lease_query.filter(contrat__chauffeur=user)

            # On exécute la requête finale
            lease = lease_query.get()

        except Lease.DoesNotExist:
            # Message générique pour ne pas donner d'indices à un attaquant
            raise serializers.ValidationError("Cette échéance est introuvable ou vous n'avez pas l'autorisation de la payer.")

        # 2. On vérifie que l'échéance n'est pas déjà soldée
        if lease.statut == Lease.STATUT_PAYE:
            raise serializers.ValidationError("Cette échéance a déjà été totalement payée.")

        return lease

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


class PaiementSerializer(serializers.ModelSerializer):
    # Affichage des noms pour le Front-End
    chauffeur_nom = serializers.CharField(source='contrat.nom_complet', read_only=True)
    encaisseur_nom = serializers.CharField(source='utilisateur.nom_complet', read_only=True)

    class Meta:
        model = Paiement
        fields = [
            'id', 'lease', 'montant', 'methode', 'reference',
            'transaction_id', 'statut', 'date_paiement',
            'chauffeur_nom', 'encaisseur_nom'
        ]
        # On ajoute 'methode' en read_only car elle est gérée par le serveur, pas par le body
        read_only_fields = [
            'methode', 'reference', 'transaction_id', 'statut',
            'date_paiement', 'chauffeur_nom', 'encaisseur_nom'
        ]

    def validate(self, attrs):
        user = self.context['request'].user
        lease = attrs.get('lease')
        montant = attrs.get('montant')

        # 1. 🛡️ SÉCURITÉ : Multi-Tenant & Droits
        if lease.contrat.compte_id != user.compte_id:
            raise serializers.ValidationError({"lease": "Cette échéance est introuvable."})

        # On vérifie que c'est un partenaire ou admin car c'est une route "Espèces"
        if not (user.is_staff or user.has_perm('recouvrement.can_validate_payment')):
            raise serializers.ValidationError(
                {"erreur": "Vous n'avez pas l'autorisation d'enregistrer un paiement en espèces."})

        # 2. 💰 VÉRIFICATIONS FINANCIÈRES
        if lease.statut == Lease.STATUT_PAYE:
            raise serializers.ValidationError({"lease": "Cette échéance est déjà totalement soldée."})

        if montant <= 0:
            raise serializers.ValidationError({"montant": "Le montant doit être positif."})

        reste_a_payer = lease.montant_attendu - lease.montant_paye
        if montant > reste_a_payer:
            raise serializers.ValidationError({
                "montant": f"Le montant dépasse le reste à payer ({reste_a_payer})."
            })

        return attrs

    def create(self, validated_data):
        user = self.context['request'].user
        lease = validated_data['lease']
        montant = validated_data['montant']

        # 🚀 INJECTION AUTOMATIQUE : On force les valeurs pour les Espèces
        validated_data['methode'] = Paiement.METHODE_ESPECES
        validated_data['contrat'] = lease.contrat
        validated_data['utilisateur'] = user
        validated_data['compte_id'] = user.compte_id
        validated_data['statut'] = Paiement.STATUT_VALIDE
        validated_data['date_paiement'] = timezone.now()

        # Génération de la référence avec ton format spécifique ESP.YYYYMMDD...
        validated_data['reference'] = Paiement.generer_reference_paiement(Paiement.METHODE_ESPECES)

        with transaction.atomic():
            # Création du paiement
            paiement = super().create(validated_data)

            # Mise à jour de l'échéance (Lease)
            lease.montant_paye += montant
            if lease.montant_paye >= lease.montant_attendu:
                lease.statut = Lease.STATUT_PAYE
            else:
                lease.statut = Lease.STATUT_PARTIEL
            lease.save()

            # Mise à jour de la dette globale (Contrat)
            contrat = lease.contrat
            contrat.montant_restant -= montant
            contrat.save()

            return paiement

    def update(self, instance, validated_data):
        # Sécurité : On ne modifie jamais un paiement Mobile Money ici
        if instance.methode == Paiement.METHODE_MOBILE_MONEY:
            raise serializers.ValidationError({"erreur": "Impossible de modifier un paiement Mobile Money."})

        # Protection des champs pivots
        validated_data.pop('methode', None)
        validated_data.pop('lease', None)

        nouveau_montant = validated_data.get('montant')

        if nouveau_montant is not None and nouveau_montant != instance.montant:
            difference = nouveau_montant - instance.montant
            lease = instance.lease
            contrat = lease.contrat

            if lease.montant_paye + difference > lease.montant_attendu:
                raise serializers.ValidationError({"montant": "Correction impossible : dépasse le montant attendu."})

            with transaction.atomic():
                # Ajustement Lease
                lease.montant_paye += difference
                lease.statut = Lease.STATUT_PAYE if lease.montant_paye >= lease.montant_attendu else Lease.STATUT_PARTIEL
                lease.save()

                # Ajustement Contrat
                contrat.montant_restant -= difference
                contrat.save()

        return super().update(instance, validated_data)