# vishi/services.py

import random
from collections import deque
from calendar import monthrange
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
        def next_occurrence(day, earliest):
            month = start_date.replace(day=1)
            while True:
                candidate = month.replace(day=min(day, monthrange(month.year, month.month)[1]))
                if candidate >= earliest:
                    return candidate
                month += relativedelta(months=1)
        draw_date = next_occurrence(draw_day, start_date)
        deadline = next_occurrence(collection_day, start_date)
        release_date = next_occurrence(release_day, max(draw_date, deadline) + timedelta(days=1))
        return {
            'current_draw_date':       draw_date,
            'current_collection_date': deadline,
            'current_release_date':    release_date,
            'next_renewal_date':       start_date,
        }
    else:
        return {
            'current_draw_date':       start_date + timedelta(days=draw_day - 1),
            'current_collection_date': collection_deadline(start_date, collection_day, frequency),
            'current_release_date':    start_date + timedelta(days=release_day - 1),
            'next_renewal_date':       start_date,
        }


def compute_finish_date(start_date, total_cycles, frequency):
    delta = FREQUENCY_DELTA[frequency]
    return start_date + (delta * total_cycles)


def advance_date(current_date, frequency):
    return current_date + FREQUENCY_DELTA[frequency]


def validate_day_constraints(draw_day, collection_day, release_day, frequency):
    maximum = {'weekly': 7, 'half_monthly': 14}.get(frequency, 31)
    for label, val in [('draw_day', draw_day), ('collection_day', collection_day), ('release_day', release_day)]:
        if not (1 <= val <= maximum):
            return f'{label} must be between 1 and {maximum} for {frequency} frequency.'
    if frequency not in MONTH_BASED and draw_day >= release_day:
        return 'Draw day must be before release day.'
    if frequency not in MONTH_BASED and collection_day >= release_day:
        return 'Collection day must be before release day.'

    return None


def validate_date_schedule(start_date, draw_date, collection_date, release_date, frequency):
    renewal = start_date + FREQUENCY_DELTA[frequency]
    for label, value in [('Draw', draw_date), ('Collection', collection_date), ('Release', release_date)]:
        if value < start_date or value >= renewal:
            return f'{label} date must be on or after the start date and before the next renewal ({renewal:%d/%m/%y}).'
    if release_date <= draw_date or release_date <= collection_date:
        return 'Release date must be after both draw and collection dates.'
    return None


def advance_event_date(vishi, field, current_date):
    """Advance from the original date to prevent Jan31 -> Feb28 -> Mar28 drift."""
    anchor = getattr(vishi, field)
    if anchor is None:
        return advance_date(current_date, vishi.frequency)
    delta = FREQUENCY_DELTA[vishi.frequency]
    period = 0
    while anchor + delta * period < current_date:
        period += 1
    if anchor + delta * period != current_date:
        return advance_date(current_date, vishi.frequency)
    return anchor + delta * (period + 1)


def collection_deadline(period_start, collection_day, frequency):
    if frequency in MONTH_BASED:
        # Tolerate invalid legacy edits when reading/migrating existing data.
        # New schedules are validated strictly before saving.
        day = max(1, min(collection_day, monthrange(period_start.year, period_start.month)[1]))
        deadline = period_start.replace(day=day)
        return deadline
    return period_start + timedelta(days=collection_day - 1)


def period_deadline(vishi, cycle_number):
    if vishi.collection_date is not None:
        return vishi.collection_date + FREQUENCY_DELTA[vishi.frequency] * (max(1, cycle_number) - 1)
    return collection_deadline(collection_date(vishi, max(1, cycle_number)), vishi.collection_day, vishi.frequency)


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
def charge_vishi(vishi, today=None, through_cycle=None):
    """Apply due start-date periods once, including renewals missed during downtime."""
    today = today or timezone.localdate()
    locked = Vishi.all_objects.select_for_update().get(pk=vishi.pk)
    if locked.is_deleted or (today < locked.start_date and through_cycle is None) or not locked.total_cycles:
        return
    due_cycle = max(locked.collection_cycle, current_collection_cycle(locked, today))
    if through_cycle is not None:
        if not 1 <= through_cycle <= locked.total_cycles:
            raise ValueError('Invalid collection cycle.')
        due_cycle = max(due_cycle, through_cycle)
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
    locked.next_renewal_date = collection_date(locked, locked.collection_cycle + 1)
    locked.current_collection_date = period_deadline(locked, locked.collection_cycle)
    if locked.status == 'upcoming':
        locked.status = 'active'
    locked.save(update_fields=['collection_cycle', 'current_collection_date', 'next_renewal_date', 'status'])
    vishi.collection_cycle = locked.collection_cycle
    vishi.current_collection_date = locked.current_collection_date
    vishi.next_renewal_date = locked.next_renewal_date
    vishi.status = locked.status


def sync_collections(user=None, vishi_id=None):
    today = timezone.localdate()
    vishis = Vishi.objects.filter(start_date__lte=today)
    if vishi_id is not None:
        vishis = vishis.filter(pk=vishi_id)
    if user is not None and not user.is_superuser:
        vishis = vishis.filter(participants__user=user, participants__is_active=True).distinct()
    for vishi in vishis:
        if vishi.collection_cycle < vishi.total_cycles and (vishi.next_renewal_date is None or vishi.next_renewal_date <= today):
            charge_vishi(vishi, today)


@transaction.atomic
def perform_draw(vishi, hide_fixed=False, force=False):
    Vishi.all_objects.select_for_update().get(pk=vishi.pk)
    vishi.refresh_from_db()
    if vishi.is_deleted or vishi.status not in ('active', 'upcoming'):
        return None, 'Vishi is not active.'
    if not force and (timezone.localdate() < vishi.start_date or timezone.localdate() < vishi.current_draw_date):
        return None, 'Cannot draw before the scheduled draw date.'
    if vishi.current_cycle >= vishi.total_cycles:
        return None, 'No remaining cycles.'
    if vishi.draw_records.filter(is_released=False).exists():
        return None, 'Release the previous draw before drawing the next cycle.'
    pool = VishiParticipant.objects.filter(vishi=vishi, is_active=True, is_drawn=False)
    if not pool.exists():
        return None, 'No remaining participants.'

    charge_vishi(vishi, through_cycle=vishi.current_cycle + 1 if force else None)

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
        drawn_at     = timezone.localdate(),
    )

    vishi.current_cycle       += 1
    vishi.fix_draw_participant = None
    vishi.current_draw_date    = advance_event_date(vishi, 'draw_date', vishi.current_draw_date)

    if vishi.current_cycle >= vishi.total_cycles:
        vishi.status = 'completed'

    vishi.save()

    return record, None


@transaction.atomic
def perform_release(vishi):
    Vishi.all_objects.select_for_update().get(pk=vishi.pk)
    vishi.refresh_from_db()
    try:
        record = VishiDrawRecord.objects.get(vishi=vishi, cycle_number=vishi.current_cycle)
    except VishiDrawRecord.DoesNotExist:
        return None, 'No draw record for current cycle.'
    if record.is_released:
        return None, 'This draw has already been released.'

    active_count           = vishi.participants.filter(is_active=True).count()
    record.is_released     = True
    record.released_at     = timezone.localdate()
    record.released_amount = vishi.amount * active_count
    record.save()

    vishi.current_release_date = advance_event_date(vishi, 'release_date', vishi.current_release_date)
    vishi.save()
    return record, None


def perform_skip(vishi, is_auto=False, reason=''):
    vishi.current_draw_date       = advance_event_date(vishi, 'draw_date', vishi.current_draw_date)
    vishi.current_release_date    = advance_event_date(vishi, 'release_date', vishi.current_release_date)
    vishi.finish_date             = advance_date(vishi.finish_date,             vishi.frequency)
    vishi.missed_cycles          += 1
    vishi.save()

    SkipRecord.objects.create(vishi=vishi, is_auto=is_auto, reason=reason)


@transaction.atomic
def record_payment(ledger, amount, note, recorded_by):
    charge_vishi(ledger.vishi)
    ledger = CollectionLedger.objects.select_for_update().select_related('vishi').get(pk=ledger.pk)
    settlement = payment_settlement(ledger, Decimal(str(amount)))
    ledger.balance      += Decimal(str(amount))
    ledger.last_paid_at  = timezone.now()
    ledger.update_status()
    ledger.save()

    PaymentEntry.objects.create(
        ledger       = ledger,
        amount       = Decimal(str(amount)),
        entry_type   = 'payment',
        cycle_number = max(ledger.vishi.collection_cycle, current_collection_cycle(ledger.vishi)),
        note         = note,
        recorded_by  = recorded_by,
        settlement   = settlement,
    )
    return ledger


def outstanding_payments(ledger):
    """Replay credits FIFO without changing the authoritative ledger balance."""
    outstanding = deque()
    advance = Decimal('0')
    for entry in sorted(ledger.entries.all(), key=lambda entry: (entry.created_at, entry.pk)):
        if entry.amount < 0:
            debt = -entry.amount
            used = min(advance, debt)
            advance -= used
            if debt > used:
                outstanding.append([entry.cycle_number, debt - used])
        else:
            credit = entry.amount
            while credit > 0 and outstanding:
                used = min(credit, outstanding[0][1])
                credit -= used
                outstanding[0][1] -= used
                if outstanding[0][1] == 0:
                    outstanding.popleft()
            advance += credit
    return outstanding


def payment_settlement(ledger, amount, today=None):
    today = today or timezone.localdate()
    debt_remaining = max(Decimal('0'), -ledger.balance)
    remaining = amount
    late = Decimal('0')
    due = Decimal('0')
    for cycle, debt in outstanding_payments(ledger):
        used = min(debt, remaining, debt_remaining)
        if today > period_deadline(ledger.vishi, cycle):
            late += used
        else:
            due += used
        remaining -= used
        debt_remaining -= used
        if remaining <= 0 or debt_remaining <= 0:
            break
    # Legacy adjustments without entry history still settle the actual balance.
    used = min(remaining, debt_remaining)
    if today > ledger.vishi.current_collection_date:
        late += used
    else:
        due += used
    remaining -= used
    return {'late_amount': f'{late:.2f}', 'due_amount': f'{due:.2f}', 'advance_amount': f'{remaining:.2f}'}

# Add these two functions to the bottom of vishi/services.py
# (after record_payment)


@transaction.atomic
def charge_participant(ledger, cycle_number, recorded_by):
    """
    Manually charge a single participant's ledger for a specific cycle.
    Used when a participant was added late or missed the auto-charge.
    Idempotent guard: raises ValueError if already charged for this cycle.
    """
    vishi = Vishi.all_objects.select_for_update().get(pk=ledger.vishi_id)
    ledger = CollectionLedger.objects.select_for_update().get(pk=ledger.pk)
    already = PaymentEntry.objects.filter(
        ledger=ledger, entry_type='charge', cycle_number=cycle_number
    ).exists()
    if already:
        raise ValueError(f'Participant already charged for cycle {cycle_number}.')

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
    vishi = Vishi.all_objects.select_for_update().get(pk=ledger.vishi_id)
    ledger = CollectionLedger.objects.select_for_update().get(pk=ledger.pk)
    already = PaymentEntry.objects.filter(
        ledger=ledger, entry_type='payment',
        cycle_number=cycle_number, note__startswith='Waived'
    ).exists()
    if already:
        raise ValueError(f'Participant already waived for cycle {cycle_number}.')

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
