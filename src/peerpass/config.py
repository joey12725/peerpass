"""Configuration: profiles, limits, host adapter and transport settings.

Everything has a default, so ``peerpass serve`` works with no config file at all.
A TOML file overrides any part of it; see ``examples/peerpass.toml``.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Capabilities a profile can grant. A pass carries exactly its profile's scopes.
SCOPES = frozenset(
    {
        'agent.ask',  # direct conversation with the host agent
        'agent.consult',  # reviewed consultation: private reasoning, minimal released answer
        'jobs.read',  # read results of this pass's own jobs
        'context.read',  # owner-published context for this pass
        'availability.read',  # owner-published time offers for this pass
        'proposal.create',  # drop a proposal into the owner's inbox
        'proposal.read',  # read the status of this pass's own proposals
    }
)

MODES = ('direct', 'reviewed')

_UNITS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}


def duration(value: Any) -> int:
    """Parse ``3600``, ``"90m"``, ``"12h"`` or ``"30d"`` into seconds."""
    if isinstance(value, bool):
        raise ValueError('duration must be a number or a string like "12h"')
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        m = re.fullmatch(r'\s*(\d+)\s*([smhdw]?)\s*', value)
        if m:
            return int(m.group(1)) * _UNITS[m.group(2) or 's']
    raise ValueError(f'invalid duration: {value!r}')


@dataclass(frozen=True)
class Profile:
    name: str
    mode: str
    scopes: tuple[str, ...]
    ttl_default: int
    ttl_max: int
    max_calls_default: int
    rate_per_minute: int = 6
    tools: tuple[str, ...] = ()
    audience: str = ''
    description: str = ''

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f'profile {self.name}: mode must be one of {MODES}')
        unknown = set(self.scopes) - SCOPES
        if unknown:
            raise ValueError(f'profile {self.name}: unknown scopes {sorted(unknown)}')
        if self.mode == 'direct' and 'agent.consult' in self.scopes:
            raise ValueError(f'profile {self.name}: direct profiles use agent.ask, not agent.consult')
        if self.mode == 'reviewed' and 'agent.ask' in self.scopes:
            raise ValueError(f'profile {self.name}: reviewed profiles use agent.consult, not agent.ask')
        if not 60 <= self.ttl_default <= self.ttl_max:
            raise ValueError(f'profile {self.name}: need 60 <= ttl_default <= ttl_max')
        if not 1 <= self.max_calls_default <= 10000 or not 1 <= self.rate_per_minute <= 600:
            raise ValueError(f'profile {self.name}: invalid call budget or rate')

    @property
    def can_message(self) -> bool:
        return bool({'agent.ask', 'agent.consult'} & set(self.scopes))

    def public(self) -> dict:
        return {
            'name': self.name,
            'mode': self.mode,
            'scopes': list(self.scopes),
            'ttl_default': self.ttl_default,
            'ttl_max': self.ttl_max,
            'max_calls_default': self.max_calls_default,
            'description': self.description,
        }


DEFAULT_PROFILES = {
    'delegate': Profile(
        name='delegate',
        mode='direct',
        scopes=('agent.ask', 'jobs.read'),
        ttl_default=12 * 3600,
        ttl_max=12 * 3600,
        max_calls_default=40,
        description='Short-lived, broad pass for your own coding or task agents. '
        'Messages go straight to the host agent with its normal tools.',
    ),
    'colleague': Profile(
        name='colleague',
        mode='reviewed',
        scopes=('agent.consult', 'jobs.read', 'context.read', 'availability.read', 'proposal.create', 'proposal.read'),
        ttl_default=30 * 86400,
        ttl_max=90 * 86400,
        max_calls_default=100,
        audience='A work contact. Assume every answer can be read by their whole team.',
        description='Reviewed consultations for a coworker or team agent, plus published '
        'work context, time offers and proposals.',
    ),
    'friend': Profile(
        name='friend',
        mode='reviewed',
        scopes=('agent.consult', 'jobs.read', 'availability.read', 'proposal.create', 'proposal.read'),
        ttl_default=30 * 86400,
        ttl_max=90 * 86400,
        max_calls_default=100,
        audience='A friend. Consider the named relationship and purpose; friendship is not blanket consent.',
        description="Reviewed consultations for a friend's agent, plus time offers and proposals.",
    ),
}


@dataclass(frozen=True)
class Limits:
    global_concurrency: int = 4
    reviewed_concurrency: int = 1
    max_message: int = 16000
    job_retention: int = 86400
    history_window: int = 20
    history_retention: int = 7 * 86400
    proposal_retention: int = 7 * 86400
    audit_retention: int = 30 * 86400
    max_bindings: int = 5


@dataclass
class Config:
    host_name: str = 'the host agent'
    owner_name: str = 'the owner'
    public_url: str = 'http://127.0.0.1:8750'
    listen: str = '127.0.0.1:8750'
    state_dir: Path = Path('./peerpass-data')
    token_prefix: str = 'pp_'
    timezone: str = 'UTC'
    agent_card: bool = True
    profiles: dict[str, Profile] = field(default_factory=lambda: dict(DEFAULT_PROFILES))
    limits: Limits = field(default_factory=Limits)
    host: dict[str, Any] = field(default_factory=lambda: {'adapter': 'echo'})
    review_host: dict[str, Any] | None = None
    transports: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.state_dir / 'peerpass.sqlite3'

    @property
    def owner_socket(self) -> Path:
        return self.state_dir / 'owner.sock'


def secret(settings: dict, key: str, required: bool = True) -> str:
    """Read a secret from the environment variable named by ``settings[key + '_env']``.

    Secrets never live in the TOML file itself, so the file can be committed.
    """
    env = settings.get(key + '_env')
    value = os.environ.get(env, '') if env else ''
    if required and not value:
        raise ValueError(f'set the environment variable named by {key}_env ({env or "unset"})')
    return value


def _profile(name: str, raw: dict, base: Profile | None) -> Profile:
    allowed = {
        'mode',
        'scopes',
        'ttl_default',
        'ttl_max',
        'max_calls_default',
        'rate_per_minute',
        'tools',
        'audience',
        'description',
    }
    extra = set(raw) - allowed
    if extra:
        raise ValueError(f'profile {name}: unknown keys {sorted(extra)}')
    merged = (
        base.__dict__.copy() if base else {'name': name, 'tools': (), 'audience': '', 'description': '', 'rate_per_minute': 6}
    )
    for key, value in raw.items():
        if key in ('ttl_default', 'ttl_max'):
            value = duration(value)
        elif key in ('scopes', 'tools'):
            value = tuple(value)
        merged[key] = value
    merged['name'] = name
    return Profile(**merged)


def load(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    if path is None:
        env = os.environ.get('PEERPASS_CONFIG')
        path = env if env else ('peerpass.toml' if Path('peerpass.toml').is_file() else None)
    if path is None:
        _env_overrides(cfg)
        return cfg
    raw = tomllib.loads(Path(path).read_text())
    known = {'peerpass', 'profiles', 'limits', 'host', 'review_host', 'transports'}
    extra = set(raw) - known
    if extra:
        raise ValueError(f'unknown config sections: {sorted(extra)}')
    top = raw.get('peerpass', {})
    for key in ('host_name', 'owner_name', 'public_url', 'listen', 'token_prefix', 'timezone', 'agent_card'):
        if key in top:
            setattr(cfg, key, top[key])
    if 'state_dir' in top:
        cfg.state_dir = (Path(path).parent / top['state_dir']).resolve()
    if not re.fullmatch(r'[a-z]{1,8}_', cfg.token_prefix):
        raise ValueError('token_prefix must be 1-8 lowercase letters followed by "_"')
    profiles = dict(DEFAULT_PROFILES) if top.get('default_profiles', True) else {}
    for name, body in raw.get('profiles', {}).items():
        profiles[name] = _profile(name, body, profiles.get(name))
    cfg.profiles = profiles
    if 'limits' in raw:
        lim = dict(raw['limits'])
        for key in ('job_retention', 'history_retention', 'proposal_retention', 'audit_retention'):
            if key in lim:
                lim[key] = duration(lim[key])
        cfg.limits = Limits(**lim)
    if 'host' in raw:
        cfg.host = raw['host']
    cfg.review_host = raw.get('review_host')
    cfg.transports = raw.get('transports', {})
    _env_overrides(cfg)
    return cfg


def _env_overrides(cfg: Config) -> None:
    if os.environ.get('PEERPASS_STATE_DIR'):
        cfg.state_dir = Path(os.environ['PEERPASS_STATE_DIR'])
    if os.environ.get('PEERPASS_LISTEN'):
        cfg.listen = os.environ['PEERPASS_LISTEN']
    if os.environ.get('PEERPASS_PUBLIC_URL'):
        cfg.public_url = os.environ['PEERPASS_PUBLIC_URL']
