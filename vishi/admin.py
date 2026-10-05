# vishi/admin.py

from django.contrib import admin
from django.contrib import messages                                          # ← add
from .models import Vishi, VishiParticipant, VishiDrawRecord, CollectionLedger, PaymentEntry
from .services import perform_draw, perform_release, perform_skip           # ← add


# ── Actions ───────────────────────────────────────────────────────────────────

@admin.action(description='🎲 Force Draw (bypass date check)')
def force_draw(modeladmin, request, queryset):
    for vishi in queryset:
        if vishi.status != 'active':
            modeladmin.message_user(request, f'"{vishi.name}" is not active — skipped.', level=messages.WARNING)
            continue
        record, error = perform_draw(vishi)
        if error:
            modeladmin.message_user(request, f'"{vishi.name}": {error}', level=messages.ERROR)
        else:
            modeladmin.message_user(request, f'"{vishi.name}" — Cycle {record.cycle_number} drawn → {record.participant}', level=messages.SUCCESS)


@admin.action(description='💸 Force Release (bypass date check)')
def force_release(modeladmin, request, queryset):
    for vishi in queryset:
        if vishi.status not in ('active', 'completed'):
            modeladmin.message_user(request, f'"{vishi.name}" must be active or completed — skipped.', level=messages.WARNING)
            continue
        record, error = perform_release(vishi)
        if error:
            modeladmin.message_user(request, f'"{vishi.name}": {error}', level=messages.ERROR)
        else:
            modeladmin.message_user(request, f'"{vishi.name}" — Cycle {record.cycle_number} released ₹{record.released_amount}', level=messages.SUCCESS)


@admin.action(description='⏭️ Force Skip Cycle (bypass date check)')
def force_skip(modeladmin, request, queryset):
    for vishi in queryset:
        if vishi.status != 'active':
            modeladmin.message_user(request, f'"{vishi.name}" is not active — skipped.', level=messages.WARNING)
            continue
        perform_skip(vishi, is_auto=False, reason='Admin force-skip (testing)')
        modeladmin.message_user(request, f'"{vishi.name}" — cycle skipped, dates advanced.', level=messages.SUCCESS)


@admin.action(description='🧾 Sync Due Collections')
def force_charge(modeladmin, request, queryset):
    from .services import charge_vishi
    for vishi in queryset:
        charge_vishi(vishi)
        modeladmin.message_user(
            request,
            f'"{vishi.name}" — due collections synced. Next renewal: {vishi.current_collection_date}',
            level=messages.SUCCESS,
        )


# ── ModelAdmin registrations ──────────────────────────────────────────────────

@admin.register(Vishi)
class VishiAdmin(admin.ModelAdmin):
    list_display  = ['name', 'amount', 'frequency', 'status', 'current_cycle', 'total_cycles', 'is_deleted']
    list_filter   = ['status', 'frequency', 'is_deleted']
    search_fields = ['name']
    actions       = [force_draw, force_release, force_skip, force_charge]
 
    def get_queryset(self, request):
        # Override to include soft-deleted vishis in Django admin
        return Vishi.all_objects.all()


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
