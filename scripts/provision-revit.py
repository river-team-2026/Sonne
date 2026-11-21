"""Create private router credentials without writing secrets to stdout."""
import argparse
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

p = argparse.ArgumentParser()
p.add_argument('runtime', type=Path)
p.add_argument('url')
p.add_argument('--client-id', default='windows-01')
p.add_argument('--label', default='Revit Windows 01')
p.add_argument('--enable-writes', action='store_true')
p.add_argument('--enable-publications', action='store_true')
a = p.parse_args()
if urlparse(a.url).scheme != 'https' or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', a.client_id):
    p.error('Use an HTTPS URL and an alphanumeric client ID.')
os.umask(0o077)
hub = a.runtime / '.state/revit-hub'
mcp = a.runtime / '.state/openclaw/integrations/revit'
for directory in [hub, mcp]:
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
registry_file = hub / 'registry.json'
registry = json.loads(registry_file.read_text()) if registry_file.exists() else {'serverToken': secrets.token_hex(32), 'clients': []}
client = next((item for item in registry['clients'] if item['id'] == a.client_id), None)
if client is None:
    client = {'id': a.client_id, 'label': a.label, 'token': secrets.token_hex(32)}
    registry['clients'].append(client)
if a.enable_writes:
    client['allowWrites'] = True
if a.enable_publications:
    client['allowPublications'] = True
files = {
    registry_file: registry,
    mcp / 'server.json': {
        'url': 'http://revit-hub:9091/',
        'publicationUrl': 'http://revit-publications:9092/',
        'token': registry['serverToken'],
    },
    hub / f'enroll-{a.client_id}.json': {
        'url': a.url,
        'token': client['token'],
        'enableWrites': client.get('allowWrites', False),
        'enablePublications': client.get('allowPublications', False),
    },
    a.runtime / '.state/openclaw/revit-mcp-config.json': [{'path': 'mcp.servers.revit', 'value': {'command': 'node', 'args': ['/home/node/.openclaw/integrations/revit/mcp-server.mjs']}}],
}
for path, data in files.items():
    path.write_text(json.dumps(data, indent=2) + '\n')
    path.chmod(0o600)
tools_path = a.runtime / 'templates/workspace/AGENTS.md'
start, end = '<!-- sonne-revit:start -->', '<!-- sonne-revit:end -->'
workflow = (Path(__file__).resolve().parents[1] / 'integrations/revit/WORKFLOW.md').read_text()
existing = tools_path.read_text() if tools_path.exists() else ''
if start in existing:
    before, section = existing.split(start, 1)
    if end not in section:
        raise SystemExit('Unclosed Revit section in AGENTS.md; preserve and repair it before setup.')
    _, after = section.split(end, 1)
    content = before + start + '\n' + workflow + end + after
else:
    content = existing.rstrip() + '\n\n' + start + '\n' + workflow + end + '\n'
tools_path.write_text(content.lstrip())
tools_path.chmod(0o644)
# Remove only our obsolete managed section: this runtime does not bootstrap TOOLS.md.
obsolete = a.runtime / 'templates/workspace/TOOLS.md'
if obsolete.exists():
    old = obsolete.read_text()
    if start in old and end in old.split(start, 1)[1]:
        before, section = old.split(start, 1)
        _, after = section.split(end, 1)
        rest = (before + after).strip()
        if rest:
            obsolete.write_text(rest + '\n')
        else:
            obsolete.unlink()
print(f'Provisioned {a.client_id}. Private enrollment file: {hub / f"enroll-{a.client_id}.json"}')
