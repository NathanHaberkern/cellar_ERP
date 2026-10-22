from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="clubdesk"),
    path("<int:pk>/<str:verb>/", views.action, name="clubdesk-action"),
]
