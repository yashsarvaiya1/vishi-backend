# vishi/cron.py

from datetime import date
from .models import Vishi, CollectionLedger
from .services import advance_date, perform_skip, charge_vishi  # ← charge_vishi replaces inline logic


def charge_collection():
    """
    Safety-net cron: charges vishis where collection_date == today AND
    the draw has already happened this cycle (current_cycle > 0) but
    the ledgers haven't been charged yet for this cycle.

    In normal flow perform_draw() calls charge_vishi() immediately,
    so this cron is a no-op most of the time. It only fires if the draw
    happened late or the server was down on draw day.
    """
    today = date.today()
    for vishi in Vishi.all_objects.filter(status='active', is_deleted=False, current_collection_date=today):
        # ← ADDED guard: skip if ledgers were already charged this cycle
        already_charged = vishi.ledgers.filter(
            is_active=True,
            entries__entry_type='charge',
            entries__cycle_number=vishi.current_cycle,
        ).exists()
        if already_charged:
            continue
        charge_vishi(vishi)


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
