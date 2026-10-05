from django.db import migrations, models
from django.db.models import Max
from django.utils import timezone
from dateutil.relativedelta import relativedelta


def initialise_collection_schedule(apps, schema_editor):
    Vishi = apps.get_model('vishi', 'Vishi')
    PaymentEntry = apps.get_model('vishi', 'PaymentEntry')
    alias = schema_editor.connection.alias
    today = timezone.localdate()
    deltas = {
        'weekly': relativedelta(weeks=1),
        'half_monthly': relativedelta(weeks=2),
        'monthly': relativedelta(months=1),
        'halfyear': relativedelta(months=6),
        'yearly': relativedelta(years=1),
    }
    for vishi in Vishi.objects.using(alias).all().iterator():
        delta = deltas[vishi.frequency]
        elapsed = 0
        while elapsed < vishi.total_cycles and vishi.start_date + delta * elapsed <= today:
            elapsed += 1
        charges = PaymentEntry.objects.using(alias).filter(
            ledger__vishi_id=vishi.pk, entry_type='charge'
        )
        last_charge = charges.aggregate(last=Max('cycle_number'))['last']
        # Preserve historical balances. Do not backfill older uncharged periods.
        # Existing draw charges (including legacy cycle 0) count as already billed.
        billed = max(vishi.current_cycle, last_charge or (1 if last_charge == 0 else 0), max(0, elapsed - 1))
        billed = min(billed, vishi.total_cycles)
        Vishi.objects.using(alias).filter(pk=vishi.pk).update(
            collection_cycle=billed,
            current_collection_date=vishi.start_date + delta * billed,
        )


class Migration(migrations.Migration):
    dependencies = [('vishi', '0003_vishi_deleted_at_vishi_is_deleted')]

    operations = [
        migrations.AddField(
            model_name='vishi',
            name='collection_cycle',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(initialise_collection_schedule, migrations.RunPython.noop),
    ]
