from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("admin-dashboard/notifications/read-all/", views.NotificationReadAllView.as_view(), name="notifications_read_all"),
    path("admin-dashboard/notifications/<str:key>/read/", views.NotificationReadView.as_view(), name="notification_read"),
    path("inventory/material-suggestions/", views.MaterialSuggestionsView.as_view(), name="material_suggestions"),
    path("inventory/whole/<int:pk>/withdraw/", views.WithdrawWholeView.as_view(), name="withdraw_whole"),
    path("inventory/offcuts/add/", views.OffcutCreateView.as_view(), name="offcut_add"),
    path("inventory/offcuts/<int:pk>/withdraw/", views.OffcutWithdrawView.as_view(), name="offcut_withdraw"),
    path("inventory/history/", views.MyHistoryView.as_view(), name="my_history"),
    path("admin-dashboard/transactions/<int:pk>/reverse/", views.ReverseTransactionView.as_view(), name="reverse_transaction"),
    # Admin dashboard
    path("admin-dashboard/", views.DashboardView.as_view(), name="dashboard"),
    path("admin-dashboard/materials/", views.MaterialListView.as_view(), name="material_list"),
    path("admin-dashboard/materials/new/", views.MaterialCreateView.as_view(), name="material_create"),
    path("admin-dashboard/materials/<int:pk>/edit/", views.MaterialUpdateView.as_view(), name="material_edit"),
    path("admin-dashboard/kirim/", views.KirimView.as_view(), name="kirim"),
    path("admin-dashboard/expenses/", views.ExpenseListCreateView.as_view(), name="expenses"),
    path("admin-dashboard/expenses/export.csv", views.ExpenseExportView.as_view(), name="expenses_export"),
    path("admin-dashboard/reports/", views.ReportView.as_view(), name="reports"),
    path("admin-dashboard/reports/export.csv", views.ReportExportView.as_view(), name="reports_export"),
]
