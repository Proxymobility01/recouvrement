from django.contrib import admin
from django.utils.html import format_html


class IdCompteAdminMixin:
    """Affiche l'identifiant de la ligne et son compte dans une colonne."""

    @admin.display(description='ID / Compte', ordering='id')
    def id_compte(self, obj):
        compte_id = obj.compte_id if obj.compte_id is not None else '—'
        return format_html(
            '<strong>#{}</strong><br>'
            '<span style="color:#888;">Compte {}</span>',
            obj.pk,
            compte_id,
        )
