"""Apply the guarded ExternalEvent adapter to the pinned upstream command set."""
import json
import re
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
repo = Path(__file__).resolve().parents[1]
project = root / 'commandset/RevitMCPCommandSet.csproj'
source = project.read_text(encoding='utf-8-sig')
if '<AssemblyName>' in source:
    source = re.sub(r'<AssemblyName>[^<]+</AssemblyName>', '<AssemblyName>SonneUpstreamCommandsV2</AssemblyName>', source)
else:
    source = source.replace('<UseWPF>true</UseWPF>', '<UseWPF>true</UseWPF>\n        <AssemblyName>SonneUpstreamCommandsV2</AssemblyName>', 1)
project.write_text(source, encoding='utf-8')
shutil.copyfile(repo / 'integrations/revit/upstream-guard.cs', root / 'commandset/SonneGuard.cs')
commands = []
for path in (root / 'commandset/Commands').rglob('*.cs'):
    source = path.read_text(encoding='utf-8-sig')
    if not re.search(r'class\s+\w+\s*:\s*ExternalEventCommandBase', source):
        continue
    source = source.replace('using RevitMCPSDK.API.Base;', 'using Sonne.Upstream;')
    if 'using var sonneScope' not in source:
        source, count = re.subn(r'(public override object Execute\(JObject parameters, string requestId\)\s*\{)', r'\1\n            using var sonneScope = BeginSonneRequest(parameters, requestId);', source)
        if count != 1:
            raise SystemExit(f'Unexpected command entry point: {path}')
    path.write_text(source, encoding='utf-8')
    commands.append(re.search(r'CommandName\s*=>\s*"([^"]+)"', source)[1])
for path in list((root / 'commandset/Services').rglob('*.cs')) + [root / 'commandset/Commands/ExecuteDynamicCode/ExecuteCodeEventHandler.cs']:
    source = path.read_text(encoding='utf-8-sig')
    source = re.sub(r'(?m)^(\s*)(\w+)\.Start\(\);(?!\s*// Sonne transaction)', r'\1\2.Start(); // Sonne transaction\n\1Sonne.Upstream.ExternalEventCommandBase.ConfigureTransaction(\2);', source)
    source = re.sub(r'(?m)^(\s*)(\w+)\.Commit\(\);', r'\1if (\2.Commit() != TransactionStatus.Committed) throw new InvalidOperationException("Revit transaction was rolled back.");', source)
    if path.name != 'SayHelloEventHandler.cs':
        source = source.replace('TaskDialog.Show(', 'Sonne.Upstream.ExternalEventCommandBase.ReportError(')
    if path.name == 'ExecuteCodeEventHandler.cs' and '.GroupBy(a => a.GetName().Name)' not in source:
        source = source.replace('.Select(a => MetadataReference.CreateFromFile(a.Location))', '.GroupBy(a => a.GetName().Name)\n                .Select(group => group.OrderByDescending(a => a.GetName().Version).First())\n                .Select(a => MetadataReference.CreateFromFile(a.Location))', 1)
    path.write_text(source, encoding='utf-8')
# Forward trusted request metadata independently of model-supplied arguments.
socket = root / 'server/src/utils/SocketClient.ts'
source = socket.read_text(encoding='utf-8')
if 'sendSonneCommand(' not in source:
    marker = '  public sendCommand('
    source = source.replace(marker, '''  public sendSonneCommand(context: any, command: string, params: any = {}): Promise<any> {
    return this.sendCommand(command, {...params, ...(context || {})});
  }

''' + marker, 1)
socket.write_text(source, encoding='utf-8')
for path in (root / 'server/src/tools').glob('*.ts'):
    source = path.read_text(encoding='utf-8')
    source = source.replace('revitClient.sendCommand(', 'revitClient.sendSonneCommand(extra._meta?.sonne, ')
    source = re.sub(r'(catch \(error\)\s*\{\s*return \{)(?!\s*isError)', r'\1\n          isError: true,', source)
    path.write_text(source, encoding='utf-8')
if len(commands) != 23:
    raise SystemExit('Unexpected upstream command count; review the pinned source before installation.')
(root / 'sonne-full-commands.json').write_text(json.dumps(sorted(commands)), encoding='utf-8')
path = root / 'commandset/Models/Common/ViewInfo.cs'
source = path.read_text(encoding='utf-8-sig')
if 'SonneGuardVersion' not in source:
    source = source.replace('public string DocumentSessionId', 'public string SonneGuardVersion { get; set; } = "full-guarded-v2";\n        public string DocumentSessionId', 1)
source = source.replace('full-guarded-v1', 'full-guarded-v2')
path.write_text(source, encoding='utf-8')
print(f'Prepared {len(commands)} guarded upstream Revit commands.')
