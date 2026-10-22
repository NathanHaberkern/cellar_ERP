from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="shipping"),
    path("pull/", views.pull, name="shipping-pull"),
    path("build/", views.build, name="shipping-build"),
    path("recheck/", views.recheck, name="shipping-recheck"),
    path("pools/<int:pk>/", views.pool_detail, name="shipping-pool"),
    path("pools/<int:pk>/dissolve/", views.dissolve, name="shipping-dissolve"),
    path("pools/<int:pk>/buy/", views.pool_buy, name="shipping-pool-buy"),
    path("pools/<int:pk>/labels.pdf", views.pool_pdf, name="shipping-pool-pdf"),
    path("pools/<int:pk>/post/", views.pool_post, name="shipping-pool-post"),
    path("packages/<int:pk>/buy/", views.package_buy, name="shipping-package-buy"),
    path("labels/<int:pk>.pdf", views.label_pdf, name="shipping-label-pdf"),
    path("labels/<int:pk>/void/", views.label_void, name="shipping-label-void"),
    path("orders/<int:pk>/post/", views.order_post, name="shipping-order-post"),
    path("orders/<int:pk>/address/", views.edit_address, name="shipping-address"),
    path("orders/<int:pk>/<str:action>/", views.order_action, name="shipping-order-action"),
]
