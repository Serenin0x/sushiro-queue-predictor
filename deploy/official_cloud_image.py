"""Pin the approved image after offline import; never pull or run an image."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

MANIFEST = 'sha256:1a26a472f4c51322cf07ec087e946d5c38d259dd67273c62ef26b566c8aadddf'
CONFIG = 'sha256:7d4479e14ce709a2210f3939b63005c1a26121d33861693de866ad6ae5d94877'
LAYERS = ('sha256:1073f425247ed24670458429c13a67afbd36f83d2004dade046ddbd6519b1839', 'sha256:f68b7776cf57b2f67d6065faa071ced251c32864a19ed7e133356b1d60178e79', 'sha256:5f70bf18a086007016e948b04aed3b82103a36bea41755b6cddfaf10ace3c6ef', 'sha256:b19163403914ff517c2caa9a744ff7e661ae821a480936bab4f1bbe52ddb6673', 'sha256:d758ac60551d1a40c7c956fb8f3521687d8a286aac91b839ec3a0072ca857171', 'sha256:64fc3701908f1ea24e86872b1f629529663e0fcbdb597ebb27e49283238dbe00')
SOURCE = 'ghcr.io/525815266/yyb-go-enhanced@sha256:1a26a472f4c51322cf07ec087e946d5c38d259dd67273c62ef26b566c8aadddf'
COMPOSE_SHA = '9767c929823168abec84c7f2ae71dcc6d22066b18005ecbe1c1e7f912e62cd00'
BOUND_CONFIG_SHA = '41cffeb8200f90c3b302389d0d22b2d3a380298dd174d321f841a06713179141'

BOUND_MANIFEST_SHA = '7b21d668ac50614d8096bb26e867f41e814399b6f1d92c51bc6b7eea80a7bf9b'

def validate_image(values):
    if (type(values) is not list or len(values) != 4
            or values[0] not in {CONFIG, MANIFEST}
            or values[1:3] != ['amd64', 'linux']
            or type(values[3]) is not list or tuple(values[3]) != LAYERS):
        raise ValueError('approved_image_mismatch')


def inspect_image(image):
    result = subprocess.run(['docker', 'image', 'inspect', image, '--format',
        '{{json .Id}}\n{{json .Architecture}}\n{{json .Os}}\n{{json .RootFS.Layers}}'],
        capture_output=True, timeout=15)
    if result.returncode:
        raise ValueError('approved_image_missing')
    values=[json.loads(x) for x in result.stdout.decode().splitlines()]
    validate_image(values)
    return values[0]


def main(root, mode):
    root = Path(root)
    if not root.is_absolute() or not root.is_dir() or mode not in {'verify', 'bind-local'}:
        raise ValueError('approved_image_scope_invalid')
    target = root/'yyb_cloud_trial.compose.yaml'
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        body = stream.read(16385)
    digest = hashlib.sha256(body).hexdigest()
    if len(body)>16384 or digest not in {COMPOSE_SHA, BOUND_CONFIG_SHA, BOUND_MANIFEST_SHA}:
        raise ValueError('approved_compose_mismatch')
    if mode == 'bind-local':
        try:
            identity=inspect_image(CONFIG)
        except ValueError as error:
            if str(error)!='approved_image_missing':raise
            identity=inspect_image(MANIFEST)
        if digest != COMPOSE_SHA or body.count(SOURCE.encode()) != 1:
            raise ValueError('approved_binding_already_changed')
        bound = body.replace(SOURCE.encode(), identity.encode())
        if hashlib.sha256(bound).hexdigest() not in {BOUND_CONFIG_SHA, BOUND_MANIFEST_SHA}:
            raise ValueError('approved_binding_mismatch')
        backup=root/'yyb_cloud_trial.compose.original.yaml'
        fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(body);stream.flush();os.fsync(stream.fileno())
        pending=root/'yyb_cloud_trial.compose.bound.pending'
        fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(bound);stream.flush();os.fsync(stream.fileno())
        os.replace(pending,target)
        fd=os.open(root,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    else:
        inspect_image(CONFIG if digest == BOUND_CONFIG_SHA else MANIFEST if digest == BOUND_MANIFEST_SHA else SOURCE)
    return {'approved_image_verified':True, 'all_layers_matched':True,
            'network_pulls_added':0,'container_started':False}


if __name__ == '__main__':
    try:
        if len(sys.argv)!=3:raise ValueError('scope')
        print(json.dumps(main(sys.argv[1],sys.argv[2])))
    except Exception:
        print('{"approved_image_verified":false,"error_code":"approved_image_guard_failed"}')
        raise SystemExit(1)
