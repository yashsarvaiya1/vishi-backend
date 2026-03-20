#!/bin/bash
set -e

echo "⏳ Waiting for postgres..."
sleep 5

echo "🔄 Applying migrations..."
python manage.py migrate --noinput

echo "📦 Collecting static files..."
python manage.py collectstatic --noinput

echo "👤 Syncing superuser..."
python manage.py shell -c "
from accounts.models import User
import os
mobile   = os.environ.get('SUPERUSER_MOBILE')
password = os.environ.get('SUPERUSER_PASSWORD')
if not mobile or not password:
    print('SUPERUSER_MOBILE or SUPERUSER_PASSWORD not set — skipping.')
else:
    user, created = User.objects.get_or_create(mobile_number=mobile)
    user.set_password(password)
    user.is_staff = True
    user.is_superuser = True
    user.save()
    print(f'Superuser {\"created\" if created else \"updated\"}: {mobile}')
"

echo "⏰ Adding cron jobs..."
python manage.py crontab add

echo "🕐 Starting cron daemon..."
cron

echo "🚀 Starting Gunicorn..."
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --timeout 120
