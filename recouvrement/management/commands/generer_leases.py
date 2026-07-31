from django.core.management.base import BaseCommand, CommandError

from recouvrement.models import RegleGenerationLease
from recouvrement.services import (
    generer_leases_pour_regle,
    normaliser_limite_generation,
)


class Command(BaseCommand):
    help = (
        "Génère les leases exigibles à partir des règles de génération. "
        "Sans limite, l'instant courant est utilisé."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--regle',
            type=int,
            help=(
                "ID de la règle à traiter. Si absent, toutes les règles "
                "actives sont traitées."
            ),
        )
        groupe_limite = parser.add_mutually_exclusive_group()
        groupe_limite.add_argument(
            '--jusqua',
            type=str,
            help=(
                "Datetime limite ISO 8601 inclusif, par exemple "
                "2026-07-29T22:00:00. Sans fuseau, le fuseau Django est utilisé."
            ),
        )
        groupe_limite.add_argument(
            '--date',
            type=str,
            help=(
                "Compatibilité avec l'ancienne commande : date YYYY-MM-DD "
                "interprétée jusqu'à 23:59:59.999999."
            ),
        )

    def handle(self, *args, **options):
        try:
            if options.get('jusqua'):
                limite = normaliser_limite_generation(options['jusqua'])
            elif options.get('date'):
                limite = normaliser_limite_generation(
                    options['date'],
                    fin_de_jour=True,
                )
            else:
                limite = normaliser_limite_generation()
        except (TypeError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        regle_id = options.get('regle')
        regles = RegleGenerationLease.objects.filter(actif=True)

        if regle_id is not None:
            regles = regles.filter(pk=regle_id)
            if not regles.exists():
                raise CommandError(
                    f"La règle active d'ID {regle_id} est introuvable."
                )

        regle_ids = list(regles.order_by('id').values_list('id', flat=True))
        if not regle_ids:
            self.stdout.write(
                self.style.WARNING("Aucune règle de génération active.")
            )
            return

        self.stdout.write(
            self.style.WARNING(
                f"--- Génération des leases jusqu'au {limite.isoformat()} ---"
            )
        )

        totaux = {
            'contrats_cibles': 0,
            'leases_crees': 0,
            'doublons_ignores': 0,
            'occurrences_repos_ignorees': 0,
            'contrats_termines': 0,
            'erreurs': 0,
        }

        for identifiant in regle_ids:
            try:
                resultat = generer_leases_pour_regle(
                    regle_id=identifiant,
                    jusqu_a=limite,
                )
            except RegleGenerationLease.DoesNotExist:
                # La règle a pu être supprimée entre la sélection et le traitement.
                self.stderr.write(
                    self.style.WARNING(
                        f"Règle {identifiant} supprimée avant son traitement."
                    )
                )
                continue

            for cle in totaux:
                totaux[cle] += resultat[cle]

            self.stdout.write(
                f"Règle #{identifiant} « {resultat['regle']} » : "
                f"{resultat['leases_crees']} créé(s), "
                f"{resultat['doublons_ignores']} doublon(s), "
                f"{resultat['occurrences_repos_ignorees']} repos ignoré(s), "
                f"{resultat['erreurs']} erreur(s)."
            )

        style = self.style.ERROR if totaux['erreurs'] else self.style.SUCCESS
        self.stdout.write(
            style(
                "--- Terminé : "
                f"{totaux['leases_crees']} lease(s) créé(s), "
                f"{totaux['doublons_ignores']} doublon(s) ignoré(s), "
                f"{totaux['occurrences_repos_ignorees']} occurrence(s) de repos, "
                f"{totaux['contrats_termines']} contrat(s) sans prochaine occurrence, "
                f"{totaux['erreurs']} erreur(s). ---"
            )
        )
