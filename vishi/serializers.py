# vishi/serializers.py

from rest_framework import serializers
from decimal import Decimal
from .models import Vishi, VishiParticipant, VishiDrawRecord, CollectionLedger, PaymentEntry, SkipRecord
from accounts.serializers import UserPublicSerializer
from .services import validate_day_constraints, compute_dates, payment_settlement, validate_date_schedule, MONTH_BASED


class PaymentEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model            = PaymentEntry
        fields           = '__all__'
        read_only_fields = ['ledger', 'entry_type', 'cycle_number', 'recorded_by', 'created_at', 'settlement']


class RecordPaymentSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'))
    note = serializers.CharField(required=False, allow_blank=True, default='')


class CollectionLedgerSerializer(serializers.ModelSerializer):
    entries          = PaymentEntrySerializer(many=True, read_only=True)
    participant_name = serializers.CharField(source='participant.vishi_name', read_only=True)
    mobile_number    = serializers.CharField(source='participant.user.mobile_number', read_only=True)
    late_amount      = serializers.SerializerMethodField()

    def get_late_amount(self, obj):
        return payment_settlement(obj, max(0, -obj.balance))['late_amount']

    class Meta:
        model            = CollectionLedger
        fields           = '__all__'
        read_only_fields = ['vishi', 'participant', 'balance', 'status',
                            'last_charged_at', 'last_paid_at', 'updated_at']


class VishiDrawRecordSerializer(serializers.ModelSerializer):
    participant_name = serializers.CharField(source='participant.vishi_name', read_only=True)
    username         = serializers.CharField(source='participant.user.username', read_only=True)

    class Meta:
        model  = VishiDrawRecord
        fields = '__all__'


class VishiDrawRecordPublicSerializer(serializers.ModelSerializer):
    vishi_name = serializers.CharField(source='participant.vishi_name')
    username   = serializers.CharField(source='participant.user.username')
    was_fixed  = serializers.SerializerMethodField()

    def get_was_fixed(self, obj):
        return obj.was_fixed and not obj.hide_fixed

    class Meta:
        model  = VishiDrawRecord
        fields = ['cycle_number', 'vishi_name', 'username', 'was_fixed',
                  'drawn_at', 'is_released', 'released_at', 'released_amount']


class DrawOptionsSerializer(serializers.Serializer):
    hide_fixed = serializers.BooleanField(default=False)


class VishiParticipantAdminSerializer(serializers.ModelSerializer):
    user_detail    = UserPublicSerializer(source='user', read_only=True)
    ledger_balance = serializers.SerializerMethodField()
    ledger_status  = serializers.SerializerMethodField()

    class Meta:
        model  = VishiParticipant
        fields = '__all__'
        # FIXED: vishi is set via URL kwargs in perform_create — must be read-only
        # Without this, DRF validates it as required in request body → "This field is required."
        read_only_fields = ['vishi', 'is_drawn', 'joined_at']

    def get_ledger_balance(self, obj):
        ledger = obj.ledger.filter(is_active=True).first()
        return str(ledger.balance) if ledger else None

    def get_ledger_status(self, obj):
        ledger = obj.ledger.filter(is_active=True).first()
        return ledger.status if ledger else None
 
class VishiParticipantPublicSerializer(serializers.ModelSerializer):
    username      = serializers.CharField(source='user.username', read_only=True)
    user_id       = serializers.IntegerField(source='user.id', read_only=True)
    drawn_at      = serializers.SerializerMethodField()
    cycle_number  = serializers.SerializerMethodField()
    ledger_balance = serializers.SerializerMethodField()   # ← ADD
    ledger_status  = serializers.SerializerMethodField()   # ← ADD

    class Meta:
        model  = VishiParticipant
        fields = ['id', 'vishi_name', 'username', 'user_id', 'is_drawn',
                  'is_active', 'drawn_at', 'cycle_number',
                  'ledger_balance', 'ledger_status']        # ← ADD both

    def get_drawn_at(self, obj):
        record = obj.draw_records.first()
        return record.drawn_at if record else None

    def get_cycle_number(self, obj):
        record = obj.draw_records.first()
        return record.cycle_number if record else None

    def get_ledger_balance(self, obj):                     # ← ADD
        ledger = obj.ledger.filter(is_active=True).first()
        return str(ledger.balance) if ledger else None

    def get_ledger_status(self, obj):                      # ← ADD
        ledger = obj.ledger.filter(is_active=True).first()
        return ledger.status if ledger else None


class VishiSerializer(serializers.ModelSerializer):
    draw_date = serializers.DateField(required=False)
    collection_date = serializers.DateField(required=False)
    release_date = serializers.DateField(required=False)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'))
    schedule_locked = serializers.SerializerMethodField()

    def get_schedule_locked(self, obj):
        return bool(obj.is_deleted or obj.status != 'upcoming' or obj.collection_cycle or obj.current_cycle or PaymentEntry.objects.filter(ledger__vishi=obj).exists())
    participants           = VishiParticipantAdminSerializer(many=True, read_only=True)
    draw_records           = VishiDrawRecordSerializer(many=True, read_only=True)
    pending_payments_count = serializers.SerializerMethodField()

    class Meta:
        model            = Vishi
        fields           = '__all__'
        extra_kwargs = {field: {'required': False} for field in ['draw_day', 'collection_day', 'release_day']}
        read_only_fields = ['current_draw_date', 'current_collection_date', 'current_release_date', 'next_renewal_date',
                            'finish_date', 'total_cycles', 'current_cycle', 'collection_cycle', 'missed_cycles',
                            'status', 'fix_draw_participant', 'created_by', 'created_at',
                            'updated_at', 'is_deleted', 'deleted_at']

    def get_pending_payments_count(self, obj):
        return obj.ledgers.filter(is_active=True, status='due').count()

    def validate(self, data):
        date_fields = ['draw_date', 'collection_date', 'release_date']
        day_fields = ['draw_day', 'collection_day', 'release_day']
        schedule = ['frequency', 'start_date', *date_fields, *day_fields]
        locked = ['amount', *schedule]
        changed = [field for field in locked if field in data and
                   (not self.instance or data[field] != getattr(self.instance, field))]
        if self.instance:
            if self.instance.is_deleted:
                raise serializers.ValidationError('Restore the vishi before editing it.')
            has_history = self.instance.collection_cycle or self.instance.current_cycle or PaymentEntry.objects.filter(ledger__vishi=self.instance).exists()
            if self.instance.status != 'upcoming' or has_history:
                for field in changed:
                    raise serializers.ValidationError(
                        {field: f'"{field}" cannot be changed after the vishi has started or has payment history.'}
                    )
        if self.instance and not changed:
            return data
        values = {field: data.get(field, getattr(self.instance, field, None)) for field in schedule}
        explicit = any(field in data for field in date_fields) or any(values[field] for field in date_fields)
        if explicit:
            missing = [field for field in date_fields if values[field] is None]
            if missing:
                raise serializers.ValidationError({field: 'Select all three schedule dates.' for field in missing})
            if any(field in data for field in day_fields):
                raise serializers.ValidationError('Use schedule dates instead of mixing dates and day numbers.')
            dates = [values[field] for field in date_fields]
        else:
            missing = [field for field in day_fields if values[field] is None]
            if missing:
                raise serializers.ValidationError({field: 'Select the draw, collection and release dates.' for field in date_fields})
            error = validate_day_constraints(*(values[field] for field in day_fields), values['frequency'])
            if error:
                raise serializers.ValidationError({'non_field_errors': [error]})
            legacy = compute_dates(values['start_date'], *(values[field] for field in day_fields), values['frequency'])
            dates = [legacy['current_draw_date'], legacy['current_collection_date'], legacy['current_release_date']]
        error = validate_date_schedule(values['start_date'], *dates, values['frequency'])
        if error:
            raise serializers.ValidationError({'non_field_errors': [error]})
        if not self.instance or any(field in changed for field in schedule):
            for field, day_field, value in zip(date_fields, day_fields, dates):
                data[field] = value
                data[day_field] = value.day if values['frequency'] in MONTH_BASED else (value - values['start_date']).days + 1
        return data


class VishiPublicSerializer(serializers.ModelSerializer):
    participants = VishiParticipantPublicSerializer(many=True, read_only=True)
    draw_records = VishiDrawRecordPublicSerializer(many=True, read_only=True)

    class Meta:
        model  = Vishi
        fields = ['id', 'name', 'amount', 'frequency', 'current_draw_date',
                  'current_collection_date', 'current_release_date', 'next_renewal_date', 'start_date',
                  'finish_date', 'status', 'current_cycle', 'collection_cycle', 'total_cycles',
                  'participants', 'draw_records']


# ─── ADDED: M2 ───────────────────────────────────────────────────────────────

class SkipRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model  = SkipRecord
        fields = ['id', 'vishi', 'skipped_at', 'reason', 'is_auto']
        read_only_fields = ['id', 'vishi', 'skipped_at', 'is_auto']


# ─── ADDED: M1 Dashboard serializers ─────────────────────────────────────────

class DashboardActionSerializer(serializers.Serializer):
    vishi_id   = serializers.IntegerField()
    vishi_name = serializers.CharField()
    action     = serializers.CharField()   # 'draw_overdue' | 'release_pending' | 'payments_pending'
    detail     = serializers.CharField()   # human-readable label


class DashboardUpcomingEventSerializer(serializers.Serializer):
    date       = serializers.DateField()
    vishi_id   = serializers.IntegerField()
    vishi_name = serializers.CharField()
    event_type = serializers.CharField()   # 'draw' | 'collection' | 'release'


class DashboardSerializer(serializers.Serializer):
    active_vishis_count   = serializers.IntegerField()
    upcoming_vishis_count = serializers.IntegerField()
    total_members         = serializers.IntegerField()
    total_users           = serializers.IntegerField()
    action_required       = DashboardActionSerializer(many=True)
    upcoming_this_week    = DashboardUpcomingEventSerializer(many=True)


# ─── ADDED: M3 Payments summary serializers ──────────────────────────────────

class PaymentVishiBreakdownSerializer(serializers.Serializer):
    vishi_id          = serializers.IntegerField()
    vishi_name        = serializers.CharField()
    total_due         = serializers.DecimalField(max_digits=12, decimal_places=2)
    due_participants  = serializers.IntegerField()
    ledgers           = serializers.ListField()


class PaymentsSummarySerializer(serializers.Serializer):
    total_outstanding = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_due_count   = serializers.IntegerField()
    by_vishi          = PaymentVishiBreakdownSerializer(many=True)


# ─── ADDED: M4 User participations serializer ────────────────────────────────

class UserParticipationSlotSerializer(serializers.ModelSerializer):
    vishi_id     = serializers.IntegerField(source='vishi.id', read_only=True)
    vishi_name_full = serializers.CharField(source='vishi.name', read_only=True)
    vishi_status = serializers.CharField(source='vishi.status', read_only=True)
    ledger_balance = serializers.SerializerMethodField()
    ledger_status  = serializers.SerializerMethodField()
    draw_record    = serializers.SerializerMethodField()

    class Meta:
        model  = VishiParticipant
        fields = ['id', 'vishi_id', 'vishi_name_full', 'vishi_status',
                  'vishi_name', 'is_active', 'is_drawn', 'joined_at',
                  'ledger_balance', 'ledger_status', 'draw_record']

    def get_ledger_balance(self, obj):
        ledger = obj.ledger.first()
        return str(ledger.balance) if ledger else None

    def get_ledger_status(self, obj):
        ledger = obj.ledger.first()
        return ledger.status if ledger else None

    def get_draw_record(self, obj):
        record = obj.draw_records.first()
        if not record:
            return None
        return {
            'cycle_number':    record.cycle_number,
            'drawn_at':        record.drawn_at,
            'was_fixed':       record.was_fixed,
            'is_released':     record.is_released,
            'released_at':     record.released_at,
            'released_amount': str(record.released_amount) if record.released_amount else None,
        }


# ─── ADDED: M5 My Vishis grouped serializer ──────────────────────────────────

class MyVishiSlotSerializer(serializers.ModelSerializer):
    ledger_balance = serializers.SerializerMethodField()
    ledger_status  = serializers.SerializerMethodField()
    draw_record    = serializers.SerializerMethodField()

    class Meta:
        model  = VishiParticipant
        fields = ['id', 'vishi_name', 'is_active', 'is_drawn', 'joined_at',
                  'ledger_balance', 'ledger_status', 'draw_record']

    def get_ledger_balance(self, obj):
        ledger = obj.ledger.filter(is_active=True).first()
        return str(ledger.balance) if ledger else None

    def get_ledger_status(self, obj):
        ledger = obj.ledger.filter(is_active=True).first()
        return ledger.status if ledger else None

    def get_draw_record(self, obj):
        record = obj.draw_records.first()
        if not record:
            return None
        return {
            'cycle_number':    record.cycle_number,
            'drawn_at':        record.drawn_at,
            'is_released':     record.is_released,
            'released_amount': str(record.released_amount) if record.released_amount else None,
        }


class MyVishiGroupedSerializer(serializers.Serializer):
    vishi_id        = serializers.IntegerField()
    vishi_name      = serializers.CharField()
    amount          = serializers.DecimalField(max_digits=12, decimal_places=2)
    frequency       = serializers.CharField()
    status          = serializers.CharField()
    current_cycle   = serializers.IntegerField()
    total_cycles    = serializers.IntegerField()
    current_draw_date       = serializers.DateField()
    current_collection_date = serializers.DateField()
    current_release_date    = serializers.DateField()
    my_slots        = MyVishiSlotSerializer(many=True)
    total_balance   = serializers.DecimalField(max_digits=12, decimal_places=2)  # sum across all my slots
    has_due         = serializers.BooleanField()


# ─── ADDED: M6 My Payments grouped serializer ────────────────────────────────

class MyPaymentVishiSerializer(serializers.Serializer):
    vishi_id      = serializers.IntegerField()
    vishi_name    = serializers.CharField()
    vishi_status  = serializers.CharField()
    slots         = serializers.ListField()   # list of {slot_name, balance, status, entries}
    total_balance = serializers.DecimalField(max_digits=12, decimal_places=2)
