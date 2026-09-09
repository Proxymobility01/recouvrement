import django.contrib.postgres.indexes
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('recouvrement', '0049_agence_zone_recherche'),
    ]

    operations = [
        migrations.CreateModel(
            name='Proprietaire',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('compte_id', models.IntegerField(db_index=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('nom_complet', models.CharField(max_length=255)),
                (
                    'nom_complet_search',
                    models.CharField(
                        blank=True,
                        editable=False,
                        max_length=255,
                    ),
                ),
                ('actif', models.BooleanField(default=True)),
            ],
            options={
                'verbose_name': 'Propriétaire',
                'verbose_name_plural': 'Propriétaires',
                'db_table': 'rc_proprietaire',
                'indexes': [
                    django.contrib.postgres.indexes.GinIndex(
                        fields=['nom_complet_search'],
                        name='idx_prop_nom_search_trgm',
                        opclasses=['gin_trgm_ops'],
                    ),
                    models.Index(
                        fields=['compte_id', 'actif'],
                        name='idx_prop_compte_actif',
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name='CompteReceptionProprietaire',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('compte_id', models.IntegerField(db_index=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'operateur',
                    models.CharField(
                        choices=[
                            ('ORANGE', 'Orange Money'),
                            ('MTN', 'MTN Mobile Money'),
                        ],
                        max_length=10,
                    ),
                ),
                ('numero', models.CharField(max_length=20)),
                (
                    'nom_titulaire',
                    models.CharField(blank=True, max_length=255),
                ),
                (
                    'nom_titulaire_search',
                    models.CharField(
                        blank=True,
                        editable=False,
                        max_length=255,
                    ),
                ),
                ('actif', models.BooleanField(default=True)),
                (
                    'proprietaire',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='comptes_reception',
                        to='recouvrement.proprietaire',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Compte de réception du propriétaire',
                'verbose_name_plural': (
                    'Comptes de réception des propriétaires'
                ),
                'db_table': 'rc_compte_reception_proprietaire',
                'indexes': [
                    models.Index(
                        fields=['numero'],
                        name='idx_recept_numero',
                    ),
                    models.Index(
                        fields=['compte_id', 'operateur', 'actif'],
                        name='idx_recept_cpte_oper_actif',
                    ),
                    django.contrib.postgres.indexes.GinIndex(
                        fields=['nom_titulaire_search'],
                        name='idx_recept_tit_search_trgm',
                        opclasses=['gin_trgm_ops'],
                    ),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('proprietaire', 'operateur'),
                        name='uniq_recept_prop_oper',
                    ),
                    models.UniqueConstraint(
                        fields=('compte_id', 'operateur', 'numero'),
                        name='uniq_recept_num_oper_compte',
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name='contrat',
            name='proprietaire',
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    'Propriétaire qui reçoit les paiements de ce contrat. '
                    'Le champ reste nullable pour permettre la reprise des '
                    'contrats existants.'
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='contrats',
                to='recouvrement.proprietaire',
            ),
        ),
        migrations.AddIndex(
            model_name='contrat',
            index=models.Index(
                fields=['compte_id', 'proprietaire'],
                name='idx_contrat_tenant_prop',
            ),
        ),
    ]
