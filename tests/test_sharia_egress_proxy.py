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

    def test_screener_has_only_the_internal_egress_network(self):
        service = self.compose['services']['sharia-screener']
        self.assertEqual(service['networks'], ['sharia-egress'])
        self.assertTrue(self.compose['networks']['sharia-egress']['internal'])
        self.assertEqual(
            service['environment']['HTTPS_PROXY'],
            'http://sharia-egress-proxy:8080')

    def test_only_secretless_proxy_bridges_internal_and_default_networks(self):
        proxy = self.compose['services']['sharia-egress-proxy']
        self.assertEqual(set(proxy['networks']), {'default', 'sharia-egress'})
        serialized = str(proxy['environment']).upper()
        for secret_name in ('BINANCE_API', 'TELEGRAM', 'HMAC', 'SIGNING',
                            'APPROVAL'):
            self.assertNotIn(secret_name, serialized)

    def test_screener_waits_for_a_healthy_proxy(self):
        dependency = self.compose['services']['sharia-screener'][
            'depends_on']['sharia-egress-proxy']
        self.assertEqual(dependency['condition'], 'service_healthy')

    def test_explicit_pinned_proxy_mode_disables_only_the_direct_peer_check(self):
        from services.sharia_retriever.retriever import Retriever
        with mock.patch.dict(os.environ, {'SHARIA_PINNED_EGRESS_PROXY': 'true'}):
            self.assertFalse(Retriever().verify_peer)
            self.assertTrue(Retriever(verify_peer=True).verify_peer)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(Retriever().verify_peer)
