from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.inventory.api import views as inventory_api
from apps.users import api as users_api

router = DefaultRouter()
router.register("materials", inventory_api.MaterialViewSet, basename="material")
router.register("whole-materials", inventory_api.WholeMaterialViewSet, basename="whole-material")
router.register("offcuts", inventory_api.LeftoverOffcutViewSet, basename="offcut")
router.register("transactions", inventory_api.TransactionViewSet, basename="transaction")
router.register("projects", inventory_api.ProjectViewSet, basename="project")
router.register("expenses", inventory_api.ExpenseViewSet, basename="expense")
router.register("pending-users", users_api.PendingUserViewSet, basename="pending-user")

app_name = "api"

urlpatterns = [
    path("auth/register/", users_api.RegistrationAPIView.as_view(), name="register"),
    path("me/", users_api.MeAPIView.as_view(), name="me"),
    path("kirim/", inventory_api.KirimAPIView.as_view(), name="kirim"),
    path("dashboard/", inventory_api.DashboardAPIView.as_view(), name="dashboard"),
    path("", include(router.urls)),
]
