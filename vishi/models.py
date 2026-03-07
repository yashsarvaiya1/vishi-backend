# vishi/models.py

from django.db import models
from accounts.models import User


class Vishi(models.Model):
    FREQUENCY_CHOICES = [
        ('weekly',       'Weekly'),
        ('half_monthly', 'Half Monthly'),
        ('monthly',      'Monthly'),
        ('halfyear',     'Half Yearly'),
        ('yearly',       'Yearly'),
    ]
    STATUS_CHOICES = [
        ('upcoming',  'Upcoming'),
        ('active',    'Active'),
        ('completed', 'Completed'),
    ]

    name                    = models.CharField(max_length=200)
    amount                  = models.DecimalField(max_digits=12, decimal_places=2)
    frequency               = models.CharField(max_length=20, choices=FREQUENCY_CHOICES)
    draw_day                = models.PositiveSmallIntegerField()
    collection_day          = models.PositiveSmallIntegerField()
    release_day             = models.PositiveSmallIntegerField()
    current_draw_date       = models.DateField()
    current_collection_date = models.DateField()
    current_release_date    = models.DateField()
    start_date              = models.DateField()
    finish_date             = models.DateField()
    status                  = models.CharField(max_length=20, choices=STATUS_CHOICES, default='upcoming')  # FIXED: was 'active'
    current_cycle           = models.PositiveIntegerField(default=0)
    total_cycles            = models.PositiveIntegerField(default=0)
    missed_cycles           = models.PositiveIntegerField(default=0)
    fix_draw_participant    = models.ForeignKey(
        'VishiParticipant', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='fixed_draw_vishi'
    )
    created_by              = models.ForeignKey(User, on_delete=models.PROTECT, related_name='created_vishis')
    created_at              = models.DateTimeField(auto_now_add=True)
    updated_at              = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering            = ['-created_at']
        verbose_name_plural = 'Vishis'


class VishiParticipant(models.Model):
    vishi      = models.ForeignKey(Vishi, on_delete=models.CASCADE, related_name='participants')
    user       = models.ForeignKey(User, on_delete=models.PROTECT, related_name='participations')
    vishi_name = models.CharField(max_length=150, blank=True)
    is_active  = models.BooleanField(default=True)
    is_drawn   = models.BooleanField(default=False)
    joined_at  = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.vishi_name or self.user.username or self.user.mobile_number} — {self.vishi.name}"

    class Meta:
        ordering = ['joined_at']


class VishiDrawRecord(models.Model):
    vishi           = models.ForeignKey(Vishi, on_delete=models.CASCADE, related_name='draw_records')
    participant     = models.ForeignKey(VishiParticipant, on_delete=models.PROTECT, related_name='draw_records')
    cycle_number    = models.PositiveIntegerField()
    was_fixed       = models.BooleanField(default=False)
    drawn_at        = models.DateField()
    is_released     = models.BooleanField(default=False)
    released_at     = models.DateField(null=True, blank=True)
    released_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    def __str__(self):
        return f"{self.vishi.name} — Cycle {self.cycle_number}"

    class Meta:
        unique_together = ('vishi', 'cycle_number')
        ordering        = ['cycle_number']


class CollectionLedger(models.Model):
    STATUS_CHOICES = [
        ('paid',     'Paid'),
        ('due',      'Due'),
        ('overpaid', 'Overpaid'),
    ]

    vishi           = models.ForeignKey(Vishi, on_delete=models.CASCADE, related_name='ledgers')
    participant     = models.ForeignKey(VishiParticipant, on_delete=models.CASCADE, related_name='ledger')
    balance         = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES, default='paid')
    is_active       = models.BooleanField(default=True)
    last_charged_at = models.DateTimeField(null=True, blank=True)
    last_paid_at    = models.DateTimeField(null=True, blank=True)
    updated_at      = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.participant} — balance: {self.balance}"

    class Meta:
        unique_together = ('vishi', 'participant')

    def update_status(self):
        if self.balance > 0:
            self.status = 'overpaid'
        elif self.balance == 0:
            self.status = 'paid'
        else:
            self.status = 'due'


class PaymentEntry(models.Model):
    ENTRY_TYPE_CHOICES = [
        ('charge',  'Charge'),
        ('payment', 'Payment'),
    ]

    ledger       = models.ForeignKey(CollectionLedger, on_delete=models.CASCADE, related_name='entries')
    amount       = models.DecimalField(max_digits=12, decimal_places=2)
    entry_type   = models.CharField(max_length=20, choices=ENTRY_TYPE_CHOICES)
    cycle_number = models.PositiveIntegerField()
    note         = models.TextField(blank=True)
    recorded_by  = models.ForeignKey(
        User, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='payment_entries'
    )
    created_at   = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.entry_type} — {self.amount} — {self.ledger}"

    class Meta:
        ordering = ['created_at']


# NEW: Audit trail for every skipped cycle
class SkipRecord(models.Model):
    vishi      = models.ForeignKey(Vishi, on_delete=models.CASCADE, related_name='skip_records')
    skipped_at = models.DateTimeField(auto_now_add=True)
    reason     = models.TextField(blank=True)
    is_auto    = models.BooleanField(default=False)  # True = cron auto-skip | False = admin manual

    def __str__(self):
        kind = 'Auto' if self.is_auto else 'Manual'
        return f"{kind} skip — {self.vishi.name} — {self.skipped_at.date()}"

    class Meta:
        ordering = ['-skipped_at']
