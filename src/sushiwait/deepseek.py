"""Opt-in official DeepSeek advice with durable, finite per-run reservations.

Only public scenario features leave the process. This is a research adapter,
not a fitted predictor, scheduler, or verified bill/latency guarantee.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import os
from pathlib import Path
import re
import secrets
import ssl
import time

from .capture import _check_parent, _open_parent
from .credentials import _private_file, _read_private_file
from .fusion import FusionError, _advice, fuse, public_request
from .intake import _canonical
from .outcomes import _json, _time, _utc
from .packets import PacketError, _write_packet

HOST = 'api.deepseek.com'
PATH = '/chat/completions'
MODEL = 'deepseek-flash'
PROMPT_VERSION = 'public-scenario-weights-1'
MAX_RESPONSE_BYTES = 32_768
_BUDGET_FIELDS = {'schema_version', 'provider', 'model', 'created_at', 'deadline_at',
    'max_calls', 'max_reserved_tokens', 'max_reserved_cost_microunits',
    'max_prompt_tokens', 'max_output_tokens', 'input_rate_microunits_per_million',
    'output_rate_microunits_per_million', 'price_basis', 'timeout_seconds'}
_SYSTEM = '''Return only a JSON object with exactly weights_ppm, feature_ids, reason_code.
weights_ppm maps every candidate_id to a nonnegative integer; its sum is 1000000.
Candidates: history=historical prior, steady=stable display trend,
fast=faster display turnover, slow=slower display turnover.
Use only the supplied public numeric features and calendar at observation.
feature_ids is a nonempty list of distinct supplied features with non-null values.
reason_code is history_dominant, rapid_display_turnover, slower_display_turnover,
mixed_evidence, or insufficient_evidence. Choose insufficient_evidence when unsure.
Removed display labels are NOT served tables or proven no-shows. Counts have an
unknown unit; source freshness and store identity remain unverified. Do not invent
queue positions, wait minutes, outcomes, throughput, or a no-show rate. You are
suggesting relative scenario weights, not predicting or certifying an ETA.'''


class DeepSeekError(ValueError):
    def __init__(self, code, *, committed=False, network_performed=False):
        # All codes originate here; never preserve a transport/parser exception.
        self.error_code = code if type(code) is str and re.fullmatch('deepseek_[a-z_]+', code) else 'deepseek_failed'
        self.committed = committed is True
        self.network_performed = network_performed is True
        super().__init__(self.error_code)


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def _now():
    return datetime.now(timezone.utc)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _budget(value, now):
    try:
        if (type(value) is not dict or set(value) != _BUDGET_FIELDS
                or not _int(value['schema_version'], 1, 1)
                or value['provider'] != 'deepseek_official' or value['model'] != MODEL
                or value['price_basis'] != 'caller_supplied_ceiling_not_verified'
                or not _int(value['max_calls'], 1, 1000)
                or not _int(value['max_reserved_tokens'], 1, 100_000_000)
                or not _int(value['max_reserved_cost_microunits'], 1, 1_000_000_000)
                or not _int(value['max_prompt_tokens'], 4096, 65536)
                or not _int(value['max_output_tokens'], 128, 2048)
                or not _int(value['timeout_seconds'], 1, 30)
                or any(not _int(value[k], 1, 1_000_000_000) for k in
                    ('input_rate_microunits_per_million', 'output_rate_microunits_per_million'))):
            raise ValueError
        start, end = _time(value['created_at']), _time(value['deadline_at'])
        if not start <= now < end or not start < end <= start + timedelta(days=3):
            raise DeepSeekError('deepseek_budget_expired')
        return {**value, 'created_at': _utc(start), 'deadline_at': _utc(end)}
    except DeepSeekError:
        raise
    except Exception:
        raise DeepSeekError('deepseek_invalid_budget') from None


def _key(path):
    try:
        value = _json(_read_private_file(path))
        if (set(value) != {'schema_version', 'purpose', 'api_key'}
                or not _int(value['schema_version'], 1, 1) or value['purpose'] != 'deepseek_official'
                or type(value['api_key']) is not str
                or not re.fullmatch('sk-[A-Za-z0-9_-]{8,256}', value['api_key'])):
            raise ValueError
        return value['api_key']
    except Exception:
        raise DeepSeekError('deepseek_invalid_key') from None


def _wire(request, max_output_tokens):
    return _canonical({'model': MODEL, 'thinking': {'type': 'disabled'},
        'stream': False, 'temperature': 0, 'max_tokens': max_output_tokens,
        'response_format': {'type': 'json_object'},
        'messages': [{'role': 'system', 'content': _SYSTEM},
                     {'role': 'user', 'content': _canonical(request)}]}).encode()


def _observation_key(request):
    # Read-time timestamps are not new observations. Do not bill another request
    # merely because fusion-context was read again. Exact-input cache remains
    # distinct: changed bindings are suppressed, never silently re-stamped.
    context = deepcopy(request['context'])
    for field in ('as_of', 'expires_at'):
        del context[field]
    for feature in context['features']:
        del feature['available_at']
    identity = {'provider': 'deepseek_official', 'model': MODEL,
        'prompt_version': PROMPT_VERSION, 'model_version': request['model_version'],
        'candidate_ids': request['candidate_ids'], 'context': context}
    if request['policy'] != 'public_scenario_interval_mixture_v1':
        identity['policy'] = request['policy']
    return _digest(identity)


def _cost(prompt, completion, budget):
    # Round up micro-CNY at caller-supplied ceiling prices, without cache discounts.
    numerator = (prompt * budget['input_rate_microunits_per_million']
                 + completion * budget['output_rate_microunits_per_million'])
    return (numerator + 999_999) // 1_000_000


class _Ledger:
    """Private directory lock; write reservation durably BEFORE the paid POST.

    Reservations never get refunded, including timeouts and process crashes.
    Resuming the same directory cannot retry an uncertain call or renew its cap.
    """
    def __init__(self, budget_file, now):
        self.path = Path(os.path.abspath(budget_file))
        self.fd = None
        try:
            import fcntl
            self.fd, self.name = _open_parent(self.path, private=True)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.budget = _budget(_json(_read_private_file(self.path)), now)
            self.budget_sha = _digest(self.budget)
            self.calls = []
            names = os.listdir(self.fd)
            numbered = [n for n in names if n.startswith(('deepseek-call-', 'deepseek-result-'))]
            if len(numbered) > self.budget['max_calls'] * 2:
                raise ValueError
            for index in range(1, self.budget['max_calls'] + 1):
                name = f'deepseek-call-{index}.json'
                if name not in names:
                    break
                call = self.read(name)
                if (set(call) != {'schema_version', 'budget_sha256', 'public_input_sha256',
                    'observation_key', 'reserved_at', 'reserved_tokens', 'reserved_cost_microunits'}
                        or not _int(call['schema_version'], 1, 1) or call['budget_sha256'] != self.budget_sha
                        or any(type(call[k]) is not str or not re.fullmatch('[0-9a-f]{64}', call[k])
                            for k in ('public_input_sha256', 'observation_key'))
                        or call['reserved_tokens'] != self.budget['max_prompt_tokens'] + self.budget['max_output_tokens']
                        or type(call['reserved_tokens']) is not int
                        or call['reserved_cost_microunits'] != _cost(self.budget['max_prompt_tokens'], self.budget['max_output_tokens'], self.budget)
                        or type(call['reserved_cost_microunits']) is not int
                        or not _time(self.budget['created_at']) <= _time(call['reserved_at']) <= now):
                    raise ValueError
                result_name = f'deepseek-result-{index}.json'
                result = self.read(result_name) if result_name in names else None
                if result is not None:
                    if (set(result) != {'public_input_sha256', 'transport_basis', 'usage', 'advice', 'overrun'}
                            or result['public_input_sha256'] != call['public_input_sha256']
                            or result['transport_basis'] not in ('official_https', 'injected_transport')
                            or type(result['overrun']) is not bool):
                        raise ValueError
                    if result['usage'] is not None:
                        stored_usage = result['usage']
                        if (type(stored_usage) is not dict or set(stored_usage) != {
                            'prompt_tokens', 'completion_tokens', 'total_tokens',
                            'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'reasoning_tokens'}
                                or _usage({'usage': {**stored_usage, 'completion_tokens_details': {
                                    'reasoning_tokens': stored_usage['reasoning_tokens']}}}) != stored_usage):
                            raise ValueError
                        actual_overrun = (stored_usage['prompt_tokens'] > self.budget['max_prompt_tokens']
                            or stored_usage['completion_tokens'] > self.budget['max_output_tokens'])
                        if actual_overrun != result['overrun']:
                            raise ValueError
                    if result['advice'] is not None and (result['usage'] is None or result['usage']['reasoning_tokens'] != 0):
                        raise ValueError
                    if result['overrun']:
                        raise DeepSeekError('deepseek_usage_overrun')
                self.calls.append((call, result))
            expected = {f'deepseek-call-{i}.json' for i in range(1, len(self.calls)+1)}
            expected |= {f'deepseek-result-{i}.json' for i in range(1, len(self.calls)+1)
                         if self.calls[i-1][1] is not None}
            if set(numbered) != expected:
                raise ValueError
        except DeepSeekError:
            self.close(); raise
        except BlockingIOError:
            self.close(); raise DeepSeekError('deepseek_ledger_busy') from None
        except Exception:
            self.close(); raise DeepSeekError('deepseek_invalid_ledger') from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd); self.fd = None

    def read(self, name):
        return _json(_read_private_file(self.path.parent / name))

    def check_budget(self, now):
        try:
            current = _budget(_json(_read_private_file(self.path)), now)
            if _digest(current) != self.budget_sha:
                raise DeepSeekError('deepseek_budget_changed')
        except DeepSeekError:
            raise
        except Exception:
            raise DeepSeekError('deepseek_invalid_ledger') from None

    def publish(self, name, value):
        body = _canonical(value).encode()
        if len(body) > 16_384:
            raise DeepSeekError('deepseek_invalid_ledger')
        temporary = '.deepseek-' + secrets.token_hex(16) + '.tmp'
        fd = None
        try:
            _check_parent(self.path, self.fd, private=True)
            os.fsync(self.fd)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.fd)
            if not _private_file(os.fstat(fd)):
                raise ValueError
            offset = 0
            while offset < len(body):
                written = os.write(fd, body[offset:])
                if written <= 0:
                    raise ValueError
                offset += written
            os.fsync(fd)
            _check_parent(self.path, self.fd, private=True)
            os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
            os.unlink(temporary, dir_fd=self.fd)
            os.fsync(self.fd)
        except Exception:
            # If publication occurred but sync failed, its reservation remains;
            # the caller still MUST NOT send the POST or remove that receipt.
            raise DeepSeekError('deepseek_ledger_write_failed') from None
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def reserve(self, request, observation_key, now):
        tokens = self.budget['max_prompt_tokens'] + self.budget['max_output_tokens']
        cost = _cost(self.budget['max_prompt_tokens'], self.budget['max_output_tokens'], self.budget)
        if (len(self.calls) >= self.budget['max_calls']
                or sum(c['reserved_tokens'] for c, _ in self.calls) + tokens > self.budget['max_reserved_tokens']
                or sum(c['reserved_cost_microunits'] for c, _ in self.calls) + cost > self.budget['max_reserved_cost_microunits']):
            raise DeepSeekError('deepseek_budget_exhausted')
        call = {'schema_version': 1, 'budget_sha256': self.budget_sha,
            'public_input_sha256': request['public_input_sha256'], 'observation_key': observation_key,
            'reserved_at': _utc(now), 'reserved_tokens': tokens, 'reserved_cost_microunits': cost}
        self.publish(f'deepseek-call-{len(self.calls)+1}.json', call)
        self.calls.append((call, None))


def _https_post(body, key, timeout):
    """Fixed TLS host/path, no redirects/proxy discovery/retries; bounded body.

    Socket timeouts and read deadline do not guarantee an OS DNS wall deadline.
    Any late complete response is rejected by the caller's monotonic deadline.
    """
    connection = http.client.HTTPSConnection(HOST, timeout=timeout, context=ssl.create_default_context())
    deadline = time.monotonic() + timeout
    try:
        connection.request('POST', PATH, body=body, headers={'Authorization': 'Bearer ' + key,
            'Content-Type': 'application/json', 'Accept': 'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            raise DeepSeekError('deepseek_http_failed')
        length = response.getheader('Content-Length')
        if length is not None and (not re.fullmatch('[0-9]{1,9}', length) or int(length) > MAX_RESPONSE_BYTES):
            raise DeepSeekError('deepseek_response_too_large')
        pieces, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeepSeekError('deepseek_timeout')
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            piece = response.read(min(4096, MAX_RESPONSE_BYTES+1-size))
            if not piece:
                break
            pieces.append(piece); size += len(piece)
            if size > MAX_RESPONSE_BYTES:
                raise DeepSeekError('deepseek_response_too_large')
        return b''.join(pieces)
    except DeepSeekError:
        raise
    except TimeoutError:
        raise DeepSeekError('deepseek_timeout') from None
    except Exception:
        raise DeepSeekError('deepseek_transport_failed') from None
    finally:
        connection.close()


def _usage(response):
    try:
        usage = response['usage']
        fields = ('prompt_tokens', 'completion_tokens', 'total_tokens',
                  'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')
        if (type(usage) is not dict or any(not _int(usage[k], 0, 100_000_000) for k in fields)
                or usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']
                or usage['prompt_tokens'] != usage['prompt_cache_hit_tokens'] + usage['prompt_cache_miss_tokens']):
            raise ValueError
        details = usage.get('prompt_tokens_details', {})
        if type(details) is not dict or details.get('cached_tokens', usage['prompt_cache_hit_tokens']) != usage['prompt_cache_hit_tokens']:
            raise ValueError
        thinking = usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0)
        if not _int(thinking, 0, usage['completion_tokens']):
            raise ValueError
        return {**{k: usage[k] for k in fields}, 'reasoning_tokens': thinking}
    except Exception:
        raise DeepSeekError('deepseek_invalid_usage') from None


def _response_advice(response, request, now):
    try:
        choices = response['choices']
        if (response['object'] != 'chat.completion' or response['model'] != MODEL
                or type(choices) is not list or len(choices) != 1 or choices[0]['finish_reason'] != 'stop'
                or not _int(choices[0]['index'], 0, 0)):
            raise ValueError
        message = choices[0]['message']
        if (message['role'] != 'assistant' or message.get('tool_calls') not in (None, [])
                or message.get('reasoning_content') not in (None, '')
                or type(message['content']) is not str or len(message['content'].encode()) > 16_384):
            raise ValueError
        answer = _json(message['content'])
        if set(answer) != {'weights_ppm', 'feature_ids', 'reason_code'}:
            raise ValueError
        advice = {'schema_version': 1, **answer, 'public_input_sha256': request['public_input_sha256'],
            'model_version': request['model_version'], 'observation_revision': request['context']['observation_revision'],
            'generated_at': _utc(now), 'expires_at': request['context']['expires_at']}
        _advice(advice, request, now=now)
        return advice
    except FusionError:
        raise
    except Exception:
        raise DeepSeekError('deepseek_invalid_response') from None


def run_deepseek(plan, *, budget_file=None, key_file=None, allow_paid_request=False,
                 current_request=None, clock=_now, monotonic=time.monotonic, transport=None):
    """Return private fusion result. No key/network/ledger access unless opted in.

    current_request is an optional frozen-input check, not an atomic online
    publisher. Injected transports are always labelled synthetic/unattested.
    """
    initial = clock()
    request = public_request(plan, now=initial)
    advice, reason, usage, ledger = None, None, None, None
    attempted = cached = False
    basis = 'injected_transport' if transport is not None else 'official_https'

    def check_current():
        if current_request is not None and current_request() != request:
            error = FusionError('fusion_superseded_input')
            error.network_performed = attempted and transport is None
            raise error

    try:
        check_current()
        if allow_paid_request is not True:
            raise DeepSeekError('deepseek_disabled')
        if len(request['candidate_ids']) < 2:
            raise DeepSeekError('deepseek_single_scenario')
        # Validate current evidence before reading a key or charging a reservation.
        context = request['context']
        preflight = {'schema_version': 1, 'public_input_sha256': request['public_input_sha256'],
            'observation_revision': context['observation_revision'], 'model_version': request['model_version'],
            'generated_at': _utc(initial), 'expires_at': context['expires_at'],
            'weights_ppm': plan['prior_weights_ppm'], 'reason_code': 'mixed_evidence',
            'feature_ids': [f['feature_id'] for f in context['features'] if f['value'] is not None]}
        _advice(preflight, request, now=initial)
        if budget_file is None or key_file is None:
            raise DeepSeekError('deepseek_configuration_missing')
        if Path(budget_file).resolve() == Path(key_file).resolve():
            raise DeepSeekError('deepseek_configuration_conflict')
        ledger = _Ledger(budget_file, initial)
        observation = _observation_key(request)
        for call, result in ledger.calls:
            if call['observation_key'] == observation:
                if (call['public_input_sha256'] == request['public_input_sha256'] and result is not None
                        and result['advice'] is not None and result['transport_basis'] == basis):
                    _advice(result['advice'], request, now=initial)
                    advice = result['advice']; usage = result['usage']; cached = True
                    break
                raise DeepSeekError('deepseek_duplicate_observation')
        if advice is None:
            key = _key(key_file)
            body = _wire(request, ledger.budget['max_output_tokens'])
            # Byte ceiling plus conservative framing allowance. This is a local
            # guard, not a claim to know the provider's tokenizer/billing rules.
            if len(body) + 4096 > ledger.budget['max_prompt_tokens']:
                raise DeepSeekError('deepseek_input_too_large')
            check_current()
            ledger.check_budget(clock())
            ledger.reserve(request, observation, clock())
            ledger.check_budget(clock())
            _advice({**preflight, 'generated_at': _utc(clock())}, request, now=clock())
            check_current()
            began = monotonic(); attempted = True
            raw = (transport or _https_post)(body, key, ledger.budget['timeout_seconds'])
            now = clock()
            if type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES:
                raise DeepSeekError('deepseek_response_too_large')
            response = _json(raw)
            usage = _usage(response)
            overrun = (usage['prompt_tokens'] > ledger.budget['max_prompt_tokens']
                       or usage['completion_tokens'] > ledger.budget['max_output_tokens'])
            result = {'public_input_sha256': request['public_input_sha256'], 'transport_basis': basis,
                      'usage': usage, 'advice': None, 'overrun': overrun}
            failure = None
            try:
                if overrun:
                    raise DeepSeekError('deepseek_usage_overrun')
                if usage['reasoning_tokens'] != 0:
                    raise DeepSeekError('deepseek_unexpected_thinking')
                if monotonic()-began >= ledger.budget['timeout_seconds']:
                    raise DeepSeekError('deepseek_timeout')
                ledger.check_budget(now)
                check_current()
                advice = _response_advice(response, request, now)
                result['advice'] = advice
            except (DeepSeekError, FusionError) as error:
                failure = error
            ledger.publish(f'deepseek-result-{len(ledger.calls)}.json', result)
            if failure is not None:
                raise failure
        check_current()
    except FusionError as error:
        if error.error_code == 'fusion_superseded_input':
            raise
        reason = error.error_code; advice = None
    except DeepSeekError as error:
        reason = error.error_code; advice = None
    except Exception:
        reason = 'deepseek_failed'; advice = None
    finally:
        if ledger is not None:
            ledger.close()
    result = fuse(plan, advice=advice, now=clock())
    if reason is not None:
        result['fallback_reason'] = reason
    result.update({'provider': 'deepseek_official', 'provider_model': MODEL,
        'prompt_version': PROMPT_VERSION, 'provider_request_attempted': attempted,
        'network_performed': attempted and transport is None, 'provider_called': attempted and transport is None,
        'advice_cache_hit': cached, 'transport_basis': basis,
        'advice_origin_basis': 'official_https_observed_not_performance_verified' if result['advice_accepted'] and basis == 'official_https'
                               else 'injected_transport_not_provider_attested' if result['advice_accepted']
                               else 'no_accepted_provider_advice',
        'usage': usage, 'usage_source': 'cached_response' if cached else 'response' if usage is not None else 'unknown',
        'estimated_cost_microunits': _cost(usage['prompt_tokens'], usage['completion_tokens'], ledger.budget)
                                  if usage is not None and ledger is not None else None,
        'price_basis': 'caller_supplied_ceiling_not_verified', 'billing_verified': False,
        'reservation_refunded': False, 'automatic_retry': False,
        'online_atomic_publication_verified': False})
    return result


def write_deepseek_fusion(plan, *, destination, **options):
    """Preflight a new private output before opting into any provider request."""
    fd = None
    try:
        fd, name = _open_parent(destination, private=True)
        try:
            os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise DeepSeekError('deepseek_output_exists')
        if options.get('allow_paid_request') is True:
            paths = [Path(os.path.abspath(p)) for p in
                     (destination, options.get('budget_file'), options.get('key_file')) if p is not None]
            if len(paths) != 3 or len(set(paths)) != 3 or paths[0].parent == paths[1].parent:
                raise DeepSeekError('deepseek_configuration_conflict')
        _check_parent(destination, fd, private=True)
    except DeepSeekError:
        raise
    except Exception:
        raise DeepSeekError('deepseek_output_unsafe') from None
    finally:
        if fd is not None:
            os.close(fd)
    result = run_deepseek(plan, **options)
    try:
        publication = _write_packet(_canonical(result).encode(), destination)
    except PacketError as error:
        # Caller must not repeat a paid POST; the durable ledger is independent.
        raise DeepSeekError('deepseek_output_write_failed', committed=error.committed,
                            network_performed=result['network_performed']) from None
    return {'artifact_written': True, 'committed': True,
        'durability_confirmed': publication['durability_confirmed'],
        **{key: result[key] for key in ('provider', 'provider_model', 'prompt_version',
            'advice_accepted', 'ai_numerical_influence_applied', 'advice_origin_basis',
            'advice_cache_hit', 'fallback_reason', 'network_performed', 'provider_called',
            'provider_request_attempted', 'usage_source', 'billing_verified', 'automatic_retry',
            'eta_available', 'verified_training_labels', 'output_requires_private_handling')}}
