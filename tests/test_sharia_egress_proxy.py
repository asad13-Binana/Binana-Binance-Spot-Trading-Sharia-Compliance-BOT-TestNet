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
