#!/bin/bash
set -e

echo "⏳ Waiting for postgres..."
sleep 5

echo "🔄 Applying migrations..."
python manage.py migrate --noinput

echo "📦 Collecting static files..."
python manage.py collectstatic --noinput

echo "👤 Creating superuser..."
python manage.py shell -c "
from accounts.models import User
import os
mobile   = os.environ.get('SUPERUSER_MOBILE')
password = os.environ.get('SUPERUSER_PASSWORD')
if mobile and not User.objects.filter(mobile_number=mobile).exists():
    User.objects.create_superuser(mobile_number=mobile, password=password)
    print(f'Superuser created: {mobile}')
else:
    print('Superuser already exists.')
"

echo "⏰ Adding cron jobs..."
python manage.py crontab add

echo "🚀 Starting Gunicorn..."
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 120
