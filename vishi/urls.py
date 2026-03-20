# vishi/urls.py

from django.urls import path
from rest_framework_nested import routers
from rest_framework.routers import DefaultRouter
from .views import (
    VishiViewSet, VishiParticipantViewSet,
    CollectionLedgerViewSet, PaymentEntryViewSet,
    SkipRecordViewSet,
    DashboardView, PaymentsSummaryView,
    MyVishisView, MyPaymentsView,              # ← add
)


router = DefaultRouter()
router.register(r'vishis', VishiViewSet, basename='vishis')

vishi_router = routers.NestedDefaultRouter(router, r'vishis', lookup='vishi')
vishi_router.register(r'participants', VishiParticipantViewSet, basename='vishi-participants')
vishi_router.register(r'ledgers',      CollectionLedgerViewSet, basename='vishi-ledgers')
vishi_router.register(r'skip-records', SkipRecordViewSet,       basename='vishi-skip-records')

ledger_router = routers.NestedDefaultRouter(vishi_router, r'ledgers', lookup='ledger')
ledger_router.register(r'entries', PaymentEntryViewSet, basename='ledger-entries')

urlpatterns = (
    router.urls
    + vishi_router.urls
    + ledger_router.urls
    + [
        path('dashboard/',           DashboardView.as_view(),    name='dashboard'),
        path('payments-summary/',    PaymentsSummaryView.as_view(), name='payments-summary'),
        path('profile/me/vishis/',   MyVishisView.as_view(),     name='my-vishis'),   # ← add
        path('profile/me/payments/', MyPaymentsView.as_view(),   name='my-payments'), # ← add
    ]
)
