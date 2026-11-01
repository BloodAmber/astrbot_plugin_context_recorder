"""Observe final httpx JSON inference requests without changing their body."""
import asyncio
import functools
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

import httpx

SECRET_KEYS = {'authorization', 'proxy-authorization', 'api_key', 'apikey',
               'api-key', 'x-api-key', 'access_token', 'password', 'secret', 'token'}


def normalize(value, include_media=False):
    if isinstance(value, dict):
        return {key: '[REDACTED]' if key.lower() in SECRET_KEYS
                else normalize(item, include_media) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item, include_media) for item in value]
    if isinstance(value, str) and not include_media and value.startswith('data:'):
        return {'media_omitted': True, 'mime': value.split(';', 1)[0].split(',', 1)[0],
                'characters': len(value), 'sha256': hashlib.sha256(value.encode()).hexdigest()}
    return value


def api_kind(path):
    path = path.rstrip('/')
    if path.endswith('/chat/completions'):
        return 'openai_chat'
    if path.endswith('/responses'):
        return 'openai_responses'
    if path.endswith('/messages'):
        return 'anthropic_messages'
    if re.search(r':(?:streamGenerateContent|generateContent)$', path):
        return 'gemini'
    return None


class HttpCapture:
    def __init__(self, store, include_media=False, hosts=(), on_error=None):
        self.store = store
        self.include_media = include_media
        self.hosts = {str(h).strip().lower() for h in hosts if str(h).strip()}
        self.on_error = on_error or (lambda exc: None)
        self.active = False
        self.patches = []

    def capture(self, request):
        try:
            if not self.active or request.method.upper() != 'POST':
                return
            kind = api_kind(request.url.path)
            if kind is None or (self.hosts and request.url.host.lower() not in self.hosts):
                return
            if 'json' not in request.headers.get('content-type', '').lower():
                return
            # Do not consume request streams. SDK JSON bodies are already buffered.
            body = json.loads(request.content)
            field = {'openai_chat': 'messages', 'openai_responses': 'input',
                     'anthropic_messages': 'messages', 'gemini': 'contents'}[kind]
            if not isinstance(body, dict) or field not in body:
                return
            payload = normalize(body, self.include_media)
            if kind == 'gemini' and not self.include_media:
                self._omit_gemini_media(payload)
            entry = {
                'id': uuid.uuid4().hex, 'time': datetime.now(timezone.utc).isoformat(),
                'stage': 'final_http_request', 'api': kind,
                'host': request.url.host, 'path': request.url.path,
                'model': body.get('model', ''),
                'conversation_id': request.headers.get('x-astrbot-conversation-id'),
                'payload': payload,
            }
            with self.store.lock:
                if self.active:
                    self.store.write(entry)
        except Exception as exc:
            # A recording failure must not prevent the original LLM request.
            self.on_error(exc)

    @classmethod
    def _omit_gemini_media(cls, node):
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if key in ('inlineData', 'inline_data') and isinstance(value, dict) and 'data' in value:
                    raw = str(value['data'])
                    node[key] = {**value, 'data': {'media_omitted': True, 'characters': len(raw),
                                                'sha256': hashlib.sha256(raw.encode()).hexdigest()}}
                else:
                    cls._omit_gemini_media(value)
        elif isinstance(node, list):
            for item in node:
                cls._omit_gemini_media(item)

    def install(self):
        if self.active:
            return
        self.active = True
        original_async = httpx.AsyncClient.send
        original_sync = httpx.Client.send

        @functools.wraps(original_async)
        async def async_send(client, request, *args, **kwargs):
            if self.active:
                try:
                    await asyncio.to_thread(self.capture, request)
                except Exception as exc:
                    self.on_error(exc)
            return await original_async(client, request, *args, **kwargs)

        @functools.wraps(original_sync)
        def sync_send(client, request, *args, **kwargs):
            if self.active:
                self.capture(request)
            return original_sync(client, request, *args, **kwargs)

        self.patches = [(httpx.AsyncClient, original_async, async_send),
                        (httpx.Client, original_sync, sync_send)]
        httpx.AsyncClient.send = async_send
        httpx.Client.send = sync_send

    def uninstall(self):
        self.active = False
        for cls, original, wrapper in self.patches:
            # Leave wrappers installed by other plugins alone. Our wrapper becomes passive.
            if cls.send is wrapper:
                cls.send = original
        # Wait for any write that has already entered its critical section.
        with self.store.lock:
            pass
