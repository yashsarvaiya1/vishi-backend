# vishi/cron.py

from datetime import date
from django.utils import timezone
from .models import Vishi, CollectionLedger, PaymentEntry
from .services import advance_date, perform_skip


def charge_collection():
    today = date.today()
    for vishi in Vishi.objects.filter(status='active', current_collection_date=today):
        for ledger in CollectionLedger.objects.filter(vishi=vishi, is_active=True):
            ledger.balance        -= vishi.amount
            ledger.last_charged_at = timezone.now()
            ledger.update_status()
            ledger.save()
            PaymentEntry.objects.create(
                ledger       = ledger,
                amount       = -vishi.amount,
                entry_type   = 'charge',
                cycle_number = vishi.current_cycle,
                recorded_by  = None,
            )
        vishi.current_collection_date = advance_date(vishi.current_collection_date, vishi.frequency)
        vishi.save()


def auto_skip_missed_draws():
    today = date.today()
    for vishi in Vishi.objects.filter(status='active'):
        if today > vishi.current_release_date:
            has_draw = vishi.draw_records.filter(cycle_number=vishi.current_cycle + 1).exists()
            if not has_draw:
                # FIXED: is_auto=True so SkipRecord audit trail marks it as system-triggered
                perform_skip(vishi, is_auto=True, reason='Auto-skipped: release date passed with no draw.')


def update_vishi_status():
    for vishi in Vishi.objects.filter(status='active'):
        all_drawn    = not vishi.participants.filter(is_active=True, is_drawn=False).exists()
        all_released = not vishi.draw_records.filter(is_released=False).exists()
        if all_drawn and all_released:
            vishi.status = 'completed'
            vishi.save()
