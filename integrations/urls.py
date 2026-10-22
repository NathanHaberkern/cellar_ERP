from django.urls import path

from . import views

urlpatterns = [
    path("qbo/", views.qbo_status, name="qbo-status"),
    path("qbo/connect/", views.qbo_connect, name="qbo-connect"),
    path("qbo/callback/", views.qbo_callback, name="qbo-callback"),
]
