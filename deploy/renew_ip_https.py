"""Renew only the owner's IP certificate under the accepted CA agreement."""
import json
import subprocess
import urllib.request

DIRECTORY = 'https://acme-v02.api.letsencrypt.org/directory'
ACCEPTED_TERMS = 'https://letsencrypt.org/documents/LE-SA-v1.8-July-06-2026.pdf'


def main():
    with urllib.request.urlopen(DIRECTORY, timeout=20) as response:
        directory = json.loads(response.read(32768))
    if directory.get('meta', {}).get('termsOfService') != ACCEPTED_TERMS:
        raise RuntimeError('ca_agreement_changed_requires_owner_confirmation')
    subprocess.run(['/opt/sushiwait-certbot/bin/certbot', 'renew',
                    '--cert-name', 'sushiwait-ip', '--quiet'], check=True, timeout=180)
    subprocess.run(['nginx', '-t'], check=True)
    subprocess.run(['systemctl', 'reload', 'nginx'], check=True)


if __name__ == '__main__':
    main()
