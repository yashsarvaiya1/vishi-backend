# vishi/urls.py

from rest_framework_nested import routers
from rest_framework.routers import DefaultRouter
from .views import (
    VishiViewSet, VishiParticipantViewSet,
    CollectionLedgerViewSet, PaymentEntryViewSet
)

router = DefaultRouter()
router.register(r'vishis', VishiViewSet, basename='vishis')

vishi_router = routers.NestedDefaultRouter(router, r'vishis', lookup='vishi')
vishi_router.register(r'participants', VishiParticipantViewSet, basename='vishi-participants')
vishi_router.register(r'ledgers',      CollectionLedgerViewSet, basename='vishi-ledgers')

ledger_router = routers.NestedDefaultRouter(vishi_router, r'ledgers', lookup='ledger')
ledger_router.register(r'entries', PaymentEntryViewSet, basename='ledger-entries')

urlpatterns = router.urls + vishi_router.urls + ledger_router.urls
