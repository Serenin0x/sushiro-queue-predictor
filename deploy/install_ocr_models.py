"""Download pinned, public PP-OCRv5 mobile models; never handles user images."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request


def install(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    manifest=json.loads(Path(__file__).with_name('ocr-models.json').read_text())
    for task,row in manifest.items():
        target=directory/(task+'.onnx')
        if target.exists():
            if target.stat().st_size==row['bytes'] and hashlib.sha256(target.read_bytes()).hexdigest()==row['sha256']:continue
            raise ValueError('existing_model_mismatch')
        with urllib.request.urlopen(row['url'],timeout=60) as response:raw=response.read(row['bytes']+1)
        if len(raw)!=row['bytes'] or hashlib.sha256(raw).hexdigest()!=row['sha256']:
            raise ValueError('downloaded_model_mismatch')
        with tempfile.NamedTemporaryFile(dir=directory,delete=False) as output:
            temporary=Path(output.name);output.write(raw);output.flush();os.fsync(output.fileno())
        os.replace(temporary,target)
    print('Two pinned CPU OCR models ready.')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--directory',required=True)
    install(parser.parse_args().directory)
