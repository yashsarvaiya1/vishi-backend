# accounts/urls.py

from rest_framework.routers import DefaultRouter
from .views import UserViewSet, AuthViewSet, ProfileViewSet

router = DefaultRouter()
router.register(r'users',   UserViewSet,   basename='users')
router.register(r'auth',    AuthViewSet,   basename='auth')
router.register(r'profile', ProfileViewSet, basename='profile')

urlpatterns = router.urls
