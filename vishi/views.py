from datetime import date
from decimal import Decimal

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import Vishi, VishiParticipant, CollectionLedger, PaymentEntry
from .serializers import (
    VishiSerializer, VishiPublicSerializer,
    VishiParticipantAdminSerializer, VishiParticipantPublicSerializer,
    CollectionLedgerSerializer, PaymentEntrySerializer,
    VishiDrawRecordSerializer,
)
from .permissions import IsSuperUser, IsAuthenticatedReadOrSuperUserWrite
from .services import (
    compute_dates, compute_finish_date, validate_day_constraints,
    perform_draw, perform_release, perform_skip, record_payment,
)


class VishiViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticatedReadOrSuperUserWrite]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]  # ← ADDED DjangoFilterBackend
    filterset_fields   = ['status', 'frequency', 'is_deleted']                                # ← ADDED
    search_fields      = ['name']
    ordering_fields    = ['created_at', 'start_date', 'finish_date', 'status']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            # ← FIXED: superuser can see deleted vishis via ?is_deleted=true
            return Vishi.all_objects.all()
        # Regular users only see their active participations (never deleted vishis)
        return Vishi.objects.filter(
            participants__user=user, participants__is_active=True
        ).distinct()

    def get_serializer_class(self):
        if self.request.user.is_superuser:
            return VishiSerializer
        return VishiPublicSerializer

    def perform_create(self, serializer):
        start_date     = serializer.validated_data['start_date']
        frequency      = serializer.validated_data['frequency']
        draw_day       = serializer.validated_data['draw_day']
        collection_day = serializer.validated_data['collection_day']
        release_day    = serializer.validated_data['release_day']

        error = validate_day_constraints(draw_day, collection_day, release_day, frequency)
        if error:
            raise ValidationError(error)

        dates = compute_dates(start_date, draw_day, collection_day, release_day, frequency)
        vishi_status = 'active' if start_date <= date.today() else 'upcoming'  # ← FIXED: auto-activate if start_date is today or past

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
        """Soft delete for all statuses; hard delete only for upcoming with no participants."""
        vishi = self.get_object()
        if vishi.status == 'upcoming' and vishi.total_cycles == 0:
            return super().destroy(request, *args, **kwargs)
        # ← FIXED: soft delete for active/completed/upcoming-with-participants
        vishi.soft_delete()
        return Response({'detail': 'Vishi deleted.'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='activate', permission_classes=[IsSuperUser])
    def activate(self, request, pk=None):
        vishi = self.get_object()
        if vishi.status != 'upcoming':
            raise ValidationError('Only upcoming vishis can be activated.')
        if vishi.total_cycles == 0:
            raise ValidationError('Add at least one participant before activating.')
        vishi.status = 'active'
        vishi.save(update_fields=['status'])
        return Response({'detail': 'Vishi activated.'})

    @action(detail=True, methods=['post'], url_path='draw', permission_classes=[IsSuperUser])  # hyphen preferred but draw is single word — OK
    def draw(self, request, pk=None):
        vishi = self.get_object()
        if vishi.status != 'active':
            raise ValidationError('Vishi is not active.')
        if date.today() < vishi.current_draw_date:
            raise ValidationError(f'Draw date is {vishi.current_draw_date}. Cannot draw before that date.')

        # ← ADDED: optional fix_draw override from request body (for pre-draw fix selection)
        fix_participant_id = request.data.get('fix_participant_id')
        if fix_participant_id:
            try:
                fix_p = VishiParticipant.objects.get(
                    pk=fix_participant_id, vishi=vishi, is_active=True, is_drawn=False
                )
                vishi.fix_draw_participant = fix_p
                vishi.save(update_fields=['fix_draw_participant'])
            except VishiParticipant.DoesNotExist:
                return Response({'detail': 'Invalid fix participant.'}, status=status.HTTP_400_BAD_REQUEST)

        record, error = perform_draw(vishi)
        if error:
            return Response({'detail': error}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VishiDrawRecordSerializer(record).data)

    @action(detail=True, methods=['post'], url_path='release', permission_classes=[IsSuperUser])
    def release(self, request, pk=None):
        vishi = self.get_object()
        if vishi.status not in ('active', 'completed'):
            raise ValidationError('Vishi must be active or completed to release.')
        if date.today() < vishi.current_release_date:
            raise ValidationError(f'Release date is {vishi.current_release_date}. Cannot release before that date.')
        record, error = perform_release(vishi)
        if error:
            return Response({'detail': error}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VishiDrawRecordSerializer(record).data)

    @action(detail=True, methods=['post'], url_path='skip-cycle', permission_classes=[IsSuperUser])  # ← FIXED: hyphen
    def skip_cycle(self, request, pk=None):
        vishi = self.get_object()
        if vishi.status != 'active':
            raise ValidationError('Only active vishis can be skipped.')
        reason = request.data.get('reason', '')
        perform_skip(vishi, is_auto=False, reason=reason)
        return Response({'detail': 'Cycle skipped.'})

    @action(detail=True, methods=['post'], url_path='set-fix-draw', permission_classes=[IsSuperUser])  # ← FIXED: hyphen
    def set_fix_draw(self, request, pk=None):
        vishi          = self.get_object()
        participant_id = request.data.get('participant_id')
        if not participant_id:
            # ← ADDED: allow clearing fix draw
            vishi.fix_draw_participant = None
            vishi.save(update_fields=['fix_draw_participant'])
            return Response({'detail': 'Fix draw cleared.'})
        try:
            participant = VishiParticipant.objects.get(
                pk=participant_id, vishi=vishi, is_active=True, is_drawn=False
            )
        except VishiParticipant.DoesNotExist:
            return Response({'detail': 'Invalid participant.'}, status=status.HTTP_400_BAD_REQUEST)
        vishi.fix_draw_participant = participant
        vishi.save(update_fields=['fix_draw_participant'])
        return Response({'detail': f'Fix draw set to {participant}.'})


class VishiParticipantViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticatedReadOrSuperUserWrite]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]  # ← ADDED
    filterset_fields   = ['is_active', 'is_drawn']                                             # ← ADDED
    search_fields      = ['vishi_name', 'user__username', 'user__mobile_number']
    ordering_fields    = ['joined_at', 'is_drawn']

    def get_queryset(self):
        user     = self.request.user
        vishi_pk = self.kwargs['vishi_pk']
        if user.is_superuser:
            # ← FIXED: admin sees all participants including inactive
            return VishiParticipant.objects.filter(vishi_id=vishi_pk)
        in_vishi = VishiParticipant.objects.filter(
            vishi_id=vishi_pk, user=user, is_active=True
        ).exists()
        if not in_vishi:
            return VishiParticipant.objects.none()
        return VishiParticipant.objects.filter(vishi_id=vishi_pk, is_active=True)  # ← FIXED: users see only active

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
            raise ValidationError('Cannot add participants to a completed vishi.')
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

    def perform_destroy(self, instance):
        if not self.request.user.is_superuser:
            raise PermissionDenied('Only admin can remove participants.')
        instance.is_active = False
        instance.save(update_fields=['is_active'])

        try:
            ledger           = instance.ledger.get()
            ledger.is_active = False
            ledger.save(update_fields=['is_active'])
        except CollectionLedger.DoesNotExist:
            pass

        vishi = instance.vishi
        # ← FIXED: only recalculate if participant was NOT yet drawn
        if not instance.is_drawn:
            count              = vishi.participants.filter(is_active=True).count()
            vishi.total_cycles = count
            vishi.finish_date  = compute_finish_date(vishi.start_date, count, vishi.frequency)
            vishi.save(update_fields=['total_cycles', 'finish_date'])
        # If already drawn → total_cycles and finish_date unchanged per flow §4.9


class CollectionLedgerViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class   = CollectionLedgerSerializer
    permission_classes = [IsAuthenticatedReadOrSuperUserWrite]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]  # ← ADDED DjangoFilterBackend
    filterset_fields   = ['status', 'is_active']                                              # ← ADDED: ?status=due filtering
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

    @action(detail=True, methods=['post'], url_path='record-payment', permission_classes=[IsSuperUser])  # ← FIXED: hyphen
    def record_payment_action(self, request, vishi_pk=None, pk=None):
        ledger = self.get_object()
        amount = request.data.get('amount')
        note   = request.data.get('note', '')
        if not amount:
            return Response({'detail': 'Amount is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            amount = Decimal(str(amount))
            if amount <= 0:
                raise ValueError
        except (ValueError, Exception):
            return Response({'detail': 'Amount must be a positive number.'}, status=status.HTTP_400_BAD_REQUEST)
        updated = record_payment(ledger, amount, note, request.user)
        return Response(CollectionLedgerSerializer(updated).data)


class PaymentEntryViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class   = PaymentEntrySerializer
    permission_classes = [IsAuthenticatedReadOrSuperUserWrite]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]  # ← ADDED
    filterset_fields   = ['entry_type', 'cycle_number']                                       # ← ADDED
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
