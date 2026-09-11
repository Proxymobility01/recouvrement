import json
import time
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import TestCase, override_settings
from rest_framework_simplejwt.exceptions import InvalidToken

from core.authentication import KeycloakJWTAuthentication

SIMPLE_JWT_TEST = {
    'ALGORITHM': 'RS256',
    'AUDIENCE': 'recouvrement_app',
    'ISSUER': 'https://keycloak.test/realms/test',
    'LEEWAY': 30,
}


class ValidationTokenAvecDecalageHorlogeTests(TestCase):
    """
    Keycloak tourne sur une machine distincte de ce backend : un léger
    décalage d'horloge entre les deux est normal (dérive réseau/NTP) et ne
    doit pas faire échouer toutes les connexions. C'est le rôle de
    SIMPLE_JWT['LEEWAY'], qui doit être effectivement transmis à
    jwt.decode() (régression : il était défini en settings mais jamais lu).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cle_privee = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(cls.cle_privee.public_key())
        )
        jwk.update({'kid': 'test-kid', 'use': 'sig', 'alg': 'RS256'})
        cls.jwks = {'keys': [jwk]}

    def _jeton(self, decalage_iat_secondes):
        maintenant = int(time.time())
        payload = {
            'sub': 'kc-utilisateur-test',
            'iat': maintenant + decalage_iat_secondes,
            'exp': maintenant + decalage_iat_secondes + 300,
            'aud': SIMPLE_JWT_TEST['AUDIENCE'],
            'iss': SIMPLE_JWT_TEST['ISSUER'],
        }
        return jwt.encode(
            payload, self.cle_privee, algorithm='RS256',
            headers={'kid': 'test-kid'},
        )

    @override_settings(SIMPLE_JWT=SIMPLE_JWT_TEST)
    def test_leger_decalage_horloge_est_tolere(self):
        auth = KeycloakJWTAuthentication()
        # AccessToken(..., verify=False) ne sert ici que de conteneur pour le
        # payload déjà validé par notre propre jwt.decode() juste avant (cf.
        # get_validated_token) : on la neutralise pour ne pas dépendre de sa
        # propre config de clé de vérification, hors-sujet ici (ce qu'on
        # teste, c'est le leeway sur iat dans NOTRE decode()).
        with (
            patch.object(auth, 'get_jwks', return_value=self.jwks),
            patch('core.authentication.AccessToken') as ClasseAccessToken,
        ):
            token = auth.get_validated_token(self._jeton(decalage_iat_secondes=15))
        self.assertEqual(token.payload['sub'], 'kc-utilisateur-test')
        ClasseAccessToken.assert_called_once()

    @override_settings(SIMPLE_JWT=SIMPLE_JWT_TEST)
    def test_decalage_horloge_trop_important_reste_refuse(self):
        # Le leeway absorbe une petite dérive, mais ne désactive pas la
        # vérification : un token réellement émis trop tôt doit toujours
        # être rejeté.
        auth = KeycloakJWTAuthentication()
        with patch.object(auth, 'get_jwks', return_value=self.jwks):
            with self.assertRaises(InvalidToken):
                auth.get_validated_token(self._jeton(decalage_iat_secondes=120))
