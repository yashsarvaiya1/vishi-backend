# vishi/views.py

from datetime import date, timedelta
from decimal import Decimal

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from accounts.models import User
from .models import Vishi, VishiParticipant, CollectionLedger, PaymentEntry, SkipRecord
from .serializers import (
    VishiSerializer, VishiPublicSerializer,
    VishiParticipantAdminSerializer, VishiParticipantPublicSerializer,
    CollectionLedgerSerializer, PaymentEntrySerializer,
    VishiDrawRecordSerializer, SkipRecordSerializer,
    DashboardSerializer, PaymentsSummarySerializer,
    UserParticipationSlotSerializer,
    MyVishiGroupedSerializer, MyPaymentVishiSerializer,
)
from .permissions import IsSuperUser, IsAuthenticatedReadOrSuperUserWrite
from .services import (
    compute_dates, compute_finish_date, validate_day_constraints,
    perform_draw, perform_release, perform_skip, record_payment,
)


class VishiViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticatedReadOrSuperUserWrite]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields   = ['status', 'frequency', 'is_deleted']
    search_fields      = ['name']
    ordering_fields    = ['created_at', 'start_date', 'finish_date', 'status']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return Vishi.all_objects.all()
        return Vishi.objects.filter(
            participants__user=user, participants__is_active=True
        ).distinct()

    def get_serializer_class(self):
        if self.request.user.is_superuser:
            return VishiSerializer
        return VishiPublicSerializer

    def get_permissions(self):
        # FIXED: restrict write operations to superuser explicitly
        if self.action in ['create', 'update', 'partial_update', 'destroy',
                           'activate', 'draw', 'release', 'skip_cycle', 'set_fix_draw']:
            return [IsSuperUser()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        start_date     = serializer.validated_data['start_date']
        frequency      = serializer.validated_data['frequency']
        draw_day       = serializer.validated_data['draw_day']
        collection_day = serializer.validated_data['collection_day']
        release_day    = serializer.validated_data['release_day']

        error = validate_day_constraints(draw_day, collection_day, release_day, frequency)
        if error:
            raise ValidationError({'non_field_errors': [error]})

        dates        = compute_dates(start_date, draw_day, collection_day, release_day, frequency)
        vishi_status = 'active' if start_date <= date.today() else 'upcoming'

        serializer.save(
            created_by              = self.request.user,
            current_draw_date       = dates['current_draw_date'],
            current_collection_date = dates['current_collection_date'],
            current_release_date    = dates['current_release_date'],
            finish_date             = start_date,
            total_cycles            = 0,
            status                  = vishi_status,
        )

    def destroy(self, request, *args, **kwargs):
        vishi = self.get_object()
        # Hard delete: upcoming with zero participants
        if vishi.status == 'upcoming' and vishi.total_cycles == 0:
            vishi.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        # Soft delete for all other cases
        vishi.soft_delete()
        return Response({'detail': 'Vishi deleted.'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='activate')
    def activate(self, request, pk=None):
        vishi = self.get_object()
        if vishi.status != 'upcoming':
            raise ValidationError('Only upcoming vishis can be activated.')
        if vishi.total_cycles == 0:
            raise ValidationError('Add at least one participant before activating.')
        vishi.status = 'active'
        vishi.save(update_fields=['status'])
        return Response({'detail': 'Vishi activated.'})

    @action(detail=True, methods=['post'], url_path='draw')
    def draw(self, request, pk=None):
        vishi = self.get_object()
        if vishi.status != 'active':
            raise ValidationError('Vishi is not active.')
        if date.today() < vishi.current_draw_date:
            raise ValidationError(
                f'Draw date is {vishi.current_draw_date}. Cannot draw before that date.'
            )

        fix_participant_id = request.data.get('fix_participant_id')
        if fix_participant_id:
            try:
                fix_p = VishiParticipant.objects.get(
                    pk=fix_participant_id, vishi=vishi, is_active=True, is_drawn=False
                )
                vishi.fix_draw_participant = fix_p
                vishi.save(update_fields=['fix_draw_participant'])
            except VishiParticipant.DoesNotExist:
                return Response(
                    {'detail': 'Invalid fix participant.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

        record, error = perform_draw(vishi)
        if error:
            return Response({'detail': error}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VishiDrawRecordSerializer(record).data)

    @action(detail=True, methods=['post'], url_path='release')
    def release(self, request, pk=None):
        vishi = self.get_object()
        if vishi.status not in ('active', 'completed'):
            raise ValidationError('Vishi must be active or completed to release.')
        if date.today() < vishi.current_release_date:
            raise ValidationError(
                f'Release date is {vishi.current_release_date}. Cannot release before that date.'
            )
        record, error = perform_release(vishi)
        if error:
            return Response({'detail': error}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VishiDrawRecordSerializer(record).data)

    @action(detail=True, methods=['post'], url_path='skip-cycle')
    def skip_cycle(self, request, pk=None):
        vishi = self.get_object()
        if vishi.status != 'active':
            raise ValidationError('Only active vishis can be skipped.')
        reason = request.data.get('reason', '')
        perform_skip(vishi, is_auto=False, reason=reason)
        return Response({'detail': 'Cycle skipped.'})

    @action(detail=True, methods=['post'], url_path='set-fix-draw')
    def set_fix_draw(self, request, pk=None):
        vishi          = self.get_object()
        participant_id = request.data.get('participant_id')
        if not participant_id:
            vishi.fix_draw_participant = None
            vishi.save(update_fields=['fix_draw_participant'])
            return Response({'detail': 'Fix draw cleared.'})
        try:
            participant = VishiParticipant.objects.get(
                pk=participant_id, vishi=vishi, is_active=True, is_drawn=False
            )
        except VishiParticipant.DoesNotExist:
            return Response(
                {'detail': 'Invalid participant.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        vishi.fix_draw_participant = participant
        vishi.save(update_fields=['fix_draw_participant'])
        return Response({'detail': f'Fix draw set to {participant}.'})


class VishiParticipantViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticatedReadOrSuperUserWrite]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields   = ['is_active', 'is_drawn']
    search_fields      = ['vishi_name', 'user__username', 'user__mobile_number']
    ordering_fields    = ['joined_at', 'is_drawn']

    def get_queryset(self):
        user     = self.request.user
        vishi_pk = self.kwargs['vishi_pk']
        if user.is_superuser:
            return VishiParticipant.objects.filter(vishi_id=vishi_pk)
        in_vishi = VishiParticipant.objects.filter(
            vishi_id=vishi_pk, user=user, is_active=True
        ).exists()
        if not in_vishi:
            return VishiParticipant.objects.none()
        return VishiParticipant.objects.filter(vishi_id=vishi_pk, is_active=True)

    def get_serializer_class(self):
        if self.request.user.is_superuser:
            return VishiParticipantAdminSerializer
        return VishiParticipantPublicSerializer

    def perform_create(self, serializer):
        if not self.request.user.is_superuser:
            raise PermissionDenied('Only admin can add participants.')
        vishi_pk = self.kwargs['vishi_pk']
        vishi    = Vishi.all_objects.get(pk=vishi_pk)
        if vishi.status == 'completed':
            raise ValidationError({'non_field_errors': ['Cannot add participants to a completed vishi.']})
        participant = serializer.save(vishi=vishi)
        CollectionLedger.objects.create(vishi=vishi, participant=participant)
        count              = vishi.participants.filter(is_active=True).count()
        vishi.total_cycles = count
        vishi.finish_date  = compute_finish_date(vishi.start_date, count, vishi.frequency)
        vishi.save(update_fields=['total_cycles', 'finish_date'])

    def perform_update(self, serializer):
        if not self.request.user.is_superuser:
            raise PermissionDenied('Only admin can edit participants.')
        serializer.save()

    # FIXED: override destroy() to return 200 with detail instead of DRF default 204
    def destroy(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied('Only admin can remove participants.')
        instance = self.get_object()
        instance.is_active = False
        instance.save(update_fields=['is_active'])

        try:
            ledger           = instance.ledger.get()
            ledger.is_active = False
            ledger.save(update_fields=['is_active'])
        except CollectionLedger.DoesNotExist:
            pass

        vishi = instance.vishi
        if not instance.is_drawn:
            count              = vishi.participants.filter(is_active=True).count()
            vishi.total_cycles = count
            vishi.finish_date  = compute_finish_date(vishi.start_date, count, vishi.frequency)
            vishi.save(update_fields=['total_cycles', 'finish_date'])

        return Response({'detail': 'Participant removed.'}, status=status.HTTP_200_OK)


class CollectionLedgerViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class   = CollectionLedgerSerializer
    permission_classes = [IsAuthenticatedReadOrSuperUserWrite]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields   = ['status', 'is_active']
    search_fields      = ['participant__vishi_name', 'participant__user__mobile_number']
    ordering_fields    = ['balance', 'status', 'last_charged_at', 'last_paid_at']

    def get_queryset(self):
        user     = self.request.user
        vishi_id = self.kwargs['vishi_pk']
        if user.is_superuser:
            return CollectionLedger.objects.filter(vishi_id=vishi_id, is_active=True)
        return CollectionLedger.objects.filter(
            vishi_id=vishi_id, participant__user=user, is_active=True
        )

    @action(detail=True, methods=['post'], url_path='record-payment',
            permission_classes=[IsSuperUser])
    def record_payment_action(self, request, vishi_pk=None, pk=None):
        ledger = self.get_object()
        amount = request.data.get('amount')
        note   = request.data.get('note', '')
        if not amount:
            return Response(
                {'detail': 'Amount is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            amount = Decimal(str(amount))
            if amount <= 0:
                raise ValueError
        except (ValueError, Exception):
            return Response(
                {'detail': 'Amount must be a positive number.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        updated = record_payment(ledger, amount, note, request.user)
        return Response(CollectionLedgerSerializer(updated).data)


class PaymentEntryViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class   = PaymentEntrySerializer
    permission_classes = [IsAuthenticatedReadOrSuperUserWrite]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields   = ['entry_type', 'cycle_number']
    search_fields      = ['entry_type', 'note']
    ordering_fields    = ['created_at', 'amount', 'entry_type']

    def get_queryset(self):
        user      = self.request.user
        ledger_id = self.kwargs['ledger_pk']
        if user.is_superuser:
            return PaymentEntry.objects.filter(ledger_id=ledger_id)
        return PaymentEntry.objects.filter(
            ledger_id=ledger_id, ledger__participant__user=user
        )


# ─── ADDED: M2 — GET /api/vishis/{vishi_pk}/skip-records/ ────────────────────

class SkipRecordViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class   = SkipRecordSerializer
    permission_classes = [IsSuperUser]
    filter_backends    = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields   = ['is_auto']
    ordering_fields    = ['skipped_at']

    def get_queryset(self):
        return SkipRecord.objects.filter(vishi_id=self.kwargs['vishi_pk'])


# ─── ADDED: M1 — GET /api/dashboard/ ─────────────────────────────────────────

class DashboardView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        today     = date.today()
        week_end  = today + timedelta(days=7)

        active_vishis   = Vishi.objects.filter(status='active')
        upcoming_vishis = Vishi.objects.filter(status='upcoming')

        # Stat cards
        active_count   = active_vishis.count()
        upcoming_count = upcoming_vishis.count()
        total_members  = VishiParticipant.objects.filter(is_active=True).values('user').distinct().count()
        total_users    = User.objects.filter(is_active=True, is_superuser=False).count()

        # Action required alerts
        action_required = []
        for vishi in active_vishis:
            # Draw overdue check
            if today >= vishi.current_draw_date:
                has_draw = vishi.draw_records.filter(
                    cycle_number=vishi.current_cycle + 1
                ).exists()
                if not has_draw:
                    overdue_days = (today - vishi.current_draw_date).days
                    action_required.append({
                        'vishi_id':   vishi.id,
                        'vishi_name': vishi.name,
                        'action':     'draw_overdue',
                        'detail':     f'Draw overdue by {overdue_days} day{"s" if overdue_days != 1 else ""}',
                    })

            # Release pending check
            if today >= vishi.current_release_date and vishi.current_cycle > 0:
                last_record = vishi.draw_records.filter(
                    cycle_number=vishi.current_cycle
                ).first()
                if last_record and not last_record.is_released:
                    action_required.append({
                        'vishi_id':   vishi.id,
                        'vishi_name': vishi.name,
                        'action':     'release_pending',
                        'detail':     'Release pending today',
                    })

            # Payments pending check
            due_count = vishi.ledgers.filter(is_active=True, status='due').count()
            if due_count > 0:
                action_required.append({
                    'vishi_id':   vishi.id,
                    'vishi_name': vishi.name,
                    'action':     'payments_pending',
                    'detail':     f'{due_count} payment{"s" if due_count != 1 else ""} pending',
                })

        # Upcoming this week
        upcoming_events = []
        for vishi in active_vishis:
            for event_type, event_date in [
                ('draw',       vishi.current_draw_date),
                ('collection', vishi.current_collection_date),
                ('release',    vishi.current_release_date),
            ]:
                if today <= event_date <= week_end:
                    upcoming_events.append({
                        'date':       event_date,
                        'vishi_id':   vishi.id,
                        'vishi_name': vishi.name,
                        'event_type': event_type,
                    })
        upcoming_events.sort(key=lambda x: x['date'])

        data = {
            'active_vishis_count':   active_count,
            'upcoming_vishis_count': upcoming_count,
            'total_members':         total_members,
            'total_users':           total_users,
            'action_required':       action_required,
            'upcoming_this_week':    upcoming_events,
        }
        return Response(DashboardSerializer(data).data)


# ─── ADDED: M3 — GET /api/payments/summary/ ──────────────────────────────────

class PaymentsSummaryView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        active_vishis     = Vishi.objects.filter(status='active', is_deleted=False)
        total_outstanding = Decimal('0')
        total_due_count   = 0
        by_vishi          = []

        for vishi in active_vishis:
            due_ledgers = CollectionLedger.objects.filter(
                vishi=vishi, is_active=True, status='due'
            ).select_related('participant', 'participant__user')

            vishi_total = sum(abs(l.balance) for l in due_ledgers)
            total_outstanding += vishi_total
            total_due_count   += due_ledgers.count()

            if due_ledgers.exists():
                by_vishi.append({
                    'vishi_id':         vishi.id,
                    'vishi_name':       vishi.name,
                    'total_due':        vishi_total,
                    'due_participants': due_ledgers.count(),
                    'ledgers':          CollectionLedgerSerializer(due_ledgers, many=True).data,
                })

        data = {
            'total_outstanding': total_outstanding,
            'total_due_count':   total_due_count,
            'by_vishi':          by_vishi,
        }
        return Response(PaymentsSummarySerializer(data).data)
