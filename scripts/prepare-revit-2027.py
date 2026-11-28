"""Adapt the pinned upstream checkout for the Sonne Windows client."""
from pathlib import Path
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('source', type=Path)
args = parser.parse_args()
root = args.source.resolve()
for name in ['plugin/RevitMCPPlugin.csproj', 'commandset/RevitMCPCommandSet.csproj']:
    path = root / name
    source = path.read_text(encoding='utf-8-sig')
    if "Configuration.Contains('R27')" not in source:
        marker = '    <PropertyGroup Condition="$(Configuration.Contains(\'R26\'))">'
        block = source.split(marker, 1)[1].split('    </PropertyGroup>', 1)[0]
        new = marker.replace('R26', 'R27') + block.replace('2026', '2027').replace('net8.0-', 'net10.0-') + '    </PropertyGroup>\n'
        source = source.replace(marker, new + marker, 1)
    if name.startswith('commandset/'):
        marker27 = '    <PropertyGroup Condition="$(Configuration.Contains(\'R27\'))">'
        before, tail = source.split(marker27, 1)
        block, after = tail.split('    </PropertyGroup>', 1)
        if 'REVIT2027_OR_GREATER' not in block:
            block = block.replace('REVIT2024_OR_GREATER', 'REVIT2024_OR_GREATER;REVIT2027_OR_GREATER')
        source = before + marker27 + block + '    </PropertyGroup>' + after
    path.write_text(source, encoding='utf-8', newline='\n')
path = root / 'plugin/Core/SocketService.cs'
source = path.read_text(encoding='utf-8-sig').replace('new TcpListener(IPAddress.Any, _port)', 'new TcpListener(IPAddress.Loopback, _port)')
source = source.replace('_port = 8080;', '_port = 18080;')
path.write_text(source, encoding='utf-8', newline='\n')
path = root / 'server/src/utils/ConnectionManager.ts'
source = path.read_text(encoding='utf-8').replace('RevitClientConnection("localhost", 8080)', 'RevitClientConnection("127.0.0.1", 18080)')
path.write_text(source, encoding='utf-8', newline='\n')
path = root / 'plugin/Core/Application.cs'
source = path.read_text(encoding='utf-8-sig')
if 'StartSonneWhenDocumentOpens' not in source:
    source = source.replace('public class Application : IExternalApplication\n    {', '''public class Application : IExternalApplication
    {
        private UIControlledApplication _controlled;
        private void StartSonneWhenDocumentOpens(object sender, Autodesk.Revit.UI.Events.IdlingEventArgs args)
        {
            if (sender is not UIApplication app || app.ActiveUIDocument == null) return;
            try
            {
                SocketService.Instance.Initialize(app);
                SocketService.Instance.Start();
                _controlled.Idling -= StartSonneWhenDocumentOpens;
            }
            catch { _controlled.Idling -= StartSonneWhenDocumentOpens; }
        }''', 1)
    source = source.replace('RibbonPanel mcpPanel =', '''_controlled = application;
            application.Idling += StartSonneWhenDocumentOpens;
            RibbonPanel mcpPanel =''', 1)
path.write_text(source, encoding='utf-8', newline='\n')
path = root / 'commandset/Models/Common/ViewInfo.cs'
source = path.read_text(encoding='utf-8-sig')
if 'DocumentSessionId' not in source:
    source = source.replace('public long Id', 'public string DocumentSessionId { get; set; }\n        public string DocumentTitle { get; set; }\n        public string RevitVersion { get; set; }\n        public long Id', 1)
path.write_text(source, encoding='utf-8', newline='\n')
path = root / 'commandset/Services/GetCurrentViewInfoEventHandler.cs'
source = path.read_text(encoding='utf-8-sig')
if 'DocumentSessionId' not in source:
    source = source.replace('var doc = uiDoc.Document;', '''if (uiDoc == null)
                {
                    ResultInfo = new CurrentViewInfo { RevitVersion = app.Application.VersionNumber };
                    return;
                }
                var doc = uiDoc.Document;''', 1)
    source = source.replace('ResultInfo = new CurrentViewInfo\n                {', '''ResultInfo = new CurrentViewInfo
                {
                    DocumentSessionId = System.Diagnostics.Process.GetCurrentProcess().Id + ":" + doc.GetHashCode(),
                    DocumentTitle = doc.Title,
                    RevitVersion = app.Application.VersionNumber,''', 1)
    source = source.replace('TaskDialog.Show("error", "获取信息失败");', 'ResultInfo = null;')
path.write_text(source, encoding='utf-8', newline='\n')
path = root / 'commandset/Utils/GeometryUtils.cs'
source = path.read_text(encoding='utf-8-sig')
if 'CurveIntersectResultOption.Detailed' not in source:
    old = '''        var results = new IntersectionResultArray();
        if (line1.Intersect(line2, out results) == SetComparisonResult.Overlap && results.Size > 0)
            return results.get_Item(0).XYZPoint;'''
    new = '''#if REVIT2027_OR_GREATER
        using var result = line1.Intersect(line2, CurveIntersectResultOption.Detailed);
        var overlaps = result.GetOverlaps();
        if (result.Result == SetComparisonResult.Overlap && overlaps.Count > 0)
            return overlaps[0].Point;
#else
''' + old + '''
#endif'''
    if old not in source:
        raise SystemExit('Unexpected upstream geometry code; review the pinned version.')
    source = source.replace(old, new, 1)
path.write_text(source, encoding='utf-8', newline='\n')
print('Prepared the Revit build targets, loopback-only socket and document session metadata.')
