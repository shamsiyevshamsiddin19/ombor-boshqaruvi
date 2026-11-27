import re

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import CreateView, ListView, TemplateView, UpdateView

from . import services
from .forms import LoginForm, RoleAssignmentForm, UserAdminEditForm, WorkerRegistrationForm, WorkerRoleForm
from .models import PHONE_DIGITS, PHONE_PREFIX, User, WorkerRole
from .permissions import WarehouseAdminRequiredMixin


def _get_client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _check_rate_limit(request, scope, max_attempts=10, window=3600):
    if request.method != "POST":
        return None
    from django.conf import settings
    if getattr(settings, "RATELIMIT_DISABLED", False):
        return None
    ip = _get_client_ip(request)
    key = f"ratelimit:{scope}:{ip}"
    try:
        attempts = cache.get(key, 0)
    except Exception:
        return None
    if attempts >= max_attempts:
        return HttpResponse(
            _("Juda ko'p urinish. %(minutes)d daqiqadan keyin qayta urinib ko'ring.")
            % {"minutes": window // 60},
            status=429,
        )
    cache.set(key, attempts + 1, window)
    return None


class LoginView(auth_views.LoginView):
    form_class = LoginForm
    template_name = "users/login.html"
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        blocked = _check_rate_limit(request, "login", max_attempts=10, window=900)
        if blocked:
            return blocked
        return super().post(request, *args, **kwargs)


class LogoutView(auth_views.LogoutView):
    pass


class RegisterView(CreateView):
    form_class = WorkerRegistrationForm
    template_name = "users/register.html"
    success_url = reverse_lazy("users:register_done")

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("inventory:home")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        blocked = _check_rate_limit(request, "register", max_attempts=5, window=3600)
        if blocked:
            return blocked
        return super().post(request, *args, **kwargs)


class RegistrationDoneView(TemplateView):
    template_name = "users/register_done.html"


# ---------------------------------------------------------------------------
# Admin dashboard: worker approval & management
# ---------------------------------------------------------------------------
class PendingUserListView(WarehouseAdminRequiredMixin, ListView):
    template_name = "users/pending_users.html"
    context_object_name = "pending_users"

    def get_queryset(self):
        return User.objects.pending_approval().order_by("date_joined")


class PendingUserReviewView(WarehouseAdminRequiredMixin, UpdateView):
    """Review a registration request, adjust roles, and approve it."""

    form_class = RoleAssignmentForm
    template_name = "users/pending_user_review.html"
    context_object_name = "candidate"

    def get_queryset(self):
        return User.objects.pending_approval()

    def form_valid(self, form):
        user = form.save(commit=False)
        services.approve_user(user, approved_by=self.request.user, roles=form.cleaned_data["roles"])
        messages.success(
            self.request, _("%(name)s tasdiqlandi va tizimga kira oladi.") % {"name": user.display_name}
        )
        return redirect("users:pending_users")


class RejectRegistrationView(WarehouseAdminRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):
        user = get_object_or_404(User.objects.pending_approval(), pk=pk)
        name = user.display_name
        try:
            services.reject_registration(user)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.info(request, _("%(name)s so'rovi rad etildi.") % {"name": name})
        return redirect("users:pending_users")


class UserListView(WarehouseAdminRequiredMixin, ListView):
    template_name = "users/user_list.html"
    context_object_name = "users"
    paginate_by = 50

    def get_queryset(self):
        qs = User.objects.all().order_by("-is_active", "full_name", "username")
        query = self.request.GET.get("q", "").strip()
        if query:
            # A phone may be typed with spaces or without the +998 prefix.
            digits = re.sub(r"\D", "", query)
            match = Q(full_name__istartswith=query) | Q(username__istartswith=query)
            if digits and re.fullmatch(r"[+\d\s().-]+", query):
                if query.startswith("+") or (digits.startswith("998") and len(digits) != PHONE_DIGITS):
                    prefix = "+" + digits
                else:
                    prefix = PHONE_PREFIX + digits.removeprefix("0")
                match |= Q(phone__startswith=prefix)
            qs = qs.filter(match)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["query"] = self.request.GET.get("q", "")
        return context


class UserUpdateView(WarehouseAdminRequiredMixin, UpdateView):
    model = User
    form_class = UserAdminEditForm
    template_name = "users/user_edit.html"
    context_object_name = "account"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["acting_user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["delete_blocker"] = services.worker_deletion_blocker(self.object, acting_user=self.request.user)
        return context

    def form_valid(self, form):
        user = form.save(commit=False)
        if user.is_approved and user.approved_at is None:
            user.approved_at = timezone.now()
            user.approved_by = self.request.user
        user.save()
        messages.success(self.request, _("%(name)s ma'lumotlari saqlandi.") % {"name": user.display_name})
        return redirect(reverse("users:user_list"))


class WorkerDeleteView(WarehouseAdminRequiredMixin, View):
    def post(self, request, pk):
        try:
            name = services.delete_worker(pk, acting_user=request.user)
        except User.DoesNotExist as exc:
            raise Http404 from exc
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("users:user_edit", pk=pk)
        messages.success(request, _("%(name)s hisobi o'chirildi.") % {"name": name})
        return redirect("users:user_list")


class WorkerRoleCreateView(WarehouseAdminRequiredMixin, CreateView):
    form_class = WorkerRoleForm
    template_name = "users/worker_role_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["existing_roles"] = WorkerRole.objects.all()
        return context

    def form_valid(self, form):
        role = form.save()
        messages.success(
            self.request,
            _("“%(name)s” kasbi qo'shildi. Endi ishchilar ro'yxatdan o'tishda uni tanlay oladi.") % {"name": role.name},
        )
        return redirect("users:user_list")

