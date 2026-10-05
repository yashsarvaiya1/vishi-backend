# vishi/services.py

import random
from datetime import date, timedelta
from decimal import Decimal
from dateutil.relativedelta import relativedelta
from django.utils import timezone
from django.db import transaction

from .models import (
    Vishi, VishiParticipant, VishiDrawRecord,
    CollectionLedger, PaymentEntry, SkipRecord
)


FREQUENCY_DELTA = {
    'weekly':       relativedelta(weeks=1),
    'half_monthly': relativedelta(weeks=2),
    'monthly':      relativedelta(months=1),
    'halfyear':     relativedelta(months=6),
    'yearly':       relativedelta(years=1),
}

MONTH_BASED = ('monthly', 'halfyear', 'yearly')


def compute_dates(start_date, draw_day, collection_day, release_day, frequency):
    if frequency in MONTH_BASED:
        return {
            'current_draw_date':       start_date.replace(day=draw_day),
            'current_collection_date': start_date,
            'current_release_date':    start_date.replace(day=release_day),
        }
    else:
        return {
            'current_draw_date':       start_date + timedelta(days=draw_day - 1),
            'current_collection_date': start_date,
            'current_release_date':    start_date + timedelta(days=release_day - 1),
        }


def compute_finish_date(start_date, total_cycles, frequency):
    delta = FREQUENCY_DELTA[frequency]
    return start_date + (delta * total_cycles)


def advance_date(current_date, frequency):
    return current_date + FREQUENCY_DELTA[frequency]


def validate_day_constraints(draw_day, collection_day, release_day, frequency):
    if not (draw_day < collection_day < release_day):
        return 'draw_day must be < collection_day < release_day.'

    if frequency == 'weekly' and (release_day - draw_day) > 7:
        return 'For weekly frequency, release_day - draw_day must be ≤ 7.'

    if frequency == 'half_monthly' and (release_day - draw_day) > 14:
        return 'For half_monthly frequency, release_day - draw_day must be ≤ 14.'

    if frequency in MONTH_BASED:
        for label, val in [('draw_day', draw_day), ('collection_day', collection_day), ('release_day', release_day)]:
            if not (1 <= val <= 28):
                return f'{label} must be between 1 and 28 for {frequency} frequency.'

    return None


def collection_date(vishi, cycle_number):
    # Always use the original anchor (31 Jan -> 28 Feb -> 31 Mar).
    return vishi.start_date + FREQUENCY_DELTA[vishi.frequency] * (cycle_number - 1)


def current_collection_cycle(vishi, today=None):
    today = today or timezone.localdate()
    cycle = 0
    while cycle < vishi.total_cycles and collection_date(vishi, cycle + 1) <= today:
        cycle += 1
    return cycle


@transaction.atomic
def charge_vishi(vishi, today=None):
    """Apply due start-date periods once, including renewals missed during downtime."""
    today = today or timezone.localdate()
    locked = Vishi.all_objects.select_for_update().get(pk=vishi.pk)
    if locked.is_deleted or today < locked.start_date or not locked.total_cycles:
        return
    due_cycle = current_collection_cycle(locked, today)
    for cycle in range(locked.collection_cycle + 1, due_cycle + 1):
        for ledger in locked.ledgers.select_for_update().filter(is_active=True):
            # A late entrant owes their joining period, not periods before joining.
            joined = timezone.localdate(ledger.participant.joined_at)
            if joined >= collection_date(locked, cycle + 1):
                continue
            if ledger.entries.filter(entry_type='charge', cycle_number=cycle).exists():
                continue
            charge_participant(ledger, cycle, None)
    locked.collection_cycle = max(locked.collection_cycle, due_cycle)
    locked.current_collection_date = collection_date(locked, locked.collection_cycle + 1)
    if locked.status == 'upcoming':
        locked.status = 'active'
    locked.save(update_fields=['collection_cycle', 'current_collection_date', 'status'])
    vishi.collection_cycle = locked.collection_cycle
    vishi.current_collection_date = locked.current_collection_date
    vishi.status = locked.status


def sync_collections(user=None, vishi_id=None):
    today = timezone.localdate()
    vishis = Vishi.objects.filter(current_collection_date__lte=today)
    if vishi_id is not None:
        vishis = vishis.filter(pk=vishi_id)
    if user is not None and not user.is_superuser:
        vishis = vishis.filter(participants__user=user, participants__is_active=True).distinct()
    for vishi in vishis:
        if vishi.collection_cycle < vishi.total_cycles:
            charge_vishi(vishi, today)


@transaction.atomic
def perform_draw(vishi, hide_fixed=False):
    charge_vishi(vishi)
    pool = VishiParticipant.objects.filter(vishi=vishi, is_active=True, is_drawn=False)
    if not pool.exists():
        return None, 'No remaining participants.'

    fix = vishi.fix_draw_participant
    if fix and pool.filter(pk=fix.pk).exists():
        drawn     = fix
        was_fixed = True
    else:
        drawn     = random.choice(list(pool))
        was_fixed = False

    drawn.is_drawn = True
    drawn.save()

    record = VishiDrawRecord.objects.create(
        vishi        = vishi,
        participant  = drawn,
        cycle_number = vishi.current_cycle + 1,
        was_fixed    = was_fixed,
        hide_fixed   = was_fixed and hide_fixed,
        drawn_at     = date.today(),
    )

    vishi.current_cycle       += 1
    vishi.fix_draw_participant = None
    vishi.current_draw_date    = advance_date(vishi.current_draw_date, vishi.frequency)

    if vishi.current_cycle >= vishi.total_cycles:
        vishi.status = 'completed'

    vishi.save()

    return record, None


def perform_release(vishi):
    try:
        record = VishiDrawRecord.objects.get(vishi=vishi, cycle_number=vishi.current_cycle)
    except VishiDrawRecord.DoesNotExist:
        return None, 'No draw record for current cycle.'

    active_count           = vishi.participants.filter(is_active=True).count()
    record.is_released     = True
    record.released_at     = date.today()
    record.released_amount = vishi.amount * active_count
    record.save()

    vishi.current_release_date = advance_date(vishi.current_release_date, vishi.frequency)
    vishi.save()
    return record, None


def perform_skip(vishi, is_auto=False, reason=''):
    vishi.current_draw_date       = advance_date(vishi.current_draw_date,       vishi.frequency)
    vishi.current_release_date    = advance_date(vishi.current_release_date,    vishi.frequency)
    vishi.finish_date             = advance_date(vishi.finish_date,             vishi.frequency)
    vishi.missed_cycles          += 1
    vishi.save()

    SkipRecord.objects.create(vishi=vishi, is_auto=is_auto, reason=reason)


@transaction.atomic
def record_payment(ledger, amount, note, recorded_by):
    charge_vishi(ledger.vishi)
    ledger = CollectionLedger.objects.select_for_update().select_related('vishi').get(pk=ledger.pk)
    ledger.balance      += Decimal(str(amount))
    ledger.last_paid_at  = timezone.now()
    ledger.update_status()
    ledger.save()

    PaymentEntry.objects.create(
        ledger       = ledger,
        amount       = Decimal(str(amount)),
        entry_type   = 'payment',
        cycle_number = current_collection_cycle(ledger.vishi),
        note         = note,
        recorded_by  = recorded_by,
    )
    return ledger

# Add these two functions to the bottom of vishi/services.py
# (after record_payment)


@transaction.atomic
def charge_participant(ledger, cycle_number, recorded_by):
    """
    Manually charge a single participant's ledger for a specific cycle.
    Used when a participant was added late or missed the auto-charge.
    Idempotent guard: raises ValueError if already charged for this cycle.
    """
    ledger = CollectionLedger.objects.select_for_update().get(pk=ledger.pk)
    already = PaymentEntry.objects.filter(
        ledger=ledger, entry_type='charge', cycle_number=cycle_number
    ).exists()
    if already:
        raise ValueError(f'Participant already charged for cycle {cycle_number}.')

    vishi = ledger.vishi
    ledger.balance        -= vishi.amount
    ledger.last_charged_at = timezone.now()
    ledger.update_status()
    ledger.save()

    PaymentEntry.objects.create(
        ledger       = ledger,
        amount       = -vishi.amount,
        entry_type   = 'charge',
        cycle_number = cycle_number,
        note         = f'Manual charge for cycle {cycle_number}' if recorded_by else '',
        recorded_by  = recorded_by,
    )
    return ledger


@transaction.atomic
def waive_participant(ledger, cycle_number, note, recorded_by):
    """
    Waive a participant's charge for a specific cycle by crediting them
    the vishi amount. Creates a 'payment' entry marked as waiver.
    Idempotent guard: raises ValueError if already waived for this cycle.
    """
    ledger = CollectionLedger.objects.select_for_update().get(pk=ledger.pk)
    already = PaymentEntry.objects.filter(
        ledger=ledger, entry_type='payment',
        cycle_number=cycle_number, note__startswith='Waived'
    ).exists()
    if already:
        raise ValueError(f'Participant already waived for cycle {cycle_number}.')

    vishi = ledger.vishi
    ledger.balance     += vishi.amount
    ledger.last_paid_at = timezone.now()
    ledger.update_status()
    ledger.save()

    PaymentEntry.objects.create(
        ledger       = ledger,
        amount       = vishi.amount,
        entry_type   = 'payment',
        cycle_number = cycle_number,
        note         = f'Waived cycle {cycle_number}' + (f': {note}' if note else ''),
        recorded_by  = recorded_by,
    )
    return ledger
