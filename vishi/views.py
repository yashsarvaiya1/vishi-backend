# vishi/views.py

from datetime import date
from decimal import Decimal

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import Vishi, VishiParticipant, CollectionLedger, PaymentEntry
from .serializers import (
    VishiSerializer, VishiPublicSerializer,
    VishiParticipantAdminSerializer, VishiParticipantPublicSerializer,
    CollectionLedgerSerializer, PaymentEntrySerializer,
)
from .permissions import IsSuperUser, IsAuthenticatedReadOrSuperUserWrite
from .services import (
    compute_dates, compute_finish_date, validate_day_constraints,
    perform_draw, perform_release, perform_skip, record_payment,
)


class VishiViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticatedReadOrSuperUserWrite]
    filter_backends    = [filters.SearchFilter, filters.OrderingFilter]
    search_fields      = ['name', 'status', 'frequency']
    ordering_fields    = ['created_at', 'start_date', 'finish_date', 'status']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return Vishi.objects.all()
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
        serializer.save(
            created_by              = self.request.user,
            current_draw_date       = dates['current_draw_date'],
            current_collection_date = dates['current_collection_date'],
            current_release_date    = dates['current_release_date'],
            finish_date             = start_date,   # recalculated as participants are added
            total_cycles            = 0,
            status                  = 'upcoming',   # always starts upcoming
        )

    def destroy(self, request, *args, **kwargs):
        vishi = self.get_object()
        if vishi.status != 'upcoming':
            raise ValidationError('Only upcoming vishis can be deleted.')
        return super().destroy(request, *args, **kwargs)

    # NEW: Explicit activate action — upcoming → active
    @action(detail=True, methods=['post'], url_path='activate', permission_classes=[IsSuperUser])
    def activate(self, request, pk=None):
        vishi = self.get_object()
        if vishi.status != 'upcoming':
            raise ValidationError('Only upcoming vishis can be activated.')
        if vishi.total_cycles == 0:
            raise ValidationError('Add at least one participant before activating.')
        vishi.status = 'active'
        vishi.save()
        return Response({'detail': 'Vishi activated.'})

    # FIXED: added today >= current_draw_date guard
    @action(detail=True, methods=['post'], url_path='draw', permission_classes=[IsSuperUser])
    def draw(self, request, pk=None):
        vishi = self.get_object()
        if vishi.status != 'active':
            raise ValidationError('Vishi is not active.')
        if date.today() < vishi.current_draw_date:
            raise ValidationError(
                f'Draw date is {vishi.current_draw_date}. Cannot draw before that date.'
            )
        record, error = perform_draw(vishi)
        if error:
            return Response({'detail': error}, status=status.HTTP_400_BAD_REQUEST)
        from .serializers import VishiDrawRecordSerializer
        return Response(VishiDrawRecordSerializer(record).data)

    # FIXED: added today >= current_release_date guard
    @action(detail=True, methods=['post'], url_path='release', permission_classes=[IsSuperUser])
    def release(self, request, pk=None):
        vishi = self.get_object()
        if date.today() < vishi.current_release_date:
            raise ValidationError(
                f'Release date is {vishi.current_release_date}. Cannot release before that date.'
            )
        record, error = perform_release(vishi)
        if error:
            return Response({'detail': error}, status=status.HTTP_400_BAD_REQUEST)
        from .serializers import VishiDrawRecordSerializer
        return Response(VishiDrawRecordSerializer(record).data)

    # FIXED: now accepts optional reason from request body
    @action(detail=True, methods=['post'], url_path='skip_cycle', permission_classes=[IsSuperUser])
    def skip_cycle(self, request, pk=None):
        vishi  = self.get_object()
        if vishi.status != 'active':
            raise ValidationError('Only active vishis can be skipped.')
        reason = request.data.get('reason', '')
        perform_skip(vishi, is_auto=False, reason=reason)
        return Response({'detail': 'Cycle skipped.'})

    @action(detail=True, methods=['post'], url_path='set_fix_draw', permission_classes=[IsSuperUser])
    def set_fix_draw(self, request, pk=None):
        vishi          = self.get_object()
        participant_id = request.data.get('participant_id')
        try:
            participant = VishiParticipant.objects.get(
                pk=participant_id, vishi=vishi, is_active=True, is_drawn=False
            )
        except VishiParticipant.DoesNotExist:
            return Response({'detail': 'Invalid participant.'}, status=status.HTTP_400_BAD_REQUEST)
        vishi.fix_draw_participant = participant
        vishi.save()
        return Response({'detail': f'Fix draw set to {participant}.'})


class VishiParticipantViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticatedReadOrSuperUserWrite]
    filter_backends    = [filters.SearchFilter, filters.OrderingFilter]
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
        return VishiParticipant.objects.filter(vishi_id=vishi_pk)

    def get_serializer_class(self):
        if self.request.user.is_superuser:
            return VishiParticipantAdminSerializer
        return VishiParticipantPublicSerializer

    def perform_create(self, serializer):
        if not self.request.user.is_superuser:
            raise PermissionDenied('Only admin can add participants.')
        vishi_pk = self.kwargs['vishi_pk']
        vishi    = Vishi.objects.get(pk=vishi_pk)
        if vishi.status == 'completed':
            raise ValidationError('Cannot add participants to a completed vishi.')
        participant        = serializer.save(vishi=vishi)
        CollectionLedger.objects.create(vishi=vishi, participant=participant)
        count              = vishi.participants.filter(is_active=True).count()
        vishi.total_cycles = count
        vishi.finish_date  = compute_finish_date(vishi.start_date, count, vishi.frequency)
        vishi.save()

    def perform_update(self, serializer):
        if not self.request.user.is_superuser:
            raise PermissionDenied('Only admin can edit participants.')
        serializer.save()

    def perform_destroy(self, instance):
        if not self.request.user.is_superuser:
            raise PermissionDenied('Only admin can remove participants.')
        instance.is_active = False
        instance.save()
        try:
            ledger           = instance.ledger.get()
            ledger.is_active = False
            ledger.save()
        except CollectionLedger.DoesNotExist:
            pass
        vishi              = instance.vishi
        count              = vishi.participants.filter(is_active=True).count()
        vishi.total_cycles = count
        vishi.finish_date  = compute_finish_date(vishi.start_date, count, vishi.frequency)
        vishi.save()


class CollectionLedgerViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class   = CollectionLedgerSerializer
    permission_classes = [IsAuthenticatedReadOrSuperUserWrite]
    filter_backends    = [filters.SearchFilter, filters.OrderingFilter]
    search_fields      = ['status', 'participant__vishi_name', 'participant__user__mobile_number']
    ordering_fields    = ['balance', 'status', 'last_charged_at', 'last_paid_at']

    def get_queryset(self):
        user     = self.request.user
        vishi_id = self.kwargs['vishi_pk']
        if user.is_superuser:
            return CollectionLedger.objects.filter(vishi_id=vishi_id, is_active=True)
        return CollectionLedger.objects.filter(
            vishi_id=vishi_id, participant__user=user, is_active=True
        )

    # FIXED: Decimal instead of float
    @action(detail=True, methods=['post'], url_path='record_payment', permission_classes=[IsSuperUser])
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
    filter_backends    = [filters.SearchFilter, filters.OrderingFilter]
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
