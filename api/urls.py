from django.urls import path

from .views.accounts.views import register

urlpatterns = [
    path("register/", register, name='register')
]