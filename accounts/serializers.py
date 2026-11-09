from rest_framework import serializers
from .models import User

from django.contrib.auth.password_validation import validate_password

class UserSerializers(serializers.ModelSerializer):

    password = serializers.CharField(
        write_only =True,
        validators = [validate_password]
    )

    class Meta:
        model = User
        fields = [
            'id',
            'username',
            'email',
            'password',
            'phone',
        ]
        read_only_fields = ['id']

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)