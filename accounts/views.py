# accounts/views.py

from django.utils import timezone
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
    filter_backends    = [filters.SearchFilter, filters.OrderingFilter]
    search_fields      = ['mobile_number', 'username']
    ordering_fields    = ['date_joined', 'username', 'mobile_number']

    def destroy(self, request, *args, **kwargs):
        user = self.get_object()
        if user.is_superuser:
            raise PermissionDenied("Superuser cannot be deleted.")
        user.is_active = False
        user.save()
        return Response({'detail': 'User deactivated.'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='clear_password')
    def clear_password(self, request, pk=None):
        user          = self.get_object()
        user.password = None
        user.save()
        return Response({'detail': 'Password cleared.'})

    @action(detail=True, methods=['post'], url_path='activate')
    def activate(self, request, pk=None):
        user           = self.get_object()
        user.is_active = True
        user.save()
        return Response({'detail': 'User activated.'})


class AuthViewSet(viewsets.ViewSet):
    permission_classes = [AllowAny]

    @action(detail=False, methods=['post'], url_path='check-number')
    def check_number(self, request):
        mobile = request.data.get('mobile_number', '').strip()
        try:
            user = User.objects.get(mobile_number=mobile, is_active=True)
            return Response({
                'password_set': bool(user.password),
                'username':     user.username or None,
            })
        except User.DoesNotExist:
            return Response({'detail': 'No user found.'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['post'], url_path='set-password')
    def set_password(self, request):
        mobile   = request.data.get('mobile_number', '').strip()
        password = request.data.get('password', '').strip()
        confirm  = request.data.get('confirm_password', '').strip()
        if not password or password != confirm:
            return Response({'detail': 'Passwords do not match.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            user = User.objects.get(mobile_number=mobile, is_active=True)
            if user.password:
                return Response({'detail': 'Password already set.'}, status=status.HTTP_400_BAD_REQUEST)
            user.set_password(password)
            user.save()
            return Response({
                'detail':       'Password set successfully.',
                'username':     user.username or None,
                'is_superuser': user.is_superuser,
            })
        except User.DoesNotExist:
            return Response({'detail': 'No user found.'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['post'], url_path='login')
    def login(self, request):
        mobile   = request.data.get('mobile_number', '').strip()
        password = request.data.get('password', '').strip()
        try:
            user = User.objects.get(mobile_number=mobile, is_active=True)
            if not user.password:
                return Response({'password_set': False}, status=status.HTTP_200_OK)
            if not user.check_password(password):
                return Response({'detail': 'Incorrect password.'}, status=status.HTTP_401_UNAUTHORIZED)
            user.last_login = timezone.now()
            user.save(update_fields=['last_login'])
            return Response({
                'detail':       'Login successful.',
                'username':     user.username or None,
                'is_superuser': user.is_superuser,
            })
        except User.DoesNotExist:
            return Response({'detail': 'No user found.'}, status=status.HTTP_404_NOT_FOUND)


class ProfileViewSet(viewsets.ViewSet):

    @action(detail=False, methods=['get'], url_path='me')
    def me(self, request):
        return Response(UserSerializer(request.user).data)

    @action(detail=False, methods=['patch'], url_path='me/update')
    def update_me(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied("Profile editing is not allowed.")
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
        return Response({'detail': 'Password cleared. Please set a new one.'})
