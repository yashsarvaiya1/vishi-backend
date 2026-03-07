# vishi/serializers.py

from rest_framework import serializers
from .models import Vishi, VishiParticipant, VishiDrawRecord, CollectionLedger, PaymentEntry
from accounts.serializers import UserPublicSerializer


class PaymentEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model            = PaymentEntry
        fields           = '__all__'
        read_only_fields = ['ledger', 'entry_type', 'cycle_number', 'recorded_by', 'created_at']


class CollectionLedgerSerializer(serializers.ModelSerializer):
    entries = PaymentEntrySerializer(many=True, read_only=True)

    class Meta:
        model            = CollectionLedger
        fields           = '__all__'
        read_only_fields = ['vishi', 'participant', 'balance', 'status',
                            'last_charged_at', 'last_paid_at', 'updated_at']


class VishiDrawRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model  = VishiDrawRecord
        fields = '__all__'


class VishiDrawRecordPublicSerializer(serializers.ModelSerializer):
    vishi_name = serializers.CharField(source='participant.vishi_name')
    username   = serializers.CharField(source='participant.user.username')

    class Meta:
        model  = VishiDrawRecord
        fields = ['cycle_number', 'vishi_name', 'username', 'was_fixed',
                  'drawn_at', 'is_released', 'released_at']


class VishiParticipantAdminSerializer(serializers.ModelSerializer):
    user_detail = UserPublicSerializer(source='user', read_only=True)

    class Meta:
        model  = VishiParticipant
        fields = '__all__'


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

    class Meta:
        model            = Vishi
        fields           = '__all__'
        read_only_fields = ['current_draw_date', 'current_collection_date', 'current_release_date',
                            'finish_date', 'total_cycles', 'current_cycle', 'missed_cycles',
                            'status', 'fix_draw_participant', 'created_by', 'created_at', 'updated_at']


class VishiPublicSerializer(serializers.ModelSerializer):
    participants = VishiParticipantPublicSerializer(many=True, read_only=True)
    draw_records = VishiDrawRecordPublicSerializer(many=True, read_only=True)

    class Meta:
        model  = Vishi
        fields = ['id', 'name', 'amount', 'frequency', 'current_draw_date',
                  'current_collection_date', 'current_release_date', 'start_date',
                  'finish_date', 'status', 'current_cycle', 'total_cycles',
                  'participants', 'draw_records']
