from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('vishi', '0006_collection_deadlines_payment_settlement')]

    # Legacy rows retain their existing schedules. Explicit anchors are stored
    # when a new schedule is created or an unstarted schedule is edited.
    operations = [
        migrations.AddField(model_name='vishi', name='draw_date', field=models.DateField(null=True, blank=True)),
        migrations.AddField(model_name='vishi', name='collection_date', field=models.DateField(null=True, blank=True)),
        migrations.AddField(model_name='vishi', name='release_date', field=models.DateField(null=True, blank=True)),
    ]
