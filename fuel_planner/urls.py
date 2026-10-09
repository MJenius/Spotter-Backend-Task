from django.urls import path
from fuel_planner.views import RoutePlanAPIView, map_demo_view

urlpatterns = [
    path('api/v1/route-plan/', RoutePlanAPIView.as_view(), name='route-plan'),
    path('', map_demo_view, name='map-demo'),
]
