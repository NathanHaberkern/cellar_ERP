from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="shipping"),
    path("pull/", views.pull, name="shipping-pull"),
    path("build/", views.build, name="shipping-build"),
    path("pools/<int:pk>/dissolve/", views.dissolve, name="shipping-dissolve"),
    path("orders/<int:pk>/address/", views.edit_address, name="shipping-address"),
    path("orders/<int:pk>/<str:action>/", views.order_action, name="shipping-order-action"),
]
