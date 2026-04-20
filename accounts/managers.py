from django.contrib.auth.base_user import BaseUserManager


class CustomUserManager(BaseUserManager):
    def create_user(self, keycloak_id, compte_id, email=None, password=None, **extra_fields):
        if not keycloak_id:
            raise ValueError("Le Keycloak ID est obligatoire.")
        if not compte_id:
            raise ValueError("Le compte_id est obligatoire pour l'isolation multi-tenant.")

        email = self.normalize_email(email)
        user = self.model(
            keycloak_id=keycloak_id,
            compte_id=compte_id,
            email=email,
            **extra_fields
        )


        if password:

            user.set_password(password)
        else:

            user.set_unusable_password()

        user.save(using=self._db)
        return user


    def create_superuser(self, keycloak_id, compte_id, email=None, password=None, **extra_fields):
        """
        Permet de créer un Super Admin via la commande CLI:
        python manage.py createsuperuser
        """
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError("Le superuser doit avoir is_staff=True.")
        if extra_fields.get('is_superuser') is not True:
            raise ValueError("Le superuser doit avoir is_superuser=True.")

        # 4. On transmet bien le mot de passe à create_user
        return self.create_user(keycloak_id, compte_id, email, password, **extra_fields)