"""Issue only the original-account capability through the fixed local broker.

The caller must verify its owner session before using this helper. This never
creates a QR session, queries a store, or resets any collector fault/ledger.
"""
import json
import re
import urllib.error
import urllib.request

from .credentials import _read_private_file
from .manualcollection import ManualError


def issue_link(key_file, origin):
    key = _read_private_file(key_file)
    if len(key) != 32:
        raise ManualError('reauthorization_unavailable',503)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    request=urllib.request.Request('http://127.0.0.1:18910/internal/issue',
        data=b'{"verification":true}',method='POST',
        headers={'Content-Type':'application/json','X-SUSHIWAIT-Internal':key.hex()})
    try:
        with opener.open(request,timeout=4) as response:
            raw=response.read(1025)
            if len(raw)>1024:raise ValueError('oversized')
            value=json.loads(raw)
            if type(value) is not dict or set(value)!={'url'} or type(value['url']) is not str:
                raise ValueError('invalid')
            prefix=origin+'/recover/#'
            if not value['url'].startswith(prefix) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',value['url'][len(prefix):]):
                raise ValueError('invalid')
            return '/recover/#'+value['url'][len(prefix):]
    except urllib.error.HTTPError as error:
        if error.code==409:
            raise ManualError('reauthorization_blocked',409) from None
        raise ManualError('reauthorization_unavailable',503) from None
    except (OSError,ValueError,KeyError):
        raise ManualError('reauthorization_unavailable',503) from None
