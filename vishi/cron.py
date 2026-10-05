# vishi/cron.py

from datetime import date
from .models import Vishi, CollectionLedger
from .services import perform_skip, sync_collections


def charge_collection():
    """Charge periods from the start date, independently of draws."""
    sync_collections()


def auto_skip_missed_draws():
    today = date.today()
    for vishi in Vishi.all_objects.filter(status='active', is_deleted=False):
        if today > vishi.current_release_date:
            has_draw = vishi.draw_records.filter(cycle_number=vishi.current_cycle + 1).exists()
            if not has_draw:
                perform_skip(
                    vishi,
                    is_auto=True,
                    reason=f'Auto-skipped: release date {vishi.current_release_date} passed with no draw.'
                )


def update_vishi_status():
    for vishi in Vishi.all_objects.filter(status='active', is_deleted=False):
        all_drawn    = not vishi.participants.filter(is_active=True, is_drawn=False).exists()
        all_released = not vishi.draw_records.filter(is_released=False).exists()
        if all_drawn and all_released:
            vishi.status = 'completed'
            vishi.save(update_fields=['status'])
