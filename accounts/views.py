from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied

from .models import User
from .serializers import UserSerializer
from .permissions import IsSuperUser


class UserViewSet(viewsets.ModelViewSet):
    queryset           = User.objects.all().order_by('date_joined')
    serializer_class   = UserSerializer
    permission_classes = [IsSuperUser]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]  # ← ADDED DjangoFilterBackend
    filterset_fields   = ['is_active', 'is_superuser']                                        # ← ADDED for ?is_active=true filter
    search_fields      = ['mobile_number', 'username']
    ordering_fields    = ['date_joined', 'username', 'mobile_number']

    def destroy(self, request, *args, **kwargs):
        user = self.get_object()
        if user.is_superuser:
            raise PermissionDenied("Superuser cannot be deactivated.")
        user.is_active = False
        user.save(update_fields=['is_active'])
        return Response({'detail': 'User deactivated.'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='clear-password')  # ← FIXED: hyphen
    def clear_password(self, request, pk=None):
        user          = self.get_object()
        user.password = None
        user.save(update_fields=['password'])
        return Response({'detail': 'Password cleared.'})

    @action(detail=True, methods=['post'], url_path='activate')
    def activate(self, request, pk=None):
        user           = self.get_object()
        user.is_active = True
        user.save(update_fields=['is_active'])
        return Response({'detail': 'User activated.'})


class AuthViewSet(viewsets.ViewSet):
    permission_classes = [AllowAny]

    @action(detail=False, methods=['post'], url_path='check-number')
    def check_number(self, request):
        mobile = request.data.get('mobile_number', '').strip()

        try:
            user = User.objects.get(mobile_number=mobile)
        except User.DoesNotExist:
            return Response(
                {'detail': 'No account found. Contact your admin.'},
                status=status.HTTP_404_NOT_FOUND
            )

        # ← FIXED: differentiate inactive vs non-existent
        if not user.is_active:
            return Response(
                {'detail': 'Your account is deactivated. Contact admin.'},
                status=status.HTTP_403_FORBIDDEN
            )

        return Response({
            'password_set': bool(user.password),
            'username':     user.username or None,
        })

    @action(detail=False, methods=['post'], url_path='set-password')
    def set_password(self, request):
        mobile   = request.data.get('mobile_number', '').strip()
        password = request.data.get('password', '').strip()
        confirm  = request.data.get('confirm_password', '').strip()

        # ← ADDED: all validations upfront before DB hit
        if not password:
            return Response(
                {'detail': 'Password is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if len(password) < 6:  # ← ADDED: min length from flow Screen 2A
            return Response(
                {'detail': 'Password must be at least 6 characters.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if password != confirm:
            return Response(
                {'detail': 'Passwords do not match.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            user = User.objects.get(mobile_number=mobile)
        except User.DoesNotExist:
            return Response(
                {'detail': 'No account found.'},
                status=status.HTTP_404_NOT_FOUND
            )

        # ← FIXED: explicit inactive check
        if not user.is_active:
            return Response(
                {'detail': 'Your account is deactivated. Contact admin.'},
                status=status.HTTP_403_FORBIDDEN
            )

        if user.password:
            return Response(
                {'detail': 'Password already set. Contact admin to reset.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        user.set_password(password)
        user.last_login = timezone.now()
        user.save(update_fields=['password', 'last_login'])

        return Response({
            'detail':       'Password set successfully.',
            'username':     user.username or None,
            'is_superuser': user.is_superuser,
        })

    @action(detail=False, methods=['post'], url_path='login')
    def login(self, request):
        mobile   = request.data.get('mobile_number', '').strip()
        password = request.data.get('password', '').strip()

        try:
            user = User.objects.get(mobile_number=mobile)
        except User.DoesNotExist:
            return Response(
                {'detail': 'No account found.'},
                status=status.HTTP_404_NOT_FOUND
            )

        # ← FIXED: explicit inactive check
        if not user.is_active:
            return Response(
                {'detail': 'Your account is deactivated. Contact admin.'},
                status=status.HTTP_403_FORBIDDEN
            )

        if not user.password:
            # User exists but never set password — redirect to Screen 2A
            return Response({'password_set': False}, status=status.HTTP_200_OK)

        if not user.check_password(password):
            return Response(
                {'detail': 'Incorrect password.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        user.last_login = timezone.now()
        user.save(update_fields=['last_login'])

        return Response({
            'detail':       'Login successful.',
            'username':     user.username or None,
            'is_superuser': user.is_superuser,
        })


class ProfileViewSet(viewsets.ViewSet):

    @action(detail=False, methods=['get'], url_path='me')
    def me(self, request):
        return Response(UserSerializer(request.user).data)

    @action(detail=False, methods=['patch'], url_path='me/update')
    def update_me(self, request):
        # ← FIXED: removed superuser-only restriction — all users can edit own profile
        allowed    = ['username', 'address', 'additional_contacts']
        data       = {k: v for k, v in request.data.items() if k in allowed}
        serializer = UserSerializer(request.user, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='clear-my-password')
    def clear_my_password(self, request):
        request.user.password = None
        request.user.save(update_fields=['password'])
        return Response({'detail': 'Password cleared. Please set a new one on next login.'})
