from datetime import date, datetime, time
from decimal import Decimal
from importlib import import_module
from unittest.mock import patch
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from .models import Vishi, VishiParticipant, CollectionLedger, PaymentEntry
from .services import charge_vishi, record_payment, perform_draw, perform_skip
from .cron import charge_collection


class CollectionScheduleTests(TestCase):
    def setUp(self):
        self.today = date(2026, 10, 5)
        self.clock = patch('django.utils.timezone.localdate', side_effect=self.localdate)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.admin = User.objects.create_superuser('9999999999', 'password')
        self.user = User.objects.create_user('8888888888', 'password')
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def localdate(self, value=None, timezone=None):
        return value.date() if value is not None else self.today

    def make_vishi(self, start=None, frequency='monthly', total=3, status='active'):
        start = start or self.today
        return Vishi.objects.create(
            name='Test Vishi', amount=Decimal('1000'), frequency=frequency,
            draw_day=15, collection_day=20, release_day=22, start_date=start,
            current_draw_date=date(2026, 10, 15), current_collection_date=start,
            current_release_date=date(2026, 10, 22), finish_date=date(2027, 1, 5),
            total_cycles=total, status=status, created_by=self.admin,
        )

    def make_ledger(self, vishi, joined=None):
        participant = VishiParticipant.objects.create(vishi=vishi, user=self.user)
        joined = joined or vishi.start_date
        VishiParticipant.objects.filter(pk=participant.pk).update(
            joined_at=timezone.make_aware(datetime.combine(joined, time.min))
        )
        return CollectionLedger.objects.create(vishi=vishi, participant=participant)

    def test_payment_after_start_before_draw_is_current_period(self):
        vishi = self.make_vishi()
        ledger = self.make_ledger(vishi)
        response = self.client.post(
            f'/api/vishis/{vishi.pk}/ledgers/{ledger.pk}/record-payment/',
            {'amount': '1000'}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        ledger.refresh_from_db()
        self.assertEqual(ledger.balance, 0)
        self.assertEqual(ledger.status, 'paid')
        self.assertEqual(ledger.entries.get(entry_type='payment').cycle_number, 1)
        vishi.refresh_from_db()
        self.assertEqual(vishi.current_cycle, 0)
        self.assertEqual(vishi.collection_cycle, 1)
        self.assertEqual(vishi.current_collection_date, date(2026, 11, 5))

    def test_before_start_payment_becomes_current_payment_on_start(self):
        vishi = self.make_vishi(start=date(2026, 10, 6))
        ledger = self.make_ledger(vishi, joined=self.today)
        ledger = record_payment(ledger, 1000, '', self.admin)
        self.assertEqual(ledger.status, 'overpaid')
        self.today = date(2026, 10, 6)
        charge_vishi(vishi)
        ledger.refresh_from_db()
        self.assertEqual(ledger.balance, 0)
        self.assertEqual(ledger.status, 'paid')

    def test_renewal_and_repeated_cron_are_idempotent(self):
        vishi = self.make_vishi()
        ledger = self.make_ledger(vishi)
        charge_vishi(vishi)
        charge_collection()
        self.today = date(2026, 11, 5)
        charge_collection()
        charge_collection()
        ledger.refresh_from_db()
        self.assertEqual(ledger.balance, -2000)
        self.assertEqual(ledger.entries.filter(entry_type='charge').count(), 2)

    def test_draw_does_not_charge_again(self):
        vishi = self.make_vishi()
        ledger = self.make_ledger(vishi)
        record_payment(ledger, 1000, '', self.admin)
        self.today = date(2026, 10, 15)
        perform_draw(vishi)
        ledger.refresh_from_db()
        self.assertEqual(ledger.balance, 0)
        self.assertEqual(ledger.entries.filter(entry_type='charge').count(), 1)

    def test_skipping_draw_does_not_shift_payment_renewal(self):
        vishi = self.make_vishi()
        self.make_ledger(vishi)
        charge_vishi(vishi)
        perform_skip(vishi)
        self.assertEqual(vishi.current_collection_date, date(2026, 11, 5))
        self.today = date(2026, 11, 5)
        charge_collection()
        vishi.refresh_from_db()
        self.assertEqual(vishi.collection_cycle, 2)

    def test_downtime_catches_up_and_stops_at_total_cycles(self):
        vishi = self.make_vishi(start=date(2026, 7, 5))
        ledger = self.make_ledger(vishi)
        charge_vishi(vishi)
        charge_vishi(vishi)
        ledger.refresh_from_db()
        self.assertEqual(ledger.balance, -3000)
        self.assertEqual(ledger.entries.count(), 3)

    def test_month_end_anchor_does_not_drift(self):
        self.today = date(2026, 3, 31)
        vishi = self.make_vishi(start=date(2026, 1, 31), total=5)
        self.make_ledger(vishi)
        charge_vishi(vishi)
        self.assertEqual(vishi.collection_cycle, 3)
        self.assertEqual(vishi.current_collection_date, date(2026, 4, 30))

    def test_other_frequencies(self):
        for frequency, renewal in [('weekly', date(2026, 10, 12)),
                                   ('half_monthly', date(2026, 10, 19)),
                                   ('halfyear', date(2027, 4, 5)),
                                   ('yearly', date(2027, 10, 5))]:
            with self.subTest(frequency=frequency):
                vishi = self.make_vishi(frequency=frequency)
                self.make_ledger(vishi)
                charge_vishi(vishi)
                self.assertEqual(vishi.current_collection_date, renewal)

    def test_existing_charge_and_waiver_are_not_reapplied(self):
        vishi = self.make_vishi()
        ledger = self.make_ledger(vishi)
        PaymentEntry.objects.create(ledger=ledger, amount=-1000, entry_type='charge', cycle_number=1)
        PaymentEntry.objects.create(ledger=ledger, amount=1000, entry_type='payment', cycle_number=1, note='Waived cycle 1')
        charge_vishi(vishi)
        ledger.refresh_from_db()
        self.assertEqual(ledger.balance, 0)
        self.assertEqual(ledger.entries.count(), 2)

    def test_deleted_vishi_and_removed_ledger_not_charged(self):
        vishi = self.make_vishi()
        ledger = self.make_ledger(vishi)
        vishi.soft_delete()
        charge_vishi(vishi)
        self.assertEqual(ledger.entries.count(), 0)
        vishi.is_deleted = False
        vishi.save()
        charge_vishi(vishi)
        self.assertEqual(ledger.entries.count(), 0)

    def test_new_late_participant_owes_current_period_only(self):
        vishi = self.make_vishi(start=date(2026, 8, 5))
        self.make_ledger(vishi)
        self.make_ledger(vishi)
        self.make_ledger(vishi)
        response = self.client.post(f'/api/vishis/{vishi.pk}/participants/',
                                    {'user': self.user.pk, 'vishi_name': 'Late'}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        ledger = CollectionLedger.objects.get(participant_id=response.data['id'])
        self.assertEqual(ledger.balance, -1000)
        self.assertEqual(list(ledger.entries.values_list('cycle_number', flat=True)), [3])

    def test_summary_includes_first_period_and_completed_dues(self):
        vishi = self.make_vishi(status='completed')
        self.make_ledger(vishi)
        response = self.client.get('/api/payments-summary/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['total_outstanding'], '1000.00')
        self.assertEqual(response.data['total_due_count'], 1)

    def test_sync_rolls_back_all_writes_on_failure(self):
        vishi = self.make_vishi()
        ledger = self.make_ledger(vishi)
        with patch('vishi.services.PaymentEntry.objects.create', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                charge_vishi(vishi)
        ledger.refresh_from_db()
        vishi.refresh_from_db()
        self.assertEqual(ledger.balance, 0)
        self.assertEqual(vishi.collection_cycle, 0)

    def test_migration_preserves_legacy_history_and_does_not_backfill(self):
        vishi = self.make_vishi(start=date(2026, 8, 5), total=5)
        ledger = self.make_ledger(vishi)
        ledger.balance = -1000
        ledger.save()
        vishi.current_cycle = 1
        vishi.save()
        PaymentEntry.objects.create(ledger=ledger, amount=-1000, entry_type='charge', cycle_number=1)
        migration = import_module('vishi.migrations.0004_vishi_collection_cycle')
        migration.initialise_collection_schedule(apps, SimpleNamespace(connection=connection))
        vishi.refresh_from_db()
        ledger.refresh_from_db()
        self.assertEqual(vishi.collection_cycle, 2)
        self.assertEqual(vishi.current_collection_date, self.today)
        self.assertEqual(ledger.balance, -1000)
        self.assertEqual(ledger.entries.count(), 1)
        charge_vishi(vishi)
        ledger.refresh_from_db()
        self.assertEqual(ledger.balance, -2000)
        self.assertEqual(list(ledger.entries.values_list('cycle_number', flat=True)), [1, 3])

    def test_upcoming_vishi_becomes_due_on_start(self):
        vishi = self.make_vishi(start=date(2026, 10, 6), status='upcoming')
        ledger = self.make_ledger(vishi, joined=self.today)
        charge_collection()
        self.assertEqual(ledger.entries.count(), 0)
        self.today = date(2026, 10, 6)
        charge_collection()
        vishi.refresh_from_db()
        ledger.refresh_from_db()
        self.assertEqual(vishi.status, 'active')
        self.assertEqual(ledger.balance, -1000)

    def test_member_sees_due_before_draw(self):
        vishi = self.make_vishi()
        self.make_ledger(vishi)
        self.client.force_authenticate(self.user)
        response = self.client.get('/api/profile/me/vishis/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data[0]['has_due'])
        self.assertEqual(response.data[0]['total_balance'], '-1000.00')

    def test_partial_payment_keeps_only_remaining_amount_due(self):
        vishi = self.make_vishi()
        ledger = self.make_ledger(vishi)
        ledger = record_payment(ledger, 400, '', self.admin)
        self.assertEqual(ledger.balance, -600)
        self.assertEqual(ledger.status, 'due')
        ledger = record_payment(ledger, 600, '', self.admin)
        self.assertEqual(ledger.balance, 0)
        self.assertEqual(ledger.entries.filter(entry_type='charge').count(), 1)


class CollectionMigrationTests(TransactionTestCase):
    def test_upgrade_from_live_schema_preserves_entries_and_balances(self):
        executor = MigrationExecutor(connection)
        executor.migrate([('vishi', '0003_vishi_deleted_at_vishi_is_deleted')])
        old = executor.loader.project_state([('vishi', '0003_vishi_deleted_at_vishi_is_deleted')]).apps
        user = old.get_model('accounts', 'User').objects.create(mobile_number='7777777777')
        today = timezone.localdate()
        vishi = old.get_model('vishi', 'Vishi').objects.create(
            name='Live Vishi', amount=1000, frequency='monthly', draw_day=15,
            collection_day=20, release_day=22, start_date=today,
            current_draw_date=today, current_collection_date=today,
            current_release_date=today, finish_date=today, total_cycles=3,
            current_cycle=1, status='active', created_by=user,
        )
        participant = old.get_model('vishi', 'VishiParticipant').objects.create(vishi=vishi, user=user)
        ledger = old.get_model('vishi', 'CollectionLedger').objects.create(
            vishi=vishi, participant=participant, balance=-600, status='due'
        )
        Entry = old.get_model('vishi', 'PaymentEntry')
        Entry.objects.create(ledger=ledger, amount=-1000, entry_type='charge', cycle_number=1)
        Entry.objects.create(ledger=ledger, amount=400, entry_type='payment', cycle_number=1)
        old.get_model('vishi', 'VishiDrawRecord').objects.create(
            vishi=vishi, participant=participant, cycle_number=1,
            was_fixed=True, drawn_at=today,
        )
        try:
            executor = MigrationExecutor(connection)
            executor.migrate([('vishi', '0005_vishidrawrecord_hide_fixed')])
            vishi = Vishi.objects.get(pk=vishi.pk)
            ledger = CollectionLedger.objects.get(pk=ledger.pk)
            self.assertEqual(vishi.collection_cycle, 1)
            self.assertTrue(vishi.draw_records.get().was_fixed)
            self.assertFalse(vishi.draw_records.get().hide_fixed)
            self.assertEqual(ledger.balance, -600)
            self.assertEqual(ledger.entries.count(), 2)
            charge_vishi(vishi)
            ledger.refresh_from_db()
            self.assertEqual(ledger.balance, -600)
            self.assertEqual(ledger.entries.count(), 2)
        finally:
            MigrationExecutor(connection).migrate([('vishi', '0005_vishidrawrecord_hide_fixed')])


class FixedDrawVisibilityTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('9999999999', 'password')
        self.user = User.objects.create_user('8888888888', 'password')
        today = timezone.localdate()
        self.vishi = Vishi.objects.create(
            name='Fixed draw', amount=1000, frequency='monthly', draw_day=5,
            collection_day=10, release_day=15, start_date=today,
            current_draw_date=today, current_collection_date=today,
            current_release_date=today, finish_date=today, total_cycles=2,
            status='active', created_by=self.admin,
        )
        self.participant = VishiParticipant.objects.create(vishi=self.vishi, user=self.user)
        CollectionLedger.objects.create(vishi=self.vishi, participant=self.participant)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def draw(self, **options):
        return self.client.post(f'/api/vishis/{self.vishi.pk}/draw/', options, format='json')

    def test_hidden_fixed_draw_keeps_admin_audit_and_hides_member_label(self):
        response = self.draw(fix_participant_id=self.participant.pk, hide_fixed=True)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['was_fixed'])
        self.assertTrue(response.data['hide_fixed'])
        self.assertEqual(response.data['participant'], self.participant.pk)
        self.client.force_authenticate(self.user)
        response = self.client.get(f'/api/vishis/{self.vishi.pk}/')
        self.assertEqual(response.status_code, 200, response.data)
        record = response.data['draw_records'][0]
        self.assertFalse(record['was_fixed'])
        self.assertNotIn('hide_fixed', record)
        self.client.force_authenticate(self.admin)
        response = self.client.get(f'/api/vishis/{self.vishi.pk}/')
        self.assertTrue(response.data['draw_records'][0]['was_fixed'])
        self.assertTrue(response.data['draw_records'][0]['hide_fixed'])

    def test_unchecked_fixed_draw_keeps_existing_member_visibility(self):
        response = self.draw(fix_participant_id=self.participant.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data['hide_fixed'])
        self.client.force_authenticate(self.user)
        response = self.client.get(f'/api/vishis/{self.vishi.pk}/')
        self.assertTrue(response.data['draw_records'][0]['was_fixed'])

    def test_random_draw_ignores_hide_option(self):
        response = self.draw(hide_fixed=True)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data['was_fixed'])
        self.assertFalse(response.data['hide_fixed'])

    def test_saved_fixed_winner_can_also_be_hidden(self):
        self.vishi.fix_draw_participant = self.participant
        self.vishi.save()
        response = self.draw(hide_fixed=True)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['hide_fixed'])
        self.assertEqual(response.data['participant'], self.participant.pk)

    def test_invalid_option_does_not_draw(self):
        response = self.draw(fix_participant_id=self.participant.pk, hide_fixed='invalid')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.vishi.draw_records.exists())
        self.vishi.refresh_from_db()
        self.assertIsNone(self.vishi.fix_draw_participant)

    def test_member_cannot_select_hidden_fixed_winner(self):
        self.client.force_authenticate(self.user)
        response = self.draw(fix_participant_id=self.participant.pk, hide_fixed=True)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.vishi.draw_records.exists())
