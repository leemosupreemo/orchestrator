"""Machine-owned public connections. Provider configuration never contains runner credentials."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, quote
from urllib.request import urlopen

from orchestrator.project_config import user_state_dir
from orchestrator.account import _ssl_context

PROVIDERS = ('cloudflare', 'funnel', 'external')


class ConnectionStopped(RuntimeError):
    pass


def endpoint_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ('', '/') or parsed.port == 0):
            return ''
        host = parsed.hostname.lower()
        if ':' in host:
            host = '[' + host + ']'
        return 'https://' + host + (':' + str(parsed.port) if parsed.port not in (None, 443) else '')
    except (ValueError, TypeError):
        return ''


def setup_url(text):
    for match in re.findall(r'https://[^\s<>\"\']+', text or ''):
        value = match.rstrip('.,)')
        if re.fullmatch(r'https://login\.tailscale\.com/f/funnel\?node=[A-Za-z0-9_-]{1,200}', value, flags=re.ASCII):
            return value
    return ''


class ConnectionStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else user_state_dir() / 'connection.json'
        self.lock = threading.RLock()
        self.data = {'version': 1, 'selected': '', 'providers': {}, 'ack_id': '', 'pending_setup': False, 'setup_url': ''}
        try:
            data = json.loads(self.path.read_text())
            if data.get('version') == 1 and isinstance(data.get('providers'), dict):
                self.data.update(data)
                if 'tailscale' in self.data['providers']:
                    self.data['providers'].setdefault('funnel', self.data['providers'].pop('tailscale'))
                if self.data['selected'] == 'tailscale':
                    self.data['selected'] = 'funnel'
                self.save()
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.parent.chmod(0o700)
            temporary = self.path.with_name(self.path.name + '.' + secrets.token_hex(6))
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as out:
                    json.dump(self.data, out)
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def configure(self, provider, url='', enabled=True, select=True, token=None):
        if provider not in PROVIDERS:
            raise ValueError('Unknown connection provider')
        if url and not endpoint_url(url):
            raise ValueError('A public HTTPS origin is required')
        with self.lock:
            options = dict(self.data['providers'].get(provider, {}))
            options.update(enabled=bool(enabled), url=endpoint_url(url))
            if token is not None and provider == 'cloudflare':
                options['token'] = str(token)
            self.data['providers'][provider] = options
            if select:
                self.data['selected'] = provider
            self.save()

    def adopt(self, url):
        url = endpoint_url(url)
        if url:
            self.configure('funnel' if urlsplit(url).hostname.endswith('.ts.net') else 'external', url)


class PublicHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a validated public address while retaining hostname TLS validation/SNI."""
    def __init__(self, host, *, address, **kwargs):
        self.address = address
        super().__init__(host, **kwargs)

    def connect(self):
        sock = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def public_addresses(host, public_dns=False):
    if not public_dns:
        return list({entry[4][0] for entry in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
    # The machine's resolver can return private MagicDNS routes. Funnel must be
    # resolved outside the tailnet, then TLS connects directly to the relay IP.
    addresses = []
    for kind in ('A', 'AAAA'):
        with urlopen('https://dns.google/resolve?name=' + quote(host, safe='') + '&type=' + kind, timeout=5, context=_ssl_context()) as response:
            data = json.loads(response.read(65536))
        addresses.extend(item['data'] for item in data.get('Answer', []) if item.get('type') in (1, 28))
    return addresses


def public_probe(url, secret, *, public_dns=False, resolver=public_addresses):
    origin = endpoint_url(url)
    if not origin:
        return False
    connection = None
    try:
        host = urlsplit(origin).hostname
        addresses = resolver(host, public_dns)
        if not addresses or not all(ipaddress.ip_address(a).is_global for a in addresses):
            return False
        nonce = secrets.token_hex(16)
        expected = hmac.new(secret.encode(), ('connection-health:' + nonce).encode(), hashlib.sha256).hexdigest()
        connection = PublicHTTPSConnection(host, port=urlsplit(origin).port or 443, address=addresses[0], timeout=8, context=_ssl_context())
        connection.request('GET', '/api/connection-health?nonce=' + nonce, headers={'Accept': 'application/json'})
        response = connection.getresponse()
        if response.status != 200:  # redirects are deliberately never followed
            return False
        value = json.loads(response.read(4096))
        return value.get('nonce') == nonce and hmac.compare_digest(str(value.get('proof', '')), expected)
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        return False
    finally:
        if connection:
            connection.close()


class ConnectionManager:
    def __init__(self, port, secret, cloudflare_start=None, notify=None, prepare_endpoint=None,
                 initial_url='', store=None, probe=public_probe, clock=time.time, native_runner=subprocess.run, cloudflare_stop=None):
        self.port, self.secret = port, secret
        self.store = store or ConnectionStore()
        if initial_url:
            self.store.adopt(initial_url)
        if not self.store.data['providers'] and cloudflare_start:
            self.store.configure('cloudflare')
        self.cloudflare_stop = cloudflare_stop
        self.cloudflare_start = cloudflare_start
        self.notify = notify or (lambda: None)
        self.prepare_endpoint = prepare_endpoint or (lambda url: None)
        self.probe, self.clock, self.native_runner = probe, clock, native_runner
        self._endpoint = ''
        self._status = {'state': 'checking', 'provider': self.store.data['selected'], 'endpoint': '',
                        'detail': 'Checking public connection.', 'checked_at': 0, 'setup_url': setup_url(self.store.data.get('setup_url', ''))}
        self._flight = threading.Lock()
        self._lock = threading.RLock()
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread = None
        self._process = None
        self._native_process = None
        self._cleanup_thread = None
        self._action = self.store.data.get('pending_action', '')
        self._attempts = 0
        self._retry_after = 0
        self._next = 0

    @property
    def endpoint(self):
        with self._lock:
            return self._endpoint

    @property
    def ack_id(self):
        return self.store.data.get('ack_id', '')

    def status(self):
        with self._lock:
            return dict(self._status)

    def _publish(self, state, provider='', detail='', **extra):
        with self._lock:
            if self._stop.is_set():
                return
            self._status.update(state=state, provider=provider, endpoint=self._endpoint,
                                detail=detail, checked_at=self.clock(), **extra)
        self.notify()

    def request(self, command_id, action, expires=None):
        if not isinstance(command_id, str) or not command_id or len(command_id) > 160 or action not in ('repair', 'setup_funnel'):
            return False
        if expires is None:
            expires = self.clock() + 900
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not self.clock() < expires <= self.clock() + 900:
            return False
        with self._lock:
            seen = self.store.data.get('accepted_commands', [])
            if command_id in seen or command_id == self.ack_id:
                return False
            previous = json.loads(json.dumps(self.store.data))
            self.store.data.update(ack_id=command_id, accepted_commands=(seen + [command_id])[-64:], pending_action=action, action_expires=expires)
            try:
                self.store.save()
            except OSError:
                self.store.data = previous
                raise
            self._action = action
            self._attempts, self._next = 0, 0
        self._wake.set()
        self.notify()
        return True

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='connection-manager', daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            if self.clock() >= self._next:
                self.check_once()
            self._wake.wait(2)
            self._wake.clear()

    def _cleanup(self):
        with self._lock:
            process, self._process = self._process, None
        if process:
            if self.cloudflare_stop:
                self.cloudflare_stop(process)
                return
            try:
                process.terminate()
                if hasattr(process, 'wait'):
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _cleanup_native(self):
        with self._lock:
            process = self._native_process
        if process and process.poll() is None:
            try:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def stop(self, wait=False):
        with self._lock:
            self._stop.set()
            if not self._cleanup_thread or not self._cleanup_thread.is_alive():
                def cleanup():
                    self._cleanup_native()
                    self._cleanup()
                self._cleanup_thread = threading.Thread(target=cleanup, name='connection-cleanup', daemon=True)
                self._cleanup_thread.start()
        self._wake.set()
        if wait:
            deadline = time.monotonic() + 5
            for thread in (self._thread, self._cleanup_thread):
                if thread and thread is not threading.current_thread():
                    thread.join(timeout=max(0, deadline - time.monotonic()))
            # A cancelled discovery may have returned its child during the join.
            self._cleanup_native()
            self._cleanup()

    def _ensure_running(self):
        if self._stop.is_set():
            raise ConnectionStopped()

    def _native(self, args):
        self._ensure_running()
        argv = ['tailscale'] + args
        if self.native_runner is not subprocess.run:
            result = self.native_runner(argv, capture_output=True, text=True, timeout=12)
            self._ensure_running()
            return result
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        with self._lock:
            self._native_process = process
        try:
            if self._stop.is_set():
                self._cleanup_native()
                raise ConnectionStopped()
            try:
                stdout, stderr = process.communicate(timeout=12)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                stdout, stderr = process.communicate()
                self._ensure_running()
                raise subprocess.TimeoutExpired(argv, 12, output=stdout, stderr=stderr) from exc
            self._ensure_running()
            return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        finally:
            with self._lock:
                if self._native_process is process:
                    self._native_process = None

    def _funnel(self, setup=False):
        result = self._native(['serve', 'status', '--json'])
        if result.returncode:
            raise RuntimeError('Tailscale is unavailable. Sign in to Tailscale on this computer.')
        config = json.loads(result.stdout or '{}')
        # Never replace an unrelated root or path handler, or TCP listener.
        for address, web in config.get('Web', {}).items():
            if address.endswith(':443'):
                handlers = web.get('Handlers', {})
                expected = {'Proxy': f'http://127.0.0.1:{self.port}'}
                if handlers and handlers != {'/': expected}:
                    raise RuntimeError('Tailscale port 443 belongs to another service. Its configuration was preserved.')
        tcp = config.get('TCP', {}).get('443', {})
        if tcp and not tcp.get('HTTPS'):
            raise RuntimeError('Tailscale port 443 belongs to another service. Its configuration was preserved.')
        node_result = self._native(['status', '--json'])
        if node_result.returncode:
            raise RuntimeError('Sign in to Tailscale on this computer.')
        node = json.loads(node_result.stdout or '{}').get('Self', {})
        hostname = node.get('DNSName', '').rstrip('.')
        if not hostname.endswith('.ts.net'):
            raise RuntimeError('Tailscale HTTPS hostname is unavailable.')
        capabilities = str(node.get('CapMap', {})) + str(node.get('Capabilities', []))
        # A setup request may obtain an owner URL, but pending polling only changes
        # routes after the owner has granted Funnel capability.
        if setup or 'funnel' in capabilities:
            try:
                result = self._native(['funnel', '--bg', '--https=443', f'http://127.0.0.1:{self.port}'])
                output = (result.stdout or '') + '\n' + (result.stderr or '')
            except subprocess.TimeoutExpired as exc:
                # subprocess.run kills and reaps the waiting CLI, leaving no prompt.
                output = (exc.stdout or b'')
                if isinstance(output, bytes):
                    output = output.decode(errors='replace')
                error = exc.stderr or ''
                output += error.decode(errors='replace') if isinstance(error, bytes) else error
            self._ensure_running()
            link = setup_url(output)
            if link:
                self.store.data.update(pending_setup=True, setup_url=link)
                self.store.save()
                self._publish('needs_setup', 'funnel', 'Enable Funnel with your Tailscale account.', setup_url=link)
                return ''
        return 'https://' + hostname

    def _verify(self, url, provider):
        if not endpoint_url(url):
            return False
        self._ensure_running()
        self.prepare_endpoint(url)
        self._ensure_running()
        verified = self.probe(url, self.secret, public_dns=provider == 'funnel')
        self._ensure_running()
        return verified

    def check_once(self):
        if not self._flight.acquire(blocking=False):
            return False
        try:
            self._ensure_running()
            action, self._action = self._action, ''
            if action and self.store.data.get('action_expires', 0) <= self.clock():
                action = ''
                self.store.data['pending_action'] = ''
                self.store.save()
            action_id = self.ack_id
            if action == 'setup_funnel':
                self.store.data['pending_setup'] = True
                if 'funnel' not in self.store.data['providers']:
                    self.store.configure('funnel', select=False)
                self.store.save()
            if self._attempts >= 3 and self.clock() >= self._retry_after:
                self._attempts = 0
            providers = self.store.data['providers']
            selected = self.store.data['selected']
            order = ([selected] if selected in providers else []) + [p for p in providers if p != selected]
            if action == 'setup_funnel' or self.store.data.get('pending_setup'):
                order = ['funnel'] + [p for p in order if p != 'funnel']
            if not self._endpoint or action:
                self._publish('checking', selected, 'Checking public connection.')
            pending = False
            for provider in order:
                self._ensure_running()
                options = providers.get(provider, {})
                if not options.get('enabled'):
                    continue
                url = self._endpoint if provider == selected and self._endpoint else options.get('url', '')
                verified = False
                try:
                    force = action == 'repair' and provider == 'cloudflare'
                    explicit_setup = action == 'setup_funnel' and provider == 'funnel'
                    if url and not force and not explicit_setup:
                        verified = self._verify(url, provider)
                    may_repair = self._attempts < 3 or bool(action) or (provider == 'funnel' and self.store.data.get('pending_setup'))
                    if not verified and may_repair:
                        self._publish('repairing', provider, 'Repairing public connection.')
                        if provider == 'cloudflare' and self.cloudflare_start:
                            self._cleanup()
                            self._ensure_running()
                            process, url = self.cloudflare_start(self.port)
                            with self._lock:
                                self._process = process
                            if self._stop.is_set():
                                self._cleanup()
                                return False
                            verified = bool(url) and self._verify(url, provider)
                        elif provider == 'funnel':
                            url = self._funnel(setup=explicit_setup)
                            verified = bool(url) and self._verify(url, provider)
                    if verified:
                        with self._lock:
                            self._ensure_running()
                            self._endpoint = url
                            self.store.configure(provider, url)
                            if provider == 'funnel':
                                self.store.data.update(pending_setup=False, setup_url='')
                                self.store.save()
                            self._attempts = 0
                            self._next = self.clock() + 30
                        self._publish('connected', provider, 'Public connection verified.', setup_url='')
                        return True
                    if provider == 'cloudflare':
                        self._cleanup()
                    pending = pending or bool(self.store.data.get('pending_setup'))
                except ConnectionStopped:
                    self._cleanup()
                    return False
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    if provider == 'cloudflare':
                        self._cleanup()
                    pending = pending or bool(self.store.data.get('pending_setup'))
            self._ensure_running()
            self._attempts += 1
            if self._attempts == 3:
                self._retry_after = self.clock() + 300
            self._next = self.clock() + min(60, 10 * 2 ** min(self._attempts - 1, 3))
            if pending:
                self._publish('needs_setup', 'funnel', 'Enable Funnel with your Tailscale account.', setup_url=setup_url(self.store.data.get('setup_url', '')))
            else:
                self._publish('failed', selected, 'Public connection unavailable. Check this computer’s network or try repair.')
            return False
        except ConnectionStopped:
            return False
        finally:
            if not self._stop.is_set() and 'action_id' in locals() and action:
                with self._lock:
                    if self.ack_id == action_id:
                        self.store.data['pending_action'] = ''
                        self.store.save()
            self._flight.release()
