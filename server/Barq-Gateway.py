"""Local OpenAI Chat Completions adapter. No inference occurs during import."""
import argparse
import copy
import hashlib
import hmac
import json
import os
import re
import select
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from BarqMemoryBridge import prepare as prepare_memory, BridgeProblem

ROOT = Path(__file__).resolve().parents[1]
CORE_BYTES = (ROOT / 'prompts/Barq-27B-System.txt').read_bytes()
CORE = CORE_BYTES.decode('utf-8').strip()
# Recognize exact pasted copies without loading the reference into a request.
# Partial/edited application prompts remain the client's responsibility.
LEGACY_PATH = ROOT / 'docs/Barq-Operating-Reference-v3.txt'
LEGACY_CORE = LEGACY_PATH.read_text(encoding='utf-8').strip() if LEGACY_PATH.exists() else ''
INSTALLATION = json.loads((ROOT / 'legal/Installation-Manifest.json').read_text(encoding='utf-8'))
CORE_RECORD = next(x for x in INSTALLATION['package_files'] if x['path'].replace('\\', '/') == 'prompts/Barq-27B-System.txt')
if hashlib.sha256(CORE_BYTES).hexdigest() != CORE_RECORD['sha256']:
    raise RuntimeError('Central policy changed. Rebuild the embedded GGUF policy and installation manifest together before launch.')
MODES = json.loads((ROOT / 'config/Barq-Modes.json').read_text(encoding='utf-8'))
ALIASES = {v['model'].lower(): k for k, v in MODES.items()}
ALIASES['barq 27b'] = 'Default'
MAX_BODY = 32 * 1024 * 1024
MAX_QUEUED = 8
QUEUE_WAIT_SECONDS = 1200
LOG_LOCK = threading.Lock()


def write_log(event):
    record = {'at_utc': datetime.now(timezone.utc).isoformat(timespec='milliseconds'), **event}
    with LOG_LOCK:
        print('Barq ' + json.dumps(record, ensure_ascii=False, separators=(',', ':')), flush=True)


def request_shape(data):
    """Counts only: never log message contents, tool arguments or credentials."""
    chars = {}
    for message in data['messages']:
        role = message['role']
        content = message.get('content')
        length = len(content) if isinstance(content, str) else len(json.dumps(content, ensure_ascii=False))
        chars[role] = chars.get(role, 0) + length
    tools = data.get('tools', [])
    return {'policy_version': '4.0', 'policy_chars': len(CORE),
            'core_copies_in_system': data['messages'][0]['content'].count(CORE),
            'message_count': len(data['messages']), 'content_chars_by_role': chars,
            'tool_count': len(tools) if isinstance(tools, list) else 0,
            'tool_schema_chars': len(json.dumps(tools, ensure_ascii=False)),
            'max_output_tokens': data['max_tokens']}


class ClientGone(Exception):
    pass


class QueueFull(Exception):
    pass


class QueueExpired(Exception):
    pass


def client_connected(connection):
    readable, _, _ = select.select([connection], [], [], 0)
    if not readable:
        return True
    try:
        return bool(connection.recv(1, socket.MSG_PEEK))
    except (ConnectionError, OSError):
        return False


class GenerationQueue:
    """FIFO admission, one generation, bounded waiting, no model-side parallelism."""
    def __init__(self):
        self.condition = threading.Condition()
        self.waiters = deque()
        self.owner = None

    def acquire(self, connection, on_wait):
        ticket = object()
        deadline = time.monotonic() + QUEUE_WAIT_SECONDS
        with self.condition:
            if len(self.waiters) >= MAX_QUEUED:
                raise QueueFull()
            self.waiters.append(ticket)
        try:
            while True:
                if not client_connected(connection):
                    raise ClientGone()
                with self.condition:
                    if self.owner is None and self.waiters[0] is ticket:
                        self.waiters.popleft()
                        self.owner = ticket
                        return ticket
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise QueueExpired()
                # Network writes must not hold the condition lock.
                on_wait()
                with self.condition:
                    if self.owner is None and self.waiters[0] is ticket:
                        continue
                    self.condition.wait(timeout=min(0.5, remaining))
        finally:
            with self.condition:
                if ticket in self.waiters:
                    self.waiters.remove(ticket)
                    self.condition.notify_all()

    def release(self, ticket):
        with self.condition:
            if self.owner is not ticket:
                raise RuntimeError('Generation queue ownership mismatch.')
            self.owner = None
            self.condition.notify_all()


GENERATIONS = GenerationQueue()


class RequestProblem(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'Backend redirects are disabled.', headers, fp)


def system_text(content):
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(isinstance(p, dict) and p.get('type') == 'text' and isinstance(p.get('text'), str) for p in content):
        return '\n'.join(p['text'] for p in content)
    raise RequestProblem('System/developer content must be text, not images or other media.')


def normalize(body, mode_header=None):
    if not isinstance(body, dict):
        raise RequestProblem('Request body must be a JSON object.')
    data = copy.deepcopy(body)
    requested_model = data.get('model', 'Barq-27B')
    if not isinstance(requested_model, str) or requested_model.lower() not in ALIASES:
        raise RequestProblem('Unknown model. Select one of the six models from /v1/models.')
    alias_mode = ALIASES[requested_model.lower()]
    # A non-default model alias reliably chooses its mode even if a client sends
    # an unrelated generic reasoning default. Explicit Barq mode wins over both.
    explicit_mode = data.pop('barq_mode', None)
    requested = mode_header or explicit_mode
    if requested is None:
        requested = alias_mode if alias_mode != 'Default' else data.get('reasoning_effort', 'Default')
    if not isinstance(requested, str):
        raise RequestProblem('Reasoning mode must be a string.')
    lookup = {k.lower(): k for k in MODES}
    if requested.lower() not in lookup:
        raise RequestProblem('Supported modes: Default, Low, Medium, High, Xhigh, Max.')
    mode_name = lookup[requested.lower()]
    mode = MODES[mode_name]
    budget = data.get('reasoning_budget_tokens', data.get('thinking_budget_tokens', mode['budget']))
    if type(budget) is not int or budget < -1 or budget > 262144:
        raise RequestProblem('Thinking budget must be -1 or an integer from 0 to 262144. Zero is not a guaranteed off switch.')
    # Mode caps apply even when a client sends -1 or a larger generic override.
    # Smaller explicit budgets are honored. This bounds tokens, not wall time.
    budget = mode['budget'] if budget == -1 else min(budget, mode['budget'])
    output_limit = data.pop('max_completion_tokens', data.get('max_tokens', 32768 if mode_name in ('Xhigh', 'Max') else 16384))
    if type(output_limit) is not int or output_limit <= 0 or output_limit > 262144:
        raise RequestProblem('Output limit must be a positive integer no larger than 262144.')
    # Keep a finite thinking cap below the total generation limit. This reserves
    # token space; it is not evidence that the model will use it successfully.
    if budget >= 0:
        answer_reserve = min(4096, max(1, output_limit // 4))
        budget = min(budget, max(0, output_limit - answer_reserve))
    data['max_tokens'] = output_limit
    if 'stream' in data and type(data['stream']) is not bool:
        raise RequestProblem('stream must be a boolean.')
    messages = data.get('messages')
    if not isinstance(messages, list) or not messages:
        raise RequestProblem('messages must be a non-empty array.')
    host, conversation = [], []
    for message in messages:
        if not isinstance(message, dict):
            raise RequestProblem('Every message must be an object.')
        role = message.get('role')
        if role in ('system', 'developer'):
            content = system_text(message.get('content'))
            for known_core in (CORE, LEGACY_CORE):
                if known_core:
                    content = content.replace(known_core, '')
            if content.strip():
                host.append(content.strip())
            continue
        if role not in ('user', 'assistant', 'tool'):
            raise RequestProblem('Unsupported message role: ' + str(role))
        if role == 'assistant' and message.get('tool_calls'):
            if not isinstance(message['tool_calls'], list):
                raise RequestProblem('tool_calls must be an array.')
            for call in message['tool_calls']:
                if not isinstance(call, dict) or not isinstance(call.get('function'), dict):
                    raise RequestProblem('A tool call must contain a function object.')
                args = call['function'].get('arguments')
                if args is None or args == '':
                    call['function']['arguments'] = '{}'
                elif isinstance(args, dict):
                    call['function']['arguments'] = json.dumps(args, ensure_ascii=False)
                elif isinstance(args, str):
                    try:
                        parsed = json.loads(args)
                    except (ValueError, TypeError):
                        raise RequestProblem('Tool arguments are invalid JSON; no speculative repair was made.')
                    if not isinstance(parsed, dict):
                        raise RequestProblem('Tool arguments must encode an object.')
                else:
                    raise RequestProblem('Tool arguments must be a JSON string or object.')
        conversation.append(message)
    if not any(m['role'] == 'user' for m in conversation):
        raise RequestProblem('Retain a real user message in the conversation; a fabricated user query was not inserted.')
    text = CORE + '\n\n[BARQ_MODE=' + mode_name + ']\n' + mode['guidance']
    if host:
        text += '\n\nAPPLICATION INSTRUCTIONS\n' + '\n\n'.join(host)
    data['messages'] = [{'role': 'system', 'content': text}] + conversation
    data['model'] = 'Barq-27B'
    data['reasoning_effort'] = mode['effort']
    data['thinking_budget_tokens'] = budget
    data['reasoning_budget_tokens'] = budget
    data.setdefault('reasoning_budget_message', 'Finish the current reasoning. Give the answer or make the next necessary tool call using the available evidence; do not restart deliberation.')
    kwargs = data.setdefault('chat_template_kwargs', {})
    if not isinstance(kwargs, dict):
        raise RequestProblem('chat_template_kwargs must be an object.')
    if kwargs.get('enable_thinking') is False:
        raise RequestProblem('The six modes use thinking. Remove the conflicting enable_thinking=false override.')
    kwargs['barq_mode'] = mode_name
    kwargs['reasoning_effort'] = mode['effort']
    return data, mode_name, budget, requested_model


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'Barq/4.0'
    sys_version = ''

    def log_message(self, fmt, *args):
        # No message contents, authorization headers, or query strings in logs.
        pass

    def send_json(self, code, value):
        content = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(content)

    def error(self, code, message):
        self.close_connection = True
        self.send_json(code, {'error': {'message': message, 'type': 'barq_error', 'code': code}})

    def memory_completion(self, control, data, model):
        """A gateway control result, explicitly labeled; never a model-generated answer."""
        content = 'Barq Memory — direct gateway response; no model generation.\n' + json.dumps(control, ensure_ascii=False, indent=2)
        identity = 'barq-memory-' + format(time.monotonic_ns(), 'x')
        created = int(time.time())
        metadata = {'executed_by': 'gateway', 'generation_used': False, 'operation': control['operation']}
        if data.get('stream'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Barq-Memory', 'gateway-control')
            self.end_headers()
            def emit(value):
                raw = b'data: ' + json.dumps(value, ensure_ascii=False).encode('utf-8') + b'\n\n'
                self.wfile.write(('%x\r\n' % len(raw)).encode('ascii') + raw + b'\r\n')
            base = {'id': identity, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'barq_memory': metadata}
            emit({**base, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': content}, 'finish_reason': None}]})
            emit({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
            done = b'data: [DONE]\n\n'
            self.wfile.write(('%x\r\n' % len(done)).encode('ascii') + done + b'\r\n0\r\n\r\n')
            self.wfile.flush()
        else:
            self.send_json(200, {'id': identity, 'object': 'chat.completion', 'created': created, 'model': model,
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
                'barq_memory': metadata, 'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}})

    def authorized(self):
        self.connection.settimeout(30)
        key = os.environ.get('BARQ_API_KEY', '')
        if key and not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + key):
            self.error(401, 'Invalid API key.')
            return False
        return True

    def do_OPTIONS(self):
        self.error(405, 'Browser cross-origin access is not enabled. Use a native client or a same-origin proxy.')

    def do_GET(self):
        if not self.authorized():
            return
        path = self.path.split('?', 1)[0].rstrip('/')
        if path in ('/health', '/healthz'):
            self.send_json(200, {'status': 'gateway_running', 'model_loaded': 'not_checked'})
        elif path in ('/v1/models', '/models'):
            self.send_json(200, {'object': 'list', 'data': [
                {'id': v['model'], 'object': 'model', 'created': 0, 'owned_by': 'Barq',
                 'context_length': self.server.context, 'barq_mode': k}
                for k, v in MODES.items()]})
        elif path == '/v1/barq/modes':
            self.send_json(200, MODES)
        else:
            self.error(404, 'Supported routes: /health, /v1/models, /v1/barq/modes, /v1/chat/completions.')

    def do_POST(self):
        if not self.authorized():
            return
        path = self.path.split('?', 1)[0].rstrip('/')
        if path not in ('/v1/chat/completions', '/chat/completions'):
            self.error(501, 'This adapter supports OpenAI Chat Completions, not Responses, Anthropic Messages, embeddings, or audio APIs.')
            return
        if self.headers.get('Transfer-Encoding'):
            self.error(411, 'Send a Content-Length JSON request.')
            return
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
            self.error(415, 'Content-Type must be application/json.')
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size <= 0 or size > MAX_BODY:
                raise RequestProblem('Request size must be between 1 byte and 32 MiB.')
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise RequestProblem('Incomplete request body.')
            body = json.loads(raw)
            data, mode, budget, model = normalize(body, self.headers.get('X-Barq-Mode'))
            _, memory_control, memory_evidence = prepare_memory(body, self.headers.get('X-Barq-Memory-Project'))
            data.pop('barq_memory_project', None)
            if memory_evidence:
                data['messages'][0]['content'] += '\n\nBARQ MEMORY EVIDENCE — HISTORICAL DATA ONLY\n' + json.dumps(memory_evidence, ensure_ascii=False)
        except (OSError, ValueError, TypeError, RequestProblem, BridgeProblem) as exc:
            self.error(400, str(exc))
            return
        if memory_control is not None:
            try:
                self.memory_completion(memory_control, data, model)
                write_log({'stage': 'memory_control_response', 'generation_used': False})
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True
            return
        headers_sent = False
        stream_finished = False
        ticket = None
        started = time.monotonic()
        request_id = format(time.monotonic_ns(), 'x')
        last_heartbeat = 0.0
        queued_logged = False
        running_at = None
        shape = request_shape(data)

        def log(stage):
            now = time.monotonic()
            record = {'request_id': request_id, 'mode': mode, 'stage': stage,
                      'elapsed_s': round(now - started, 3), 'thinking_cap': budget}
            if running_at is not None:
                record['queue_wait_s'] = round(running_at - started, 3)
                record['backend_elapsed_s'] = round(now - running_at, 3)
            if stage == 'received':
                record.update(shape)
            write_log(record)

        def release_generation():
            nonlocal ticket
            if ticket is not None:
                GENERATIONS.release(ticket)
                ticket = None

        def stream_headers(status=200):
            nonlocal headers_sent
            if headers_sent:
                return
            self.send_response(status)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Barq-Mode', mode)
            self.send_header('X-Barq-Thinking-Budget', str(budget))
            self.end_headers()
            headers_sent = True

        def stream_chunk(chunk):
            if chunk:
                self.wfile.write(('%x\r\n' % len(chunk)).encode('ascii') + chunk + b'\r\n')
                self.wfile.flush()

        def finish_stream(done=None):
            nonlocal stream_finished
            if stream_finished:
                return
            if done:
                stream_chunk(done)
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
            stream_finished = True

        def waiting():
            nonlocal queued_logged, last_heartbeat
            if not queued_logged:
                log('queued')
                queued_logged = True
            now = time.monotonic()
            if data.get('stream') and now - last_heartbeat >= 10:
                stream_headers()
                stream_chunk(b': Barq waiting for the active generation\n\n')
                last_heartbeat = now

        def fail(code, message, value=None):
            release_generation()
            if headers_sent:
                payload = value or {'error': {'message': message, 'type': 'barq_error', 'code': code}}
                stream_chunk(b'data: ' + json.dumps(payload, ensure_ascii=False).encode('utf-8') + b'\n\n')
                finish_stream(b'data: [DONE]\n\n')
            else:
                if value is None:
                    self.error(code, message)
                else:
                    self.close_connection = True
                    self.send_json(code, value)
            log('error_' + str(code))

        log('received')
        try:
            ticket = GENERATIONS.acquire(self.connection, waiting)
            running_at = time.monotonic()
            log('running')
            request = urllib.request.Request(self.server.backend + '/v1/chat/completions',
                data=json.dumps(data, ensure_ascii=False).encode('utf-8'),
                headers={'Content-Type': 'application/json'}, method='POST')
            # Ignore environment HTTP proxies: input must stay on the fixed local backend.
            with self.server.opener.open(request, timeout=300) as response:
                if data.get('stream'):
                    stream_headers(response.status)
                    pending = b''
                    terminal = None
                    while chunk := response.read1(16384):
                        pending += chunk
                        while boundary := re.search(rb'\r?\n\r?\n', pending):
                            event = pending[:boundary.end()]
                            pending = pending[boundary.end():]
                            if any(line.strip() == b'data: [DONE]' for line in event.splitlines()):
                                terminal = event
                                break
                            stream_chunk(event)
                        if terminal is not None:
                            break
                        if len(pending) > 8 * 1024 * 1024:
                            raise RequestProblem('An incomplete SSE event exceeds 8 MiB.')
                    if terminal is None:
                        stream_chunk(pending)
                else:
                    raw_response = response.read(64 * 1024 * 1024 + 1)
                    if len(raw_response) > 64 * 1024 * 1024:
                        raise RequestProblem('Backend response exceeds 64 MiB.')
                    value = json.loads(raw_response)
                    if isinstance(value, dict):
                        value['model'] = model
                    response_status = response.status
            # Close the backend response and release admission before publishing
            # [DONE] or the final JSON; a tool follow-up can arrive immediately.
            release_generation()
            if data.get('stream'):
                finish_stream(terminal)
            else:
                self.send_json(response_status, value)
            log('completed')
        except QueueFull:
            fail(429, 'Barq waiting queue is full (8 queued requests). Try later.')
        except QueueExpired:
            fail(503, 'Barq queue wait exceeded 1200 seconds. Request was not sent to the model.')
        except ClientGone:
            self.close_connection = True
            log('cancelled_while_queued')
        except urllib.error.HTTPError as exc:
            try:
                value = json.loads(exc.read(1024 * 1024))
            except (ValueError, TypeError):
                value = {'error': {'message': 'Local runtime rejected the request.', 'type': 'runtime_error'}}
            fail(exc.code, 'Local runtime rejected the request.', value)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            log('client_disconnected')
        except (OSError, ValueError, RequestProblem) as exc:
            try:
                fail(502, 'Local runtime unavailable or invalid response: ' + str(exc))
            except (OSError, ValueError):
                self.close_connection = True
        finally:
            release_generation()


def main():
    parser = argparse.ArgumentParser(description='Barq 27B local Chat Completions gateway')
    parser.add_argument('--port', type=int, default=8081)
    parser.add_argument('--backend-port', type=int, default=8082)
    parser.add_argument('--context', type=int, default=262144)
    args = parser.parse_args()
    if not (1 <= args.port <= 65535 and 1 <= args.backend_port <= 65535):
        parser.error('Ports must be from 1 to 65535.')
    if args.port == args.backend_port or not 1024 <= args.context <= 262144:
        parser.error('Ports must differ and context must be from 1024 to 262144.')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    server.context = args.context
    server.backend = 'http://127.0.0.1:' + str(args.backend_port)
    server.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    print('Barq 27B API: http://127.0.0.1:' + str(args.port) + '/v1', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
