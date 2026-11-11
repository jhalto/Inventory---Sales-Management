from rest_framework.response import Response


def authentication_response(user, access_token, refresh_token, message):
    """Public success contract shared by every app-token issuing endpoint."""
    return Response({
        'status': True,
        'message': message,
        'data': {
            'user': {
                'id': user.pk,
                'email': user.email,
                'phone': user.phone,
                'role': user.role,
            },
            'access_token': str(access_token),
            'refresh_token': str(refresh_token),
        },
    }, headers={'Cache-Control': 'no-store'})
