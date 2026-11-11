from django.urls import path
from .views.accounts.tokens import token_refresh, logout
from .views.accounts.self_service import (
    password_reset_request, password_reset_confirm, password_change,
    reauth_request, contact_request, contact_confirm, google_unlink,
)

from .views.accounts.views import register, send_otp, verify_otp, login, verify_email, resend_verification_email
from .views.accounts.google import google_page, google_config, google_login, google_link, google_mobile_login, google_mobile_link

urlpatterns = [
    path('password/reset/request/', password_reset_request, name='password-reset-request'),
    path('password/reset/confirm/', password_reset_confirm, name='password-reset-confirm'),
    path('password/change/', password_change, name='password-change'),
    path('account/reauth/request/', reauth_request, name='reauth-request'),
    path('account/email/request/', contact_request, {'channel': 'email'}, name='account-email-request'),
    path('account/email/confirm/', contact_confirm, {'channel': 'email'}, name='account-email-confirm'),
    path('account/phone/request/', contact_request, {'channel': 'phone'}, name='account-phone-request'),
    path('account/phone/confirm/', contact_confirm, {'channel': 'phone'}, name='account-phone-confirm'),
    path('auth/google/unlink/', google_unlink, name='google-unlink'),
    path('token/refresh/', token_refresh, name='token-refresh'),
    path('logout/', logout, name='logout'),
    path('auth/google/sign-in/', google_page, name='google-sign-in'),
    path('auth/google/config/', google_config, name='google-config'),
    path('auth/google/', google_login, name='google-login'),
    path('auth/google/link/', google_link, name='google-link'),
    path('auth/google/mobile/', google_mobile_login, name='google-mobile-login'),
    path('auth/google/mobile/link/', google_mobile_link, name='google-mobile-link'),
    path("register/", register, name="register"),
    path('login/', login, name= "login"),
    path("email/verify/", verify_email, name="verify-email"),
    path("email/resend-verification/", resend_verification_email, name="resend-verification-email"),
    path("phone/send-otp/", send_otp, name="send-otp"),
    path("phone/verify-otp/", verify_otp, name="verify-otp"),
]
