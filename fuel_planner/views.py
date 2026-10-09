import logging
from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.views import exception_handler
from fuel_planner.serializers import RoutePlanRequestSerializer
from fuel_planner.services.planner import RoutePlanningService
from fuel_planner.services.geocoding import (
    GeocodingError,
    GeocodingRateLimitError,
    GeocodingAuthError,
    GeocodingLocationNotFoundError,
    GeocodingProviderError,
)
from fuel_planner.services.routing import (
    RoutingError,
    RoutingRateLimitError,
    RoutingAuthError,
    RoutingNotFoundError,
    RoutingSchemaError,
)

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
    upstream rate limits (429), and upstream provider errors (502).
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

        except GeocodingRateLimitError as e:
            return Response({'success': False, 'error': str(e)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        except GeocodingAuthError as e:
            return Response({'success': False, 'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        except GeocodingLocationNotFoundError as e:
            return Response({'success': False, 'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except GeocodingProviderError as e:
            return Response({'success': False, 'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        except GeocodingError as e:
            return Response({'success': False, 'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        except RoutingRateLimitError as e:
            return Response({'success': False, 'error': str(e)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        except RoutingAuthError as e:
            return Response({'success': False, 'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        except RoutingNotFoundError as e:
            return Response({'success': False, 'error': str(e)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except RoutingSchemaError as e:
            return Response({'success': False, 'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        except RoutingError as e:
            return Response({'success': False, 'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        except Exception as e:
            logger.exception("Unexpected error during route planning")
            return Response(
                {'success': False, 'error': "An internal server error occurred while calculating the route."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


def map_demo_view(request):
    """Renders the Leaflet map demo interface."""
    return render(request, 'map_demo.html')
