"""Shared fixtures. All data here is fabricated."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from peerpass.config import Config
from peerpass.gateway import Gateway
from peerpass.store import Denied, Store

NOW = 1800000000


class Recorder:
    """A host adapter that records every request and returns a fixed reply."""

    def __init__(self, reply='safe reply'):
        self.reply, self.calls = reply, []

    def __call__(self, request):
        self.calls.append(request)
        return self.reply(request) if callable(self.reply) else self.reply


class GatewayCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = NOW
        self.config = Config(host_name='Atlas', owner_name='Alex', state_dir=Path(self.tmp.name))
        self.store = Store(Path(self.tmp.name) / 'test.sqlite3', self.config, lambda: self.now)
        self.host = Recorder()
        self.gw = Gateway(self.store, self.host, config=self.config)

    def tearDown(self):
        self.gw.close()
        self.tmp.cleanup()

    @property
    def calls(self):
        return [(r.grant['id'], r.message) for r in self.host.calls]

    def issue(self, profile='delegate', **kw):
        return self.store.issue({'profile': profile, 'label': 'test', 'purpose': 'test boundary', **kw})

    def denied(self, status, fn, *args, **kwargs):
        with self.assertRaises(Denied) as c:
            fn(*args, **kwargs)
        self.assertEqual(c.exception.status, status)
        return c.exception
