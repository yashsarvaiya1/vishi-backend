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


class CollectionTestCase(TestCase):
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


class CollectionScheduleTests(CollectionTestCase):
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
        self.assertEqual(vishi.next_renewal_date, date(2026, 11, 5))

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
        self.assertEqual(vishi.next_renewal_date, date(2026, 11, 5))
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
        self.assertEqual(vishi.next_renewal_date, date(2026, 4, 30))

    def test_other_frequencies(self):
        for frequency, renewal in [('weekly', date(2026, 10, 12)),
                                   ('half_monthly', date(2026, 10, 19)),
                                   ('halfyear', date(2027, 4, 5)),
                                   ('yearly', date(2027, 10, 5))]:
            with self.subTest(frequency=frequency):
                vishi = self.make_vishi(frequency=frequency)
                self.make_ledger(vishi)
                charge_vishi(vishi)
                self.assertEqual(vishi.next_renewal_date, renewal)

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
            executor.migrate([('vishi', '0007_vishi_schedule_dates')])
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
            MigrationExecutor(connection).migrate([('vishi', '0007_vishi_schedule_dates')])


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


class DeadlineAndEditTests(CollectionTestCase):
    def payload(self, **changes):
        data = dict(name='Schedule', amount='1000', frequency='monthly',
                    start_date='2026-11-05', draw_day=15, collection_day=10, release_day=22)
        data.update(changes)
        return data

    def test_create_collection_before_draw_and_separate_dates(self):
        response = self.client.post('/api/vishis/', self.payload(), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['current_collection_date'], '2026-11-10')
        self.assertEqual(response.data['next_renewal_date'], '2026-11-05')
        self.assertEqual(response.data['current_draw_date'], '2026-11-15')

    def test_create_collection_on_draw_allowed(self):
        response = self.client.post('/api/vishis/', self.payload(collection_day=15), format='json')
        self.assertEqual(response.status_code, 201, response.data)

    def test_create_rejects_before_start_and_invalid_days(self):
        for change in [dict(collection_day=2), dict(draw_day=2), dict(release_day=15),
                       dict(collection_day=22), dict(collection_day=0), dict(draw_day=29),
                       dict(amount='0'), dict(amount='-1'), dict(collection_day=10.5),
                       dict(frequency='weekly', draw_day=4, collection_day=3, release_day=8),
                       dict(frequency='half_monthly', draw_day=4, collection_day=3, release_day=15)]:
            with self.subTest(change=change):
                response = self.client.post('/api/vishis/', self.payload(**change), format='json')
                self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(Vishi.objects.exists())

    def test_create_weekly_before_draw_valid_and_start_is_smallest(self):
        response = self.client.post('/api/vishis/', self.payload(frequency='weekly', draw_day=4, collection_day=2, release_day=6), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['current_collection_date'], '2026-11-06')
        self.assertEqual(response.data['current_draw_date'], '2026-11-08')

    def test_future_partial_edit_validates_combined_schedule_and_rolls_back(self):
        vishi = self.make_vishi(start=date(2026, 11, 5), status='upcoming')
        for change in [dict(collection_day=2), dict(draw_day=23), dict(release_day=19),
                       dict(start_date='2026-11-21'), dict(frequency='weekly'), dict(amount='0')]:
            with self.subTest(change=change):
                response = self.client.patch(f'/api/vishis/{vishi.pk}/', change, format='json')
                self.assertEqual(response.status_code, 400, response.data)
        vishi.refresh_from_db()
        self.assertEqual(vishi.collection_day, 20)
        self.assertEqual(vishi.start_date, date(2026, 11, 5))
        self.assertEqual(vishi.amount, 1000)

    def test_future_edit_recalculates_all_dates_and_finish(self):
        vishi = self.make_vishi(start=date(2026, 11, 5), status='upcoming')
        response = self.client.patch(f'/api/vishis/{vishi.pk}/',
                                     dict(start_date='2026-11-08', collection_day=10), format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['current_draw_date'], '2026-11-15')
        self.assertEqual(response.data['current_collection_date'], '2026-11-10')
        self.assertEqual(response.data['current_release_date'], '2026-11-22')
        self.assertEqual(response.data['next_renewal_date'], '2026-11-08')
        self.assertEqual(response.data['finish_date'], '2027-02-08')

    def test_invalid_legacy_schedule_allows_name_but_not_financial_change(self):
        vishi = self.make_vishi(start=date(2026, 11, 5), status='upcoming')
        vishi.draw_day = 25
        vishi.save()
        response = self.client.patch(f'/api/vishis/{vishi.pk}/', {'name': 'Renamed'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        response = self.client.patch(f'/api/vishis/{vishi.pk}/', {'amount': '1500'}, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        # Unchanged legacy settings in a full form must not trigger date.replace errors.
        vishi.draw_day = 35
        vishi.save()
        response = self.client.patch(f'/api/vishis/{vishi.pk}/',
            dict(name='Name only', draw_day=35, collection_day=20, release_day=22,
                 frequency='monthly', start_date='2026-11-05', amount='1000'), format='json')
        self.assertEqual(response.status_code, 200, response.data)

    def test_frequency_edit_recalculates_offset_schedule(self):
        vishi = self.make_vishi(start=date(2026, 11, 5), status='upcoming')
        response = self.client.patch(f'/api/vishis/{vishi.pk}/',
            dict(frequency='weekly', draw_day=4, collection_day=2, release_day=6), format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['current_collection_date'], '2026-11-06')
        self.assertEqual(response.data['current_release_date'], '2026-11-10')
        self.assertEqual(response.data['finish_date'], '2026-11-26')

    def test_active_completed_and_prepaid_schedules_locked_but_name_editable(self):
        for status in ['active', 'completed', 'upcoming']:
            with self.subTest(status=status):
                vishi = self.make_vishi(start=date(2026, 11, 5), status=status)
                if status == 'upcoming':
                    record_payment(self.make_ledger(vishi, joined=self.today), 1000, '', self.admin)
                for change in [dict(amount='2000'), dict(collection_day=10), dict(start_date='2026-11-06')]:
                    response = self.client.patch(f'/api/vishis/{vishi.pk}/', change, format='json')
                    self.assertEqual(response.status_code, 400, response.data)
                response = self.client.patch(f'/api/vishis/{vishi.pk}/', {'name': 'Renamed'}, format='json')
                self.assertEqual(response.status_code, 200, response.data)
                self.assertTrue(response.data['schedule_locked'])

    def test_deleted_edit_and_member_edit_rejected(self):
        vishi = self.make_vishi(start=date(2026, 11, 5), status='upcoming')
        self.make_ledger(vishi)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.patch(f'/api/vishis/{vishi.pk}/', {'name': 'bad'}, format='json').status_code, 403)
        self.client.force_authenticate(self.admin)
        vishi.soft_delete()
        response = self.client.patch(f'/api/vishis/{vishi.pk}/?is_deleted=true', {'name': 'bad'}, format='json')
        self.assertEqual(response.status_code, 400, response.data)

    def test_edit_start_to_today_charges_once(self):
        vishi = self.make_vishi(start=date(2026, 11, 5), status='upcoming')
        ledger = self.make_ledger(vishi, joined=self.today)
        response = self.client.patch(f'/api/vishis/{vishi.pk}/', {'start_date': '2026-10-05'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 'active')
        ledger.refresh_from_db()
        self.assertEqual(ledger.balance, -1000)
        self.assertEqual(ledger.entries.count(), 1)

    def test_deadline_does_not_gate_renewal_and_draw_skip_does_not_move_it(self):
        vishi = self.make_vishi()
        ledger = self.make_ledger(vishi)
        charge_vishi(vishi)
        self.assertEqual(vishi.current_collection_date, date(2026, 10, 20))
        self.assertEqual(vishi.next_renewal_date, date(2026, 11, 5))
        perform_skip(vishi)
        self.assertEqual(vishi.current_collection_date, date(2026, 10, 20))
        self.today = date(2026, 10, 21)
        charge_collection()
        self.assertEqual(ledger.entries.count(), 1)
        self.today = date(2026, 11, 5)
        charge_collection()
        vishi.refresh_from_db()
        self.assertEqual(ledger.entries.count(), 2)
        self.assertEqual(vishi.current_collection_date, date(2026, 11, 20))
        self.assertEqual(vishi.next_renewal_date, date(2026, 12, 5))

    def test_payment_on_deadline_is_on_time_after_deadline_is_late(self):
        for day, expected in [(20, 'due_amount'), (21, 'late_amount')]:
            with self.subTest(day=day):
                vishi = self.make_vishi()
                ledger = self.make_ledger(vishi)
                self.today = date(2026, 10, day)
                record_payment(ledger, 400, '', self.admin)
                entry = ledger.entries.get(entry_type='payment')
                self.assertEqual(entry.settlement[expected], '400.00')
                ledger.refresh_from_db()
                self.assertEqual(ledger.balance, -600)

    def test_collection_before_draw_payment_late_before_draw(self):
        vishi = self.make_vishi()
        vishi.collection_day = 10
        vishi.save()
        ledger = self.make_ledger(vishi)
        self.today = date(2026, 10, 11)
        response = self.client.post(f'/api/vishis/{vishi.pk}/ledgers/{ledger.pk}/record-payment/', {'amount': '1000'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['entries'][-1]['settlement']['late_amount'], '1000.00')
        self.assertEqual(response.data['balance'], '0.00')
        self.assertEqual(response.data['status'], 'paid')
        self.assertEqual(vishi.draw_records.count(), 0)

    def test_renewal_payment_settles_prior_late_then_current_then_advance(self):
        vishi = self.make_vishi()
        ledger = self.make_ledger(vishi)
        self.today = date(2026, 11, 5)
        record_payment(ledger, 2500, '', self.admin)
        entry = ledger.entries.get(entry_type='payment')
        self.assertEqual(entry.settlement, dict(late_amount='1000.00', due_amount='1000.00', advance_amount='500.00'))
        ledger.refresh_from_db()
        self.assertEqual(ledger.balance, 500)
        self.today = date(2026, 12, 5)
        charge_collection()
        ledger.refresh_from_db()
        self.assertEqual(ledger.balance, -500)
        self.assertEqual(ledger.entries.filter(entry_type='charge').count(), 3)

    def test_advance_before_start_and_overpayment_after_start(self):
        vishi = self.make_vishi(start=date(2026, 10, 6))
        ledger = self.make_ledger(vishi, joined=self.today)
        record_payment(ledger, 1000, '', self.admin)
        self.assertEqual(ledger.entries.get(entry_type='payment').settlement['advance_amount'], '1000.00')
        self.today = date(2026, 10, 6)
        charge_vishi(vishi)
        ledger.refresh_from_db()
        self.assertEqual(ledger.balance, 0)
        record_payment(ledger, 200, '', self.admin)
        self.assertEqual(ledger.entries.filter(entry_type='payment').last().settlement['advance_amount'], '200.00')

    def test_prepaid_and_partial_credits_reduce_only_remaining_late_debt(self):
        vishi = self.make_vishi()
        ledger = self.make_ledger(vishi)
        record_payment(ledger, 400, '', self.admin)
        self.today = date(2026, 10, 21)
        response = self.client.get(f'/api/vishis/{vishi.pk}/ledgers/{ledger.pk}/')
        self.assertEqual(response.data['late_amount'], '600.00')
        record_payment(ledger, 900, '', self.admin)
        self.assertEqual(ledger.entries.filter(entry_type='payment').last().settlement,
                         dict(late_amount='600.00', due_amount='0.00', advance_amount='300.00'))

    def test_late_classification_survives_following_renewal_and_member_history(self):
        vishi = self.make_vishi()
        ledger = self.make_ledger(vishi)
        self.today = date(2026, 10, 21)
        record_payment(ledger, 1000, '', self.admin)
        self.today = date(2026, 11, 5)
        charge_collection()
        self.client.force_authenticate(self.user)
        response = self.client.get('/api/profile/me/payments/')
        self.assertEqual(response.status_code, 200, response.data)
        entries = response.data[0]['slots'][0]['entries']
        payment = next(e for e in entries if e['entry_type'] == 'payment')
        self.assertEqual(payment['settlement']['late_amount'], '1000.00')

    def test_invalid_payment_amount_is_rejected_without_credit(self):
        vishi = self.make_vishi()
        ledger = self.make_ledger(vishi)
        for amount in ['NaN', 'Infinity', '-1', '0', '1.001', '10000000000000']:
            response = self.client.post(f'/api/vishis/{vishi.pk}/ledgers/{ledger.pk}/record-payment/', {'amount': amount}, format='json')
            self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ledger.entries.filter(entry_type='payment').exists())

    def test_deadlines_for_each_frequency(self):
        from .services import period_deadline
        for frequency, first, second in [
            ('weekly', date(2026, 10, 7), date(2026, 10, 14)),
            ('half_monthly', date(2026, 10, 7), date(2026, 10, 21)),
            ('monthly', date(2026, 10, 20), date(2026, 11, 20)),
            ('halfyear', date(2026, 10, 20), date(2027, 4, 20)),
            ('yearly', date(2026, 10, 20), date(2027, 10, 20)),
        ]:
            vishi = self.make_vishi(frequency=frequency)
            if frequency in ['weekly', 'half_monthly']:
                vishi.collection_day = 3
            self.assertEqual(period_deadline(vishi, 1), first)
            self.assertEqual(period_deadline(vishi, 2), second)


class DeadlineMigrationTests(TransactionTestCase):
    def test_upgrade_from_deployed_collection_cursor_preserves_all_money_and_history(self):
        previous = [('vishi', '0005_vishidrawrecord_hide_fixed')]
        latest = [('vishi', '0007_vishi_schedule_dates')]
        executor = MigrationExecutor(connection)
        executor.migrate(previous)
        old = executor.loader.project_state(previous).apps
        user = old.get_model('accounts', 'User').objects.create(mobile_number='7777777777')
        vishi = old.get_model('vishi', 'Vishi').objects.create(
            name='Existing', amount=1000, frequency='monthly', start_date=date(2026, 10, 5),
            draw_day=15, collection_day=20, release_day=22, total_cycles=3,
            collection_cycle=1, current_cycle=0, status='active', created_by=user,
            current_draw_date=date(2026, 10, 15), current_collection_date=date(2026, 11, 5),
            current_release_date=date(2026, 10, 22), finish_date=date(2027, 1, 5),
        )
        participant = old.get_model('vishi', 'VishiParticipant').objects.create(vishi=vishi, user=user)
        ledger = old.get_model('vishi', 'CollectionLedger').objects.create(vishi=vishi, participant=participant, balance=-600, status='due')
        Entry = old.get_model('vishi', 'PaymentEntry')
        Entry.objects.create(ledger=ledger, amount=-1000, entry_type='charge', cycle_number=1)
        payment = Entry.objects.create(ledger=ledger, amount=400, entry_type='payment', cycle_number=1)
        original_time = payment.created_at
        try:
            MigrationExecutor(connection).migrate(latest)
            vishi = Vishi.objects.get(pk=vishi.pk)
            ledger = CollectionLedger.objects.get(pk=ledger.pk)
            self.assertEqual(vishi.current_collection_date, date(2026, 10, 20))
            self.assertEqual(vishi.next_renewal_date, date(2026, 11, 5))
            self.assertEqual(vishi.collection_cycle, 1)
            self.assertEqual(ledger.balance, -600)
            self.assertEqual(ledger.entries.count(), 2)
            payment = PaymentEntry.objects.get(pk=payment.pk)
            self.assertIsNone(payment.settlement)
            self.assertEqual(payment.created_at, original_time)
            self.assertEqual(payment.amount, 400)
            charge_vishi(vishi, today=date(2026, 10, 21))
            ledger.refresh_from_db()
            self.assertEqual(ledger.balance, -600)
            self.assertEqual(ledger.entries.count(), 2)
        finally:
            MigrationExecutor(connection).migrate(latest)


class ExplicitScheduleTests(CollectionTestCase):
    def payload(self, **changes):
        values = dict(name='Date schedule', amount='1000', frequency='monthly',
                      start_date='2026-10-10', draw_date='2026-10-20',
                      collection_date='2026-11-01', release_date='2026-11-05')
        values.update(changes)
        return values

    def create(self, **changes):
        return self.client.post('/api/vishis/', self.payload(**changes), format='json')

    def test_cross_month_collection_and_release_inside_cycle(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['current_draw_date'], '2026-10-20')
        self.assertEqual(response.data['current_collection_date'], '2026-11-01')
        self.assertEqual(response.data['current_release_date'], '2026-11-05')
        self.assertEqual(response.data['collection_date'], '2026-11-01')
        self.assertEqual(response.data['collection_day'], 1)

    def test_collection_before_draw_and_collection_on_draw(self):
        for deadline in ['2026-10-15', '2026-10-20']:
            response = self.create(collection_date=deadline)
            self.assertEqual(response.status_code, 201, response.data)

    def test_invalid_dates_and_partial_dates_are_rejected(self):
        for values in [dict(collection_date='2026-10-09'), dict(draw_date='2026-10-09'),
                       dict(release_date='2026-11-10'), dict(collection_date='2026-11-10'),
                       dict(draw_date='2026-11-11'), dict(release_date='2026-10-19'),
                       dict(release_date='2026-11-01'), dict(collection_date=None),
                       dict(draw_date='2026-02-30'), dict(draw_day=20),
                       dict(frequency='weekly'), dict(frequency='half_monthly')]:
            with self.subTest(values=values):
                response = self.create(**values)
                self.assertEqual(response.status_code, 400, response.data)
        payload = self.payload()
        payload.pop('collection_date')
        response = self.client.post('/api/vishis/', payload, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(Vishi.objects.exists())

    def test_all_frequency_windows(self):
        for frequency, draw, collect, release in [
            ('weekly', '2026-10-11', '2026-10-12', '2026-10-16'),
            ('half_monthly', '2026-10-15', '2026-10-11', '2026-10-23'),
            ('halfyear', '2026-12-20', '2027-03-01', '2027-04-09'),
            ('yearly', '2027-01-20', '2027-09-01', '2027-10-09'),
        ]:
            with self.subTest(frequency=frequency):
                response = self.create(frequency=frequency, draw_date=draw, collection_date=collect, release_date=release)
                self.assertEqual(response.status_code, 201, response.data)

    def test_future_edit_updates_explicit_dates_and_validates_merged_values(self):
        response = self.create(start_date='2026-11-10', draw_date='2026-11-20', collection_date='2026-12-01', release_date='2026-12-05')
        self.assertEqual(response.status_code, 201, response.data)
        pk = response.data['id']
        response = self.client.patch(f'/api/vishis/{pk}/', {'collection_date': '2026-11-15'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['current_collection_date'], '2026-11-15')
        response = self.client.patch(f'/api/vishis/{pk}/', {'release_date': '2026-11-19'}, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        response = self.client.patch(f'/api/vishis/{pk}/', {'frequency': 'weekly'}, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        response = self.client.patch(f'/api/vishis/{pk}/', {'collection_day': 10}, format='json')
        self.assertEqual(response.status_code, 400, response.data)

    def test_existing_future_schedule_can_switch_to_dates(self):
        vishi = self.make_vishi(start=date(2026, 11, 5), status='upcoming')
        response = self.client.patch(f'/api/vishis/{vishi.pk}/',
            dict(draw_date='2026-11-15', collection_date='2026-12-01', release_date='2026-12-03'), format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['current_collection_date'], '2026-12-01')
        self.assertEqual(response.data['collection_date'], '2026-12-01')

    def test_started_and_prepaid_explicit_schedules_stay_locked(self):
        response = self.create()
        vishi = Vishi.objects.get(pk=response.data['id'])
        ledger = self.make_ledger(vishi, joined=self.today)
        record_payment(ledger, 1000, '', self.admin)
        response = self.client.patch(f'/api/vishis/{vishi.pk}/', {'collection_date': '2026-10-30'}, format='json')
        self.assertEqual(response.status_code, 400, response.data)

    def test_cross_month_late_payment_and_start_renewal(self):
        response = self.create()
        vishi = Vishi.objects.get(pk=response.data['id'])
        vishi.total_cycles = 3
        vishi.save(update_fields=['total_cycles'])
        ledger = self.make_ledger(vishi, joined=self.today)
        self.today = date(2026, 10, 10)
        charge_collection()
        self.today = date(2026, 11, 1)
        record_payment(ledger, 400, '', self.admin)
        self.assertEqual(ledger.entries.filter(entry_type='payment').last().settlement['due_amount'], '400.00')
        self.today = date(2026, 11, 2)
        record_payment(ledger, 600, '', self.admin)
        self.assertEqual(ledger.entries.filter(entry_type='payment').last().settlement['late_amount'], '600.00')
        self.today = date(2026, 11, 10)
        charge_collection()
        vishi.refresh_from_db()
        ledger.refresh_from_db()
        self.assertEqual(vishi.current_collection_date, date(2026, 12, 1))
        self.assertEqual(vishi.next_renewal_date, date(2026, 12, 10))
        self.assertEqual(ledger.balance, -1000)
        self.assertEqual(ledger.entries.filter(entry_type='charge').count(), 2)

    def test_draw_release_and_skip_advance_frequency_without_charging_twice(self):
        from .services import perform_release
        response = self.create()
        vishi = Vishi.objects.get(pk=response.data['id'])
        vishi.total_cycles = 3
        vishi.save(update_fields=['total_cycles'])
        ledger = self.make_ledger(vishi, joined=self.today)
        self.today = date(2026, 10, 20)
        perform_draw(vishi)
        self.assertEqual(vishi.current_draw_date, date(2026, 11, 20))
        perform_release(vishi)
        self.assertEqual(vishi.current_release_date, date(2026, 12, 5))
        perform_skip(vishi)
        self.assertEqual(vishi.current_draw_date, date(2026, 12, 20))
        self.assertEqual(vishi.current_release_date, date(2027, 1, 5))
        self.assertEqual(vishi.current_collection_date, date(2026, 11, 1))
        self.assertEqual(ledger.entries.filter(entry_type='charge').count(), 1)

    def test_month_end_and_leap_dates_do_not_drift(self):
        from .services import advance_event_date, period_deadline
        response = self.create(start_date='2028-01-01', draw_date='2028-01-29', collection_date='2028-01-30', release_date='2028-01-31')
        self.assertEqual(response.status_code, 201, response.data)
        vishi = Vishi.objects.get(pk=response.data['id'])
        next_draw = advance_event_date(vishi, 'draw_date', vishi.draw_date)
        self.assertEqual(next_draw, date(2028, 2, 29))
        self.assertEqual(advance_event_date(vishi, 'draw_date', next_draw), date(2028, 3, 29))
        next_release = advance_event_date(vishi, 'release_date', vishi.release_date)
        self.assertEqual(next_release, date(2028, 2, 29))
        self.assertEqual(advance_event_date(vishi, 'release_date', next_release), date(2028, 3, 31))
        self.assertEqual(period_deadline(vishi, 2), date(2028, 2, 29))
        self.assertEqual(period_deadline(vishi, 3), date(2028, 3, 30))

    def test_legacy_day_request_wraps_smaller_day_to_next_month(self):
        response = self.client.post('/api/vishis/',
            dict(name='Compatibility', amount='1000', frequency='monthly', start_date='2026-10-10',
                 draw_day=20, collection_day=1, release_day=5), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['collection_date'], '2026-11-01')
        self.assertEqual(response.data['release_date'], '2026-11-05')


class ExplicitScheduleMigrationTests(TransactionTestCase):
    def test_upgrade_keeps_live_dates_balances_and_settlement_history_unchanged(self):
        previous = [('vishi', '0006_collection_deadlines_payment_settlement')]
        latest = [('vishi', '0007_vishi_schedule_dates')]
        executor = MigrationExecutor(connection)
        executor.migrate(previous)
        old = executor.loader.project_state(previous).apps
        user = old.get_model('accounts', 'User').objects.create(mobile_number='7777777777')
        vishi = old.get_model('vishi', 'Vishi').objects.create(
            name='Live dates', amount=1000, frequency='monthly', start_date=date(2026, 10, 5),
            draw_day=15, collection_day=20, release_day=22, total_cycles=3, collection_cycle=1,
            current_cycle=1, missed_cycles=1, status='active', created_by=user,
            current_draw_date=date(2026, 12, 15), current_collection_date=date(2026, 10, 20),
            current_release_date=date(2026, 11, 22), next_renewal_date=date(2026, 11, 5), finish_date=date(2027, 2, 5),
        )
        p = old.get_model('vishi', 'VishiParticipant').objects.create(vishi=vishi, user=user)
        ledger = old.get_model('vishi', 'CollectionLedger').objects.create(vishi=vishi, participant=p, balance=-600, status='due')
        settlement = {'late_amount': '400.00', 'due_amount': '0.00', 'advance_amount': '0.00'}
        entry = old.get_model('vishi', 'PaymentEntry').objects.create(ledger=ledger, amount=400, entry_type='payment', cycle_number=1, settlement=settlement)
        try:
            MigrationExecutor(connection).migrate(latest)
            vishi = Vishi.objects.get(pk=vishi.pk)
            self.assertEqual(vishi.current_draw_date, date(2026, 12, 15))
            self.assertEqual(vishi.current_collection_date, date(2026, 10, 20))
            self.assertEqual(vishi.current_release_date, date(2026, 11, 22))
            self.assertEqual(vishi.next_renewal_date, date(2026, 11, 5))
            self.assertIsNone(vishi.draw_date)
            self.assertIsNone(vishi.collection_date)
            self.assertIsNone(vishi.release_date)
            self.assertEqual(CollectionLedger.objects.get(pk=ledger.pk).balance, -600)
            self.assertEqual(PaymentEntry.objects.get(pk=entry.pk).settlement, settlement)
        finally:
            MigrationExecutor(connection).migrate(latest)


class ForceDrawTests(CollectionTestCase):
    def setup_future(self, total=3):
        vishi = self.make_vishi(start=date(2026, 10, 10), total=total, status='upcoming')
        ledgers = [self.make_ledger(vishi, joined=self.today) for _ in range(total)]
        return vishi, ledgers

    def draw(self, vishi, **options):
        return self.client.post(f'/api/vishis/{vishi.pk}/draw/', options, format='json')

    def release(self, vishi):
        return self.client.post(f'/api/vishis/{vishi.pk}/release/', {}, format='json')

    def test_force_before_start_charges_every_participant_and_allows_unpaid_release(self):
        vishi, ledgers = self.setup_future()
        result = self.draw(vishi, force=True)
        self.assertEqual(result.status_code, 200, result.data)
        vishi.refresh_from_db()
        self.assertEqual((vishi.status, vishi.current_cycle, vishi.collection_cycle), ('active', 1, 1))
        self.assertEqual(vishi.next_renewal_date, date(2026, 11, 10))
        for ledger in ledgers:
            ledger.refresh_from_db()
            self.assertEqual((ledger.balance, ledger.status), (Decimal('-1000'), 'due'))
            self.assertEqual(ledger.entries.get(entry_type='charge').cycle_number, 1)
        result = self.release(vishi)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(Decimal(result.data['released_amount']), 3000)
        for ledger in ledgers:
            ledger.refresh_from_db()
            self.assertEqual(ledger.balance, -1000)

    def test_force_keeps_existing_advance_credit_and_payment_settlement(self):
        vishi, ledgers = self.setup_future()
        record_payment(ledgers[0], 1500, '', self.admin)
        self.assertEqual(self.draw(vishi, force=True).status_code, 200)
        ledgers[0].refresh_from_db()
        self.assertEqual((ledgers[0].balance, ledgers[0].status), (500, 'overpaid'))
        record_payment(ledgers[1], 1000, '', self.admin)
        entry = ledgers[1].entries.get(entry_type='payment')
        self.assertEqual(entry.cycle_number, 1)
        self.assertEqual(entry.settlement, {'late_amount': '0.00', 'due_amount': '1000.00', 'advance_amount': '0.00'})

    def test_force_current_period_does_not_recharge_paid_participant(self):
        vishi = self.make_vishi()
        ledger = self.make_ledger(vishi)
        record_payment(ledger, 1000, '', self.admin)
        self.assertEqual(self.draw(vishi, force=True).status_code, 200)
        ledger.refresh_from_db()
        self.assertEqual(ledger.balance, 0)
        self.assertEqual(ledger.entries.filter(entry_type='charge').count(), 1)

    def test_future_cycles_and_cron_are_charged_once(self):
        vishi, ledgers = self.setup_future()
        for cycle in (1, 2):
            result = self.draw(vishi, force=True)
            self.assertEqual(result.status_code, 200, result.data)
            self.assertEqual(result.data['cycle_number'], cycle)
            self.assertEqual(self.release(vishi).status_code, 200)
        for day in (date(2026, 10, 10), date(2026, 11, 10)):
            self.today = day
            charge_collection()
            charge_collection()
        vishi.refresh_from_db()
        self.assertEqual(vishi.collection_cycle, 2)
        self.assertEqual(vishi.next_renewal_date, date(2026, 12, 10))
        for ledger in ledgers:
            ledger.refresh_from_db()
            self.assertEqual(ledger.balance, -2000)
            self.assertEqual(list(ledger.entries.filter(entry_type='charge').order_by('cycle_number').values_list('cycle_number', flat=True)), [1, 2])
        self.today = date(2026, 12, 10)
        charge_collection()
        for ledger in ledgers:
            ledger.refresh_from_db()
            self.assertEqual(ledger.balance, -3000)
            self.assertEqual(ledger.entries.filter(entry_type='charge').count(), 3)

    def test_pending_release_blocks_another_draw_without_another_charge(self):
        vishi, ledgers = self.setup_future()
        self.assertEqual(self.draw(vishi, force=True).status_code, 200)
        result = self.draw(vishi, force=True)
        self.assertEqual(result.status_code, 400)
        self.assertIn('Release the previous draw', str(result.data))
        self.assertEqual(vishi.draw_records.count(), 1)
        self.assertEqual(ledgers[0].entries.filter(entry_type='charge').count(), 1)

    def test_repeat_release_does_not_advance_schedule_twice(self):
        vishi, _ = self.setup_future()
        self.assertEqual(self.release(vishi).status_code, 400)
        self.assertEqual(self.draw(vishi, force=True).status_code, 200)
        self.assertEqual(self.release(vishi).status_code, 200)
        vishi.refresh_from_db()
        release_date = vishi.current_release_date
        self.assertEqual(self.release(vishi).status_code, 400)
        vishi.refresh_from_db()
        self.assertEqual(vishi.current_release_date, release_date)

    def test_final_forced_draw_can_release_unpaid_then_rejects_more_draws(self):
        vishi, ledgers = self.setup_future(total=1)
        self.assertEqual(self.draw(vishi, force=True).status_code, 200)
        vishi.refresh_from_db()
        self.assertEqual(vishi.status, 'completed')
        self.assertEqual(self.release(vishi).status_code, 200)
        self.assertEqual(self.draw(vishi, force=True).status_code, 400)
        self.assertEqual(ledgers[0].entries.filter(entry_type='charge').count(), 1)

    def test_normal_draw_still_rejects_early_dates_and_invalid_force(self):
        vishi, ledgers = self.setup_future()
        for options in ({}, {'force': False}, {'force': 'invalid'}):
            self.assertEqual(self.draw(vishi, **options).status_code, 400)
        self.assertFalse(vishi.draw_records.exists())
        self.assertFalse(ledgers[0].entries.exists())
        vishi.status = 'active'
        vishi.save()
        self.assertEqual(self.draw(vishi).status_code, 400)

    def test_member_and_deleted_vishi_cannot_force_draw(self):
        vishi, _ = self.setup_future()
        self.client.force_authenticate(self.user)
        self.assertEqual(self.draw(vishi, force=True).status_code, 403)
        self.client.force_authenticate(self.admin)
        vishi.soft_delete()
        self.assertEqual(self.draw(vishi, force=True).status_code, 404)
        self.assertFalse(vishi.draw_records.exists())

    def test_force_draw_retains_fixed_winner_and_visibility_options(self):
        vishi, ledgers = self.setup_future()
        result = self.draw(vishi, force=True, fix_participant_id=ledgers[1].participant_id, hide_fixed=True)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data['participant'], ledgers[1].participant_id)
        self.assertTrue(result.data['was_fixed'])
        self.assertTrue(result.data['hide_fixed'])

    def test_participant_added_before_start_after_force_owes_forced_cycle(self):
        vishi, _ = self.setup_future()
        self.assertEqual(self.draw(vishi, force=True).status_code, 200)
        result = self.client.post(f'/api/vishis/{vishi.pk}/participants/', {'user': self.user.pk, 'vishi_name': 'New slot'}, format='json')
        self.assertEqual(result.status_code, 201, result.data)
        ledger = CollectionLedger.objects.get(participant_id=result.data['id'])
        self.assertEqual(ledger.balance, -1000)
        self.assertEqual(ledger.entries.get(entry_type='charge').cycle_number, 1)


class ForceDrawConcurrencyTests(TransactionTestCase):
    setUp = CollectionTestCase.setUp
    localdate = CollectionTestCase.localdate
    make_vishi = CollectionTestCase.make_vishi
    make_ledger = CollectionTestCase.make_ledger

    def test_simultaneous_force_draws_make_only_one_unreleased_draw(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections
        if connection.vendor != 'postgresql':
            self.skipTest('Requires PostgreSQL row locks.')
        vishi = self.make_vishi(start=date(2026, 10, 10), total=2, status='upcoming')
        ledgers = [self.make_ledger(vishi, joined=self.today) for _ in range(2)]
        barrier = Barrier(2)

        def draw():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(self.admin)
                barrier.wait(timeout=10)
                return client.post(f'/api/vishis/{vishi.pk}/draw/', {'force': True}, format='json').status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: draw(), range(2)))
        self.assertEqual(sorted(results), [200, 400])
        self.assertEqual(vishi.draw_records.count(), 1)
        for ledger in ledgers:
            ledger.refresh_from_db()
            self.assertEqual(ledger.balance, -1000)
            self.assertEqual(ledger.entries.filter(entry_type='charge').count(), 1)
