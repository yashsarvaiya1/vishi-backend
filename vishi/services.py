# vishi/services.py

import random
from datetime import date, timedelta
from decimal import Decimal
from dateutil.relativedelta import relativedelta
from django.utils import timezone

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
            'current_collection_date': start_date.replace(day=collection_day),
            'current_release_date':    start_date.replace(day=release_day),
        }
    else:
        return {
            'current_draw_date':       start_date + timedelta(days=draw_day - 1),
            'current_collection_date': start_date + timedelta(days=collection_day - 1),
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


# ← ADDED: extracted so perform_draw and the admin force_charge action share the same logic
def charge_vishi(vishi):
    """
    Charges all active ledgers for a vishi by one cycle amount.
    Advances current_collection_date.
    Called automatically after every draw (and by cron as a no-op safety net).
    """
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
    vishi.save(update_fields=['current_collection_date'])


def perform_draw(vishi):
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
        drawn_at     = date.today(),
    )

    vishi.current_cycle       += 1
    vishi.fix_draw_participant = None
    vishi.current_draw_date    = advance_date(vishi.current_draw_date, vishi.frequency)

    if vishi.current_cycle >= vishi.total_cycles:
        vishi.status = 'completed'

    vishi.save()

    charge_vishi(vishi)  # ← ADDED: charge all participants immediately after draw

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
    vishi.current_collection_date = advance_date(vishi.current_collection_date, vishi.frequency)
    vishi.current_release_date    = advance_date(vishi.current_release_date,    vishi.frequency)
    vishi.finish_date             = advance_date(vishi.finish_date,             vishi.frequency)
    vishi.missed_cycles          += 1
    vishi.save()

    SkipRecord.objects.create(vishi=vishi, is_auto=is_auto, reason=reason)


def record_payment(ledger, amount, note, recorded_by):
    ledger.balance      += Decimal(str(amount))
    ledger.last_paid_at  = timezone.now()
    ledger.update_status()
    ledger.save()

    PaymentEntry.objects.create(
        ledger       = ledger,
        amount       = Decimal(str(amount)),
        entry_type   = 'payment',
        cycle_number = ledger.vishi.current_cycle,
        note         = note,
        recorded_by  = recorded_by,
    )
    return ledger

# Add these two functions to the bottom of vishi/services.py
# (after record_payment)


def charge_participant(ledger, cycle_number, recorded_by):
    """
    Manually charge a single participant's ledger for a specific cycle.
    Used when a participant was added late or missed the auto-charge.
    Idempotent guard: raises ValueError if already charged for this cycle.
    """
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
        note         = f'Manual charge for cycle {cycle_number}',
        recorded_by  = recorded_by,
    )
    return ledger


def waive_participant(ledger, cycle_number, note, recorded_by):
    """
    Waive a participant's charge for a specific cycle by crediting them
    the vishi amount. Creates a 'payment' entry marked as waiver.
    Idempotent guard: raises ValueError if already waived for this cycle.
    """
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
