from rest_framework import serializers
from .models import Vishi, VishiParticipant, VishiDrawRecord, CollectionLedger, PaymentEntry
from accounts.serializers import UserPublicSerializer


class PaymentEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model            = PaymentEntry
        fields           = '__all__'
        read_only_fields = ['ledger', 'entry_type', 'cycle_number', 'recorded_by', 'created_at']


class CollectionLedgerSerializer(serializers.ModelSerializer):
    entries          = PaymentEntrySerializer(many=True, read_only=True)
    participant_name = serializers.CharField(source='participant.vishi_name', read_only=True)  # ← ADDED
    mobile_number    = serializers.CharField(source='participant.user.mobile_number', read_only=True)  # ← ADDED

    class Meta:
        model            = CollectionLedger
        fields           = '__all__'
        read_only_fields = ['vishi', 'participant', 'balance', 'status',
                            'last_charged_at', 'last_paid_at', 'updated_at']


class VishiDrawRecordSerializer(serializers.ModelSerializer):
    participant_name = serializers.CharField(source='participant.vishi_name', read_only=True)  # ← ADDED
    username         = serializers.CharField(source='participant.user.username', read_only=True)  # ← ADDED

    class Meta:
        model  = VishiDrawRecord
        fields = '__all__'


class VishiDrawRecordPublicSerializer(serializers.ModelSerializer):
    vishi_name = serializers.CharField(source='participant.vishi_name')
    username   = serializers.CharField(source='participant.user.username')

    class Meta:
        model  = VishiDrawRecord
        fields = ['cycle_number', 'vishi_name', 'username', 'was_fixed',
                  'drawn_at', 'is_released', 'released_at', 'released_amount']  # ← ADDED released_amount


class VishiParticipantAdminSerializer(serializers.ModelSerializer):
    user_detail    = UserPublicSerializer(source='user', read_only=True)
    ledger_balance = serializers.SerializerMethodField()   # ← ADDED: needed for participant card balance
    ledger_status  = serializers.SerializerMethodField()   # ← ADDED: ✅ Paid / ⚠️ Due / Overpaid

    class Meta:
        model  = VishiParticipant
        fields = '__all__'

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
    participants = VishiParticipantAdminSerializer(many=True, read_only=True)
    draw_records = VishiDrawRecordSerializer(many=True, read_only=True)
    pending_payments_count = serializers.SerializerMethodField()   # ← ADDED: for dashboard badge

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
        # ← ADDED: lock structural fields on active vishi — only name editable
        if self.instance and self.instance.status == 'active':
            locked = ['amount', 'frequency', 'draw_day', 'collection_day', 'release_day', 'start_date']
            for field in locked:
                if field in data:
                    raise serializers.ValidationError(
                        f'"{field}" cannot be changed on an active vishi.'
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
