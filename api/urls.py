from django.urls import path

from .views.accounts.views import register, send_otp, verify_otp, login, verify_email, resend_verification_email

urlpatterns = [
    path("register/", register, name="register"),
    path('login/', login, name= "login"),
    path("email/verify/", verify_email, name="verify-email"),
    path("email/resend-verification/", resend_verification_email, name="resend-verification-email"),
    path("phone/send-otp/", send_otp, name="send-otp"),
    path("phone/verify-otp/", verify_otp, name="verify-otp"),
]
