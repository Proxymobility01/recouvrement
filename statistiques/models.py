from django.db import models
from accounts.models import BaseModel


class StatistiqueJournaliere(BaseModel):
    date = models.DateField()

    # Finances
    montant_attendu = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    montant_collecte = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    montant_echec = models.DecimalField(max_digits=15, decimal_places=2, default=0)

    # Chauffeurs
    total_attendus = models.IntegerField(default=0)
    ayant_verse = models.IntegerField(default=0)
    n_ayant_pas_verse = models.IntegerField(default=0)


    class Meta:
        db_table = "rc_stat_journaliere"
        constraints = [
            models.UniqueConstraint(fields=['compte_id', 'date'], name='unique_stat_par_compte_et_date')
        ]
        indexes = [
            models.Index(fields=['compte_id', '-date'], name='idx_stat_tenant_date'),
        ]

    def __str__(self):
        return f"Stats du {self.date} - Compte {self.compte_id}"