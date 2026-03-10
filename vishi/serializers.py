# vishi/serializers.py

from rest_framework import serializers
from .models import Vishi, VishiParticipant, VishiDrawRecord, CollectionLedger, PaymentEntry, SkipRecord
from accounts.serializers import UserPublicSerializer


class PaymentEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model            = PaymentEntry
        fields           = '__all__'
        read_only_fields = ['ledger', 'entry_type', 'cycle_number', 'recorded_by', 'created_at']


class CollectionLedgerSerializer(serializers.ModelSerializer):
    entries          = PaymentEntrySerializer(many=True, read_only=True)
    participant_name = serializers.CharField(source='participant.vishi_name', read_only=True)
    mobile_number    = serializers.CharField(source='participant.user.mobile_number', read_only=True)

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

    class Meta:
        model  = VishiDrawRecord
        fields = ['cycle_number', 'vishi_name', 'username', 'was_fixed',
                  'drawn_at', 'is_released', 'released_at', 'released_amount']


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
    username     = serializers.CharField(source='user.username', read_only=True)
    drawn_at     = serializers.SerializerMethodField()
    cycle_number = serializers.SerializerMethodField()

    class Meta:
        model  = VishiParticipant
        fields = ['id', 'vishi_name', 'username', 'is_drawn', 'is_active', 'drawn_at', 'cycle_number']

    def get_drawn_at(self, obj):
        record = obj.draw_records.first()
        return record.drawn_at if record else None

    def get_cycle_number(self, obj):
        record = obj.draw_records.first()
        return record.cycle_number if record else None


class VishiSerializer(serializers.ModelSerializer):
    participants           = VishiParticipantAdminSerializer(many=True, read_only=True)
    draw_records           = VishiDrawRecordSerializer(many=True, read_only=True)
    pending_payments_count = serializers.SerializerMethodField()

    class Meta:
        model            = Vishi
        fields           = '__all__'
        read_only_fields = ['current_draw_date', 'current_collection_date', 'current_release_date',
                            'finish_date', 'total_cycles', 'current_cycle', 'missed_cycles',
                            'status', 'fix_draw_participant', 'created_by', 'created_at',
                            'updated_at', 'is_deleted', 'deleted_at']

    def get_pending_payments_count(self, obj):
        return obj.ledgers.filter(is_active=True, status='due').count()

    def validate(self, data):
        # FIXED: wrap error string in list so DRF returns {"non_field_errors": [...]}
        if self.instance and self.instance.status == 'active':
            locked = ['amount', 'frequency', 'draw_day', 'collection_day', 'release_day', 'start_date']
            for field in locked:
                if field in data:
                    raise serializers.ValidationError(
                        {field: f'"{field}" cannot be changed on an active vishi.'}
                    )
        return data


class VishiPublicSerializer(serializers.ModelSerializer):
    participants = VishiParticipantPublicSerializer(many=True, read_only=True)
    draw_records = VishiDrawRecordPublicSerializer(many=True, read_only=True)

    class Meta:
        model  = Vishi
        fields = ['id', 'name', 'amount', 'frequency', 'current_draw_date',
                  'current_collection_date', 'current_release_date', 'start_date',
                  'finish_date', 'status', 'current_cycle', 'total_cycles',
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
    ledgers           = CollectionLedgerSerializer(many=True)


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
