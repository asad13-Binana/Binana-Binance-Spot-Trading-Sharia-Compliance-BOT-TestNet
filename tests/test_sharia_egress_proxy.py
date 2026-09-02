"""Security invariants for the network-isolated Sharia HTTPS proxy."""
from __future__ import annotations

import socket
import os
import unittest
from pathlib import Path
from unittest import mock

import yaml

from services.sharia_egress_proxy.server import (
    ProxyRefused,
    canonical_target,
    resolve_public,
)


ROOT = Path(__file__).resolve().parents[1]


class ConnectTargetTests(unittest.TestCase):
    def test_only_credential_free_dns_names_on_https_are_allowed(self):
        self.assertEqual(
            canonical_target('Docs.Example.COM.:443'),
            ('docs.example.com', 443))
        refused = (
            '127.0.0.1:443', '[::1]:443', '169.254.169.254:443',
            'user@example.com:443', 'example.com:80', 'localhost:443',
            'example.com/path:443', 'com:443',
        )
        for target in refused:
            with self.subTest(target=target), self.assertRaises(ProxyRefused):
                canonical_target(target)

    def test_any_private_answer_rejects_the_entire_dns_set(self):
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
             ('93.184.216.34', 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
             ('169.254.169.254', 443)),
        ]
        with mock.patch('socket.getaddrinfo', return_value=answers):
            with self.assertRaisesRegex(ProxyRefused, 'non-public'):
                resolve_public('approved.example', 443)

    def test_public_resolution_is_returned_as_numeric_socket_addresses(self):
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
             ('93.184.216.34', 443)),
        ]
        with mock.patch('socket.getaddrinfo', return_value=answers):
            self.assertEqual(
                resolve_public('approved.example', 443),
                [(socket.AF_INET, ('93.184.216.34', 443))])


class ComposeIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = yaml.safe_load(
            (ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))

    def test_manual_registry_projector_is_networkless_and_secret_minimal(self):
        service = self.compose['services']['sharia-screener']
        self.assertEqual(service['network_mode'], 'none')
        self.assertNotIn('networks', service)
        self.assertNotIn('depends_on', service)
        environment = service['environment']
        self.assertEqual(environment['SHARIA_REGISTRY_MODE'], 'manual')
        for name in ('HTTPS_PROXY', 'COINGECKO_API_KEY',
                     'COINMARKETCAP_API_KEY', 'CMC_API_KEY',
                     'SHARIA_HMAC_KEY', 'SHARIA_APPROVAL_HMAC_KEY'):
            self.assertNotIn(name, environment)

    def test_only_secretless_proxy_bridges_internal_and_default_networks(self):
        proxy = self.compose['services']['sharia-egress-proxy']
        self.assertEqual(proxy['profiles'], ['automatic-sharia-research'])
        self.assertEqual(set(proxy['networks']), {'runtime-egress', 'sharia-egress'})
        serialized = str(proxy['environment']).upper()
        for secret_name in ('BINANCE_API', 'TELEGRAM', 'HMAC', 'SIGNING',
                            'APPROVAL'):
            self.assertNotIn(secret_name, serialized)

    def test_dormant_proxy_is_not_a_required_default_service(self):
        installer = (ROOT / 'deploy/install_artifact.sh').read_text(encoding='utf-8')
        required = installer.split('REQUIRED_SERVICES=(', 1)[1].split(')', 1)[0]
        self.assertNotIn('sharia-egress-proxy', required)

    def test_explicit_pinned_proxy_mode_disables_only_the_direct_peer_check(self):
        from services.sharia_retriever.retriever import Retriever
        with mock.patch.dict(os.environ, {'SHARIA_PINNED_EGRESS_PROXY': 'true'}):
            self.assertFalse(Retriever().verify_peer)
            self.assertTrue(Retriever(verify_peer=True).verify_peer)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(Retriever().verify_peer)
