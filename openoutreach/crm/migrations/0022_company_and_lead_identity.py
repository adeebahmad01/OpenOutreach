"""The Company table, and the Lead identity the export hands to other tools.

Purely additive and nullable throughout: existing rows take ``NULL``, nothing is
rewritten, and no backfill is attempted because there is nothing to backfill from — the
Lead Finder row was never retained, and ``profile_text`` is a lowercased space-join with
no delimiters, so a company name cannot be carved back out of it. Leads discovered
before this migration keep a null name and no company, for good.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0021_remove_deal_email_message_id_deal_thread'),
    ]

    operations = [
        migrations.CreateModel(
            name='Company',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=255, unique=True)),
                ('name', models.CharField(blank=True, default=None, max_length=200, null=True)),
                ('domain', models.CharField(blank=True, default=None, max_length=200, null=True)),
                ('creation_date', models.DateTimeField(auto_now_add=True)),
                ('update_date', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Company',
                'verbose_name_plural': 'Companies',
            },
        ),
        migrations.AddField(
            model_name='lead',
            name='first_name',
            field=models.CharField(blank=True, default=None, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='lead',
            name='full_name',
            field=models.CharField(blank=True, default=None, max_length=200, null=True),
        ),
        migrations.AddField(
            model_name='lead',
            name='job_title',
            field=models.CharField(blank=True, default=None, max_length=200, null=True),
        ),
        migrations.AddField(
            model_name='lead',
            name='last_name',
            field=models.CharField(blank=True, default=None, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='lead',
            name='company',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='leads', to='crm.company'),
        ),
    ]
