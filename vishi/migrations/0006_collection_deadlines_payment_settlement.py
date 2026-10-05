from calendar import monthrange
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from django.db import migrations, models


def restore_collection_deadlines(apps, schema_editor):
    Vishi = apps.get_model('vishi', 'Vishi')
    alias = schema_editor.connection.alias
    deltas = {
        'weekly': relativedelta(weeks=1),
        'half_monthly': relativedelta(weeks=2),
        'monthly': relativedelta(months=1),
        'halfyear': relativedelta(months=6),
        'yearly': relativedelta(years=1),
    }
    for vishi in Vishi.objects.using(alias).all().iterator():
        delta = deltas[vishi.frequency]
        period_start = vishi.start_date + delta * (max(1, vishi.collection_cycle) - 1)
        if vishi.frequency in ('monthly', 'halfyear', 'yearly'):
            day = max(1, min(vishi.collection_day, monthrange(period_start.year, period_start.month)[1]))
            deadline = period_start.replace(day=day)
        else:
            deadline = period_start + timedelta(days=vishi.collection_day - 1)
        Vishi.objects.using(alias).filter(pk=vishi.pk).update(
            current_collection_date=deadline,
            next_renewal_date=vishi.start_date + delta * vishi.collection_cycle,
        )


class Migration(migrations.Migration):
    dependencies = [('vishi', '0005_vishidrawrecord_hide_fixed')]

    operations = [
        migrations.AddField(
            model_name='vishi', name='next_renewal_date',
            field=models.DateField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='paymententry', name='settlement',
            field=models.JSONField(null=True, blank=True),
        ),
        migrations.RunPython(restore_collection_deadlines, migrations.RunPython.noop),
    ]
