"""Configuration loading and the owner CLI."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from peerpass import cli, config


class ConfigTests(unittest.TestCase):
    def write(self, text):
        d = tempfile.mkdtemp()
        p = Path(d) / 'peerpass.toml'
        p.write_text(text)
        return p

    def test_defaults_without_a_file(self):
        with patch.dict(os.environ, {}, clear=True), tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                cfg = config.load()
            finally:
                os.chdir(cwd)
        self.assertEqual(set(cfg.profiles), {'delegate', 'colleague', 'friend'})
        self.assertEqual(cfg.host, {'adapter': 'echo'})

    def test_durations(self):
        self.assertEqual(
            [config.duration(v) for v in (60, '90m', '12h', '30d', '2w', '45')], [60, 5400, 43200, 2592000, 1209600, 45]
        )
        for bad in ('soon', '1y', True, None, 1.5):
            with self.assertRaises(ValueError):
                config.duration(bad)

    def test_custom_profile_override_and_state_dir(self):
        p = self.write("""
[peerpass]
host_name = "Atlas"
state_dir = "data"
token_prefix = "atlas_"
[profiles.contractor]
mode = "reviewed"
scopes = ["agent.consult", "jobs.read"]
ttl_default = "7d"
ttl_max = "30d"
max_calls_default = 50
tools = ["docs.search"]
[profiles.delegate]
ttl_default = "1h"
[limits]
job_retention = "2d"
""")
        cfg = config.load(p)
        self.assertEqual(cfg.profiles['contractor'].tools, ('docs.search',))
        self.assertEqual(cfg.profiles['delegate'].ttl_default, 3600)
        self.assertEqual(cfg.profiles['delegate'].scopes, ('agent.ask', 'jobs.read'))
        self.assertEqual(cfg.limits.job_retention, 172800)
        self.assertEqual(cfg.state_dir, (p.parent / 'data').resolve())
        self.assertEqual(cfg.token_prefix, 'atlas_')

    def test_invalid_configs(self):
        for text in (
            '[nonsense]\n',
            '[profiles.x]\nmode="direct"\nscopes=["agent.consult"]\nttl_default=60\nttl_max=60\nmax_calls_default=1\n',
            '[profiles.x]\nmode="reviewed"\nscopes=["root"]\nttl_default=60\nttl_max=60\nmax_calls_default=1\n',
            '[profiles.delegate]\nttl_default="30d"\n',
            '[profiles.delegate]\ncolour="red"\n',
            '[peerpass]\ntoken_prefix="Bad-"\n',
            '[peerpass]\ndefault_profiles=false\n[profiles.x]\nmode="direct"\n',
        ):
            with self.assertRaises((ValueError, TypeError), msg=text):
                config.load(self.write(text))

    def test_secret_from_env_only(self):
        with patch.dict(os.environ, {'MY_TOKEN': 'abc'}):
            self.assertEqual(config.secret({'token_env': 'MY_TOKEN'}, 'token'), 'abc')
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            config.secret({'token_env': 'MY_TOKEN'}, 'token')


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = Path(self.dir.name) / 'peerpass.toml'
        self.cfg.write_text('[peerpass]\nstate_dir = "state"\nhost_name = "Atlas"\n')

    def tearDown(self):
        self.dir.cleanup()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main([*args])
        return code, out.getvalue(), err.getvalue()

    def test_issue_list_bind_revoke_audit(self):
        c = ['--config', str(self.cfg)]
        code, out, _ = self.run_cli(
            'issue',
            'friend',
            'Fabricated friend agent',
            '--ttl',
            '3d',
            '--calls',
            '9',
            '--bind',
            'telegram:5550001',
            '--json',
            *c,
        )
        self.assertEqual(code, 0)
        g = json.loads(out)
        self.assertEqual((g['max_calls'], g['bindings']), (9, ['telegram:5550001']))
        code, out, _ = self.run_cli('list', *c)
        self.assertIn('Fabricated friend agent', out)
        self.assertIn('telegram:5550001', out)
        self.assertEqual(self.run_cli('bind', g['grant_id'][:6], 'slack:U0FAKE', *c)[0], 0)
        self.assertEqual(self.run_cli('unbind', 'slack:U0FAKE', *c)[0], 0)
        self.assertEqual(self.run_cli('revoke', g['grant_id'][:8], *c)[0], 0)
        code, out, _ = self.run_cli('list', *c)
        self.assertIn('No active passes', out)
        code, out, _ = self.run_cli('list', '--all', *c)
        self.assertIn('revoked', out)
        code, out, _ = self.run_cli('audit', *c)
        self.assertIn('revoked', out)
        code, _, err = self.run_cli('issue', 'nope', 'x', *c)
        self.assertEqual(code, 1)
        self.assertIn('Unknown profile', err)

    def test_human_issue_output_and_card(self):
        code, out, _ = self.run_cli('issue', 'delegate', 'Fabricated coder', '--config', str(self.cfg))
        self.assertIn('Pass (shown once', out)
        code, out, _ = self.run_cli('card', '--config', str(self.cfg))
        self.assertEqual(json.loads(out)['name'], 'Atlas')

    def test_init_writes_loadable_config(self):
        path = Path(self.dir.name) / 'new.toml'
        self.assertEqual(self.run_cli('init', str(path))[0], 0)
        cfg = config.load(path)
        self.assertEqual(cfg.host_name, 'Atlas')
        with self.assertRaises(SystemExit):
            self.run_cli('init', str(path))

    def test_raw_owner_op_reads_stdin(self):
        body = io.StringIO(json.dumps({'profile': 'delegate', 'label': 'scripted'}))
        with patch('sys.stdin', body):
            code, out, _ = self.run_cli('owner', 'issue', '--json-stdin', '--config', str(self.cfg))
        self.assertEqual(json.loads(out)['label'], 'scripted')
