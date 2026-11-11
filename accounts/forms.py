from django import forms
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm

from .models import User
from .identities import normalize_email, normalize_phone


class EmailIdentityMixin:
    def clean_phone(self):
        phone = normalize_phone(self.cleaned_data.get('phone'))
        if phone and User.objects.filter(phone=phone).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('This phone number is already registered.')
        return phone

    def clean_email(self):
        email = self.cleaned_data.get('email')
        if not email:
            return None
        email = normalize_email(email)
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('This email is already registered.')
        return email


class AccountCreationForm(EmailIdentityMixin, AdminUserCreationForm):
    email = forms.EmailField(required=True)

    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ('email', 'phone')
        field_classes = {}


class AccountChangeForm(EmailIdentityMixin, UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = '__all__'
        field_classes = {}

    def _post_clean(self):
        super()._post_clean()
        # AbstractUser.clean() converts a missing email to an empty string.
        # Keep optional unique emails nullable for phone-only accounts.
        if not self.instance.email:
            self.instance.email = None


class ManualIdentityVerificationForm(forms.Form):
    identity = forms.ChoiceField(choices=[('email', 'Email'), ('phone', 'Phone')])
    reason = forms.CharField(max_length=500, widget=forms.Textarea, help_text='Describe the evidence used to verify ownership.')
    email_snapshot = forms.CharField(required=False, widget=forms.HiddenInput)
    phone_snapshot = forms.CharField(required=False, widget=forms.HiddenInput)
