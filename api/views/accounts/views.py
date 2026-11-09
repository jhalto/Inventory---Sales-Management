from rest_framework.response import Response
from accounts.serializers import UserSerializers
from rest_framework.decorators import api_view
from accounts.models import User
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import authenticate


@api_view(["POST"])
def register(request):

    if request.method == "POST":
        serializer = UserSerializers(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
           
            refresh = RefreshToken.for_user(user)
            
            return Response(
                {
                    "status": True,
                    "message": "User created successfuly",
                    "data": serializer.data,
                    "access_token": str(refresh.access_token),
                    "refresh_token": str(refresh)
                },
                status=status.HTTP_201_CREATED,
            )

        return Response(
            {
                "status": False,
                "message": "Invalid Data",
                "errors": serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )


@api_view(["POST"])
def login(request):

    if request.method == "POST":
        username = request.data.get("username")
        password = request.data.get("password")
        user = authenticate(
            request,
            username=username,
            password=password,
        )
        if user is None:
            return Response(
                {"status": False, "message": "Invalid credentials"},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        refresh = RefreshToken.for_user(user)
   
        return Response(
            {
                "status": True,
                "message": "Login successful",
                "data": {
                    "id": user.id,
                    "username": user.username,
                    "role": user.role,
                    "access_token": str(refresh.access_token),
                    "refresh_token": str(refresh),
                },
            },
            status=status.HTTP_200_OK,
        )
