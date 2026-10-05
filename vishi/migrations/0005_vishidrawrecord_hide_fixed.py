from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('vishi', '0004_vishi_collection_cycle')]

    operations = [
        migrations.AddField(
            model_name='vishidrawrecord',
            name='hide_fixed',
            field=models.BooleanField(default=False),
        ),
    ]
