from decimal import Decimal

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
            validated_data['nom_complet'] = chauffeur.nom_complet or chauffeur.email or "Nom pas défini "

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

class CalendrierSerializer(serializers.ModelSerializer):
    chauffeur_nom = serializers.CharField(source='contrat.chauffeur.nom_complet', read_only=True, default="Inconnu")


    class Meta:
        model = Lease
        fields = [
            'id', 'date_echeance',
            'chauffeur_nom','statut',
        ]


# class InitiationPaiementSerializer(serializers.Serializer):
#     lease_id = serializers.IntegerField()
#     montant = serializers.DecimalField(max_digits=12, decimal_places=2)
#     phone_number = serializers.CharField(max_length=20, required=False, allow_blank=True)
#
#     def validate_lease_id(self, value):
#         user = self.context['request'].user
#         try:
#             lease_query = Lease.objects.filter(id=value, contrat__compte_id=user.compte_id)
#             if not user.is_staff and not user.is_superuser:
#                 lease_query = lease_query.filter(contrat__chauffeur=user)
#             lease = lease_query.get()
#
#         except Lease.DoesNotExist:
#             raise serializers.ValidationError("Cette échéance est introuvable ou vous n'avez pas l'autorisation de la payer.")
#         if lease.statut == Lease.STATUT_PAYE:
#             raise serializers.ValidationError("Cette échéance a déjà été totalement payée.")
#         return lease
#
#     def validate(self, attrs):
#         lease = attrs.get('lease_id')
#         montant = attrs.get('montant')
#         if montant <= 0:
#             raise serializers.ValidationError({"montant": "Le montant doit être strictement positif."})
#         reste_a_payer = lease.montant_attendu - lease.montant_paye
#         if montant > reste_a_payer:
#             raise serializers.ValidationError({
#                 "montant": f"Le montant ({montant}) dépasse le reste à payer pour cette échéance ({reste_a_payer})."
#             })
#         return attrs

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
            raise serializers.ValidationError(f"L'échéance #{value} est introuvable ou vous n'avez pas l'autorisation.")

        if lease.statut == Lease.STATUT_PAYE:
            raise serializers.ValidationError(f"L'échéance #{value} a déjà été totalement payée.")

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
    Le serializer principal qui reçoit la requête globale.
    """
    # On accepte une liste de lignes, et on interdit qu'elle soit vide
    lignes = LignePaiementSerializer(many=True, allow_empty=False)
    phone_number = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate(self, attrs):
        lignes = attrs.get('lignes', [])

        # 1. Protection contre les doublons
        # On vérifie que le Front-End n'a pas envoyé deux fois le même lease_id dans le même panier
        lease_ids = [ligne['lease_id'].id for ligne in lignes]
        if len(lease_ids) != len(set(lease_ids)):
            raise serializers.ValidationError(
                {"lignes": "Vous avez sélectionné plusieurs fois la même échéance dans votre panier."})

        # 2. Règle métier (Optionnelle mais recommandée)
        # On s'assure que toutes les échéances qu'il essaie de payer appartiennent bien au MÊME contrat.
        contrat_ids = set([ligne['lease_id'].contrat_id for ligne in lignes])
        if len(contrat_ids) > 1:
            raise serializers.ValidationError(
                {"lignes": "Toutes les échéances payées en une fois doivent appartenir au même contrat."})

        return attrs


class PaiementSerializer(serializers.ModelSerializer):
    chauffeur_nom_complet = serializers.CharField(source='contrat.nom_complet', read_only=True)
    enregistre_par = serializers.CharField(source='utilisateur.nom_complet', read_only=True)

    class Meta:
        model = Paiement
        fields = [
            'id', 'lease', 'montant', 'methode', 'reference',
            'transaction_id', 'statut', 'date_paiement',
            'chauffeur_nom_complet', 'enregistre_par'
        ]
        # 'methode' est read_only car elle est gérée par le serveur
        read_only_fields = [
            'methode', 'reference', 'transaction_id', 'statut',
            'date_paiement', 'chauffeur_nom_complet', 'enregistre_par'
        ]

    def validate(self, attrs):
        user = self.context['request'].user

        # On utilise get avec getattr pour supporter les requêtes PATCH (où tous les champs ne sont pas envoyés)
        lease = attrs.get('lease', getattr(self.instance, 'lease', None))
        montant = attrs.get('montant', getattr(self.instance, 'montant', None))

        # 1. 🛡️ SÉCURITÉ : Multi-Tenant & Droits
        if lease and lease.contrat.compte_id != user.compte_id:
            raise serializers.ValidationError({"lease": "Cette échéance est introuvable."})

        # Vérification du droit d'encaissement d'espèces
        if not (user.is_staff or user.has_perm('recouvrement.can_validate_payment')):
            raise serializers.ValidationError(
                {"erreur": "Vous n'avez pas l'autorisation de gérer des paiements en espèces."})

        if montant is not None and montant <= 0:
            raise serializers.ValidationError({"montant": "Le montant doit être strictement positif."})

        # 2. 💰 VÉRIFICATIONS FINANCIÈRES INTELLIGENTES (Création vs Mise à jour)
        if lease and montant is not None:
            if self.instance:
                # --- MODE MISE À JOUR (UPDATE) ---
                # On calcule le reste à payer en ignorant le montant de CE paiement actuel
                deja_paye_autres = lease.montant_paye - self.instance.montant
                reste_a_payer = lease.montant_attendu - deja_paye_autres
            else:
                # --- MODE CRÉATION (CREATE) ---
                if lease.statut == Lease.STATUT_PAYE:
                    raise serializers.ValidationError({"lease": "Cette échéance est déjà totalement soldée."})
                reste_a_payer = lease.montant_attendu - lease.montant_paye

            # Vérification finale commune
            if montant > reste_a_payer:
                raise serializers.ValidationError({
                    "montant": f"Le montant ({montant}) dépasse le reste à payer autorisé ({reste_a_payer})."
                })

        return attrs

    def create(self, validated_data):
        user = self.context['request'].user
        lease = validated_data['lease']
        montant = validated_data['montant']

        # 🚀 INJECTION AUTOMATIQUE : Valeurs pour Espèces
        validated_data['methode'] = Paiement.METHODE_ESPECES
        validated_data['contrat'] = lease.contrat
        validated_data['utilisateur'] = user
        validated_data['compte_id'] = user.compte_id
        validated_data['statut'] = Paiement.STATUT_VALIDE
        validated_data['date_paiement'] = timezone.now()

        # Génération de la référence (ESP.YYYYMMDD...)
        validated_data['reference'] = Paiement.generer_reference_paiement(Paiement.METHODE_ESPECES)

        with transaction.atomic():
            paiement = super().create(validated_data)

            # Ajustement Lease
            lease.montant_paye += montant
            lease.statut = Lease.STATUT_PAYE if lease.montant_paye >= lease.montant_attendu else Lease.STATUT_PARTIEL
            lease.save()

            # Ajustement Contrat
            contrat = lease.contrat
            contrat.montant_restant = max(contrat.montant_restant - montant, Decimal('0.00'))
            if contrat.montant_restant == 0:
                contrat.statut = Contrat.STATUT_SOLDE
            contrat.save()

            return paiement

    def update(self, instance, validated_data):
        # Sécurité : Un paiement Mobile Money est intouchable depuis cette route
        if instance.methode == Paiement.METHODE_MOBILE_MONEY:
            raise serializers.ValidationError(
                {"erreur": "Impossible de modifier un paiement Mobile Money manuellement."})

        # Protection : On ne peut pas déplacer le paiement sur une autre échéance ni changer sa nature
        validated_data.pop('methode', None)
        validated_data.pop('lease', None)

        nouveau_montant = validated_data.get('montant')

        # ⚖️ RECALCUL COMPTABLE (Correction)
        if nouveau_montant is not None and nouveau_montant != instance.montant:
            difference = nouveau_montant - instance.montant
            lease = instance.lease
            contrat = lease.contrat

            with transaction.atomic():
                # On ajuste le Lease avec la différence mathématique (+ ou -)
                lease.montant_paye += difference
                lease.statut = Lease.STATUT_PAYE if lease.montant_paye >= lease.montant_attendu else Lease.STATUT_PARTIEL
                lease.save()
                contrat.montant_restant = max(contrat.montant_restant - difference, 0)

                # Gestion dynamique du statut en cas de correction
                if contrat.montant_restant == 0:
                    contrat.statut = Contrat.STATUT_SOLDE
                elif contrat.statut == Contrat.STATUT_SOLDE and contrat.montant_restant > 0:
                    contrat.statut = Contrat.STATUT_ACTIF
                contrat.save()

        return super().update(instance, validated_data)