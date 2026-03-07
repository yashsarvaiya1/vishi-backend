# vishi/admin.py

from django.contrib import admin
from .models import Vishi, VishiParticipant, VishiDrawRecord, CollectionLedger, PaymentEntry


@admin.register(Vishi)
class VishiAdmin(admin.ModelAdmin):
    list_display  = ['name', 'amount', 'frequency', 'status', 'current_cycle', 'total_cycles']
    list_filter   = ['status', 'frequency']
    search_fields = ['name']


@admin.register(VishiParticipant)
class VishiParticipantAdmin(admin.ModelAdmin):
    list_display  = ['vishi', 'user', 'vishi_name', 'is_drawn', 'is_active']
    list_filter   = ['is_drawn', 'is_active']


@admin.register(VishiDrawRecord)
class VishiDrawRecordAdmin(admin.ModelAdmin):
    list_display  = ['vishi', 'cycle_number', 'participant', 'was_fixed', 'is_released']
    list_filter   = ['is_released', 'was_fixed']


@admin.register(CollectionLedger)
class CollectionLedgerAdmin(admin.ModelAdmin):
    list_display  = ['vishi', 'participant', 'balance', 'status', 'is_active']
    list_filter   = ['status', 'is_active']


@admin.register(PaymentEntry)
class PaymentEntryAdmin(admin.ModelAdmin):
    list_display  = ['ledger', 'amount', 'entry_type', 'cycle_number', 'recorded_by', 'created_at']
    list_filter   = ['entry_type']
