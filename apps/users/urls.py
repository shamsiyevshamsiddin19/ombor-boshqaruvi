from django.urls import path

from . import views

app_name = "users"

urlpatterns = [
    path("accounts/login/", views.LoginView.as_view(), name="login"),
    path("accounts/logout/", views.LogoutView.as_view(), name="logout"),
    path("accounts/register/", views.RegisterView.as_view(), name="register"),
    path("accounts/register/done/", views.RegistrationDoneView.as_view(), name="register_done"),
    # Admin dashboard: approval workflow & account management
    path("admin-dashboard/pending-users/", views.PendingUserListView.as_view(), name="pending_users"),
    path(
        "admin-dashboard/pending-users/<int:pk>/",
        views.PendingUserReviewView.as_view(),
        name="pending_user_review",
    ),
    path(
        "admin-dashboard/pending-users/<int:pk>/reject/",
        views.RejectRegistrationView.as_view(),
        name="pending_user_reject",
    ),
    path("admin-dashboard/users/", views.UserListView.as_view(), name="user_list"),
    path("admin-dashboard/users/<int:pk>/edit/", views.UserUpdateView.as_view(), name="user_edit"),
    path("admin-dashboard/users/<int:pk>/delete/", views.WorkerDeleteView.as_view(), name="user_delete"),
    path("admin-dashboard/worker-types/new/", views.WorkerRoleCreateView.as_view(), name="worker_role_create"),
]
