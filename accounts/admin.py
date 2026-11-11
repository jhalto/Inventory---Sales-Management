from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from .forms import AccountChangeForm, AccountCreationForm, ManualIdentityVerificationForm
from .models import User, EmailOTP, PhoneOTP


@admin.register(User)
class AccountAdmin(UserAdmin):
    add_form = AccountCreationForm
    form = AccountChangeForm
    ordering = ('email',)
    list_display = ('email', 'phone', 'role', 'is_staff', 'is_active')
    search_fields = ('email', 'phone', 'first_name', 'last_name')
    list_filter = ('role', 'is_staff', 'is_superuser', 'is_active', 'email_verified', 'phone_verified')
    readonly_fields = ('last_login', 'date_joined', 'email_verified', 'phone_verified', 'manual_verification')
    fieldsets = (
        (None, {'fields': ('email', 'phone', 'password')}),
        ('Personal information', {'fields': ('first_name', 'last_name')}),
        ('Verification', {'fields': ('email_verified', 'phone_verified', 'manual_verification')}),
        ('Permissions', {'fields': ('role', 'is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Dates', {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'phone', 'usable_password', 'password1', 'password2', 'role'),
        }),
    )

    @admin.display(description='Manual verification')
    def manual_verification(self, obj):
        return format_html('<a href="{}">Verify identity with an audit reason (superusers only)</a>',
            reverse('admin:accounts_user_verify_identity', args=[obj.pk]))

    def get_urls(self):
        return [path('<path:object_id>/verify-identity/', self.admin_site.admin_view(self.verify_identity_view),
                     name='accounts_user_verify_identity'), *super().get_urls()]

    def verify_identity_view(self, request, object_id):
        if not request.user.is_superuser or not self.has_change_permission(request):
            raise PermissionDenied
        user = self.get_object(request, object_id)
        if user is None:
            raise Http404
        initial = {'identity': 'email' if user.email else 'phone',
                   'email_snapshot': user.email or '', 'phone_snapshot': user.phone or ''}
        form = ManualIdentityVerificationForm(request.POST if request.method == 'POST' else None, initial=initial)
        if request.method == 'POST' and form.is_valid():
            with transaction.atomic():
                user = User.objects.select_for_update().filter(pk=user.pk).first()
                if user is None:
                    raise Http404
                identity = form.cleaned_data['identity']
                value = getattr(user, identity)
                if not value or form.cleaned_data[identity + '_snapshot'] != value:
                    form.add_error(None, 'This identity changed or is empty. Reload the page and review it again.')
                else:
                    flag = identity + '_verified'
                    setattr(user, flag, True)
                    user.save(update_fields=[flag])
                    if identity == 'email':
                        EmailOTP.objects.filter(user=user).delete()
                    else:
                        PhoneOTP.objects.filter(phone=value).delete()
                    self.log_change(request, user,
                        f'Manual {identity} verification of {value}. Evidence/reason: {form.cleaned_data["reason"]}')
                    self.message_user(request, f'{identity.title()} manually verified; audit entry recorded.')
                    return HttpResponseRedirect(reverse('admin:accounts_user_change', args=[user.pk]))
        context = {**self.admin_site.each_context(request), 'title': 'Manually verify identity',
                   'opts': self.model._meta, 'original': user, 'form': form}
        return TemplateResponse(request, 'admin/accounts/user/verify_identity.html', context)
