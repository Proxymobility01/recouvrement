from rest_framework.permissions import BasePermission, DjangoModelPermissions

class StrictDjangoModelPermissions(DjangoModelPermissions):
    """
    Surcharge de DjangoModelPermissions pour exiger la permission
    'view' sur les requêtes GET/HEAD/OPTIONS.
    """
    perms_map = {
        'GET':     ['%(app_label)s.view_%(model_name)s'],
        'OPTIONS': ['%(app_label)s.view_%(model_name)s'],
        'HEAD':    ['%(app_label)s.view_%(model_name)s'],
        'POST':    ['%(app_label)s.add_%(model_name)s'],
        'PUT':     ['%(app_label)s.change_%(model_name)s'],
        'PATCH':   ['%(app_label)s.change_%(model_name)s'],
        'DELETE':  ['%(app_label)s.delete_%(model_name)s'],
    }


class CanAssignRuleToContracts(BasePermission):
    """
    Autorise une action d'assignation uniquement aux utilisateurs pouvant
    consulter la règle ciblée et modifier les contrats.
    """

    message = (
        "Vous devez pouvoir consulter cette règle et modifier les contrats "
        "pour effectuer cette assignation."
    )

    def has_permission(self, request, view):
        queryset = getattr(view, 'queryset', None)
        model = getattr(queryset, 'model', None)
        if model is None:
            return False

        opts = model._meta
        return request.user.has_perms([
            f'{opts.app_label}.view_{opts.model_name}',
            'recouvrement.change_contrat',
        ])


class CanExecuteLeaseGenerationRule(BasePermission):
    """Autorise le déclenchement manuel d'une règle de génération."""

    message = (
        "Vous devez pouvoir consulter et modifier les règles de génération "
        "pour lancer une exécution manuelle."
    )

    def has_permission(self, request, view):
        return request.user.has_perms([
            'recouvrement.view_reglegenerationlease',
            'recouvrement.change_reglegenerationlease',
        ])


class CanValidateUSSDPayment(BasePermission):
    """Autorise la validation ou le rejet d'une preuve de paiement USSD."""

    message = (
        "Vous devez pouvoir consulter les preuves de paiement USSD et "
        "avoir la permission de les valider pour effectuer cette action."
    )

    def has_permission(self, request, view):
        return request.user.has_perms([
            'recouvrement.view_preuvepaiementussd',
            'recouvrement.can_validate_ussd_payment',
        ])
