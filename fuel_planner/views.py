import logging
from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.views import exception_handler
from fuel_planner.serializers import RoutePlanRequestSerializer
from fuel_planner.services.planner import RoutePlanningService
from fuel_planner.services.geocoding import GeocodingError
from fuel_planner.services.routing import RoutingError

logger = logging.getLogger(__name__)


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None:
        response.data = {
            'success': False,
            'error': response.data
        }
    return response


class RoutePlanAPIView(APIView):
    """
    POST /api/v1/route-plan/
    Plans fuel stops along a driving route within the United States.
    Distinguishes client validation errors (400), unprocessable plans (422),
    and provider errors (502/503).
    """

    def post(self, request, *args, **kwargs):
        serializer = RoutePlanRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    'success': False,
                    'errors': serializer.errors,
                    'message': 'Invalid input parameters.'
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        data = serializer.validated_data
        planner = RoutePlanningService()

        try:
            result = planner.plan_route(
                start_input=data['start'],
                finish_input=data['finish'],
                starting_fuel_gallons=data.get('starting_fuel_gallons'),
                max_off_route_distance=data.get('max_off_route_distance_miles'),
                allow_approximate_stations=data.get('allow_approximate_stations', False)
            )

            if not result.get('success', False):
                return Response(result, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

            return Response(result, status=status.HTTP_200_OK)

        except GeocodingError as e:
            err_msg = str(e)
            if "rate limit" in err_msg.lower():
                return Response({'success': False, 'error': err_msg}, status=status.HTTP_429_TOO_MANY_REQUESTS)
            if "provider error" in err_msg.lower() or "authentication" in err_msg.lower():
                return Response({'success': False, 'error': err_msg}, status=status.HTTP_502_BAD_GATEWAY)
            # Input outside USA or not found
            return Response({'success': False, 'error': err_msg}, status=status.HTTP_400_BAD_REQUEST)

        except RoutingError as e:
            err_msg = str(e)
            if "rate limit" in err_msg.lower():
                return Response({'success': False, 'error': err_msg}, status=status.HTTP_429_TOO_MANY_REQUESTS)
            if "not found" in err_msg.lower() or "no driving route" in err_msg.lower():
                return Response({'success': False, 'error': err_msg}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
            return Response({'success': False, 'error': err_msg}, status=status.HTTP_502_BAD_GATEWAY)

        except Exception as e:
            logger.exception("Unexpected error during route planning")
            return Response(
                {'success': False, 'error': "An internal server error occurred while calculating the route."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


def map_demo_view(request):
    """Renders the Leaflet map demo interface."""
    return render(request, 'map_demo.html')
