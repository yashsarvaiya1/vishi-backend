# accounts/serializers.py

from rest_framework import serializers
from .models import User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model  = User
        fields = [
            'id', 'mobile_number', 'username', 'address',
            'additional_contacts', 'is_active', 'is_superuser',
            'date_joined', 'last_login', 'password',
        ]
        extra_kwargs = {
            'password':     {'write_only': True, 'required': False, 'allow_null': True},
            'is_superuser': {'read_only': True},
            'is_active':    {'read_only': True},
            'date_joined':  {'read_only': True},
            'last_login':   {'read_only': True},
        }

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.password = None
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class UserPublicSerializer(serializers.ModelSerializer):
    class Meta:
        model  = User
        fields = ['id', 'username', 'mobile_number']
