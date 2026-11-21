using Autodesk.Revit.ApplicationServices;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Architecture;
using Autodesk.Revit.UI;
using Newtonsoft.Json.Linq;
using System.IO;
using System.Security.Cryptography;
using System.Text.RegularExpressions;

namespace Sonne.Revit;

public sealed class ReadRoomPublicationCommand : ElementCommand
{
    public override string CommandName => "sonne_extract_room_publication";
    public ReadRoomPublicationCommand(UIApplication app) : base(4) { }
}

internal static class RoomPublication
{
    internal static object Read(Application app, JObject args, Action check)
    {
        var digest = args.Value<string>("digest") ?? "";
        if (!Regex.IsMatch(digest, @"\A[a-f0-9]{64}\z")) throw new ArgumentException("Expected the saved publication's SHA-256.");
        var id = args.Value<string>("schedule_id");
        if (string.IsNullOrWhiteSpace(id)) throw new ArgumentException("Configure an approved room schedule UniqueId.");
        var directory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Sonne", "RevitPublications", "inbox");
        var path = Path.Combine(directory, digest + ".rvt");
        if (!File.Exists(path) || (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
            throw new InvalidOperationException("The saved RVT is not available in the local publication inbox.");
        // Keep a read handle while extracting so the verified bytes cannot be replaced.
        using var file = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        if (!StringComparer.OrdinalIgnoreCase.Equals(Convert.ToHexString(SHA256.HashData(file)), digest))
            throw new InvalidOperationException("The RVT does not match the publication digest.");
        check();
        using var info = BasicFileInfo.Extract(path);
        using var options = new OpenOptions();
        if (info.IsWorkshared) options.DetachFromCentralOption = DetachFromCentralOption.DetachAndDiscardWorksets;
        var doc = app.OpenDocumentFile(ModelPathUtils.ConvertUserVisiblePathToModelPath(path), options);
        try
        {
            check();
            if (doc.GetElement(id) is not ViewSchedule view || view.IsTemplate ||
                !view.Name.EndsWith("-Sonne", StringComparison.Ordinal) ||
                view.Definition.CategoryId.Value != (long)BuiltInCategory.OST_Rooms ||
                !view.Definition.IsItemized || view.Definition.IsKeySchedule || view.Definition.IncludeLinkedFiles)
                throw new InvalidOperationException("The approved schedule must be an itemized, host-only room schedule named with the -Sonne suffix.");
            var columns = view.Definition.GetFieldOrder().Select(view.Definition.GetField)
                .Where(field => !field.IsHidden).Select(field => field.ParameterId.Value).ToHashSet();
            if (!columns.Contains((long)BuiltInParameter.ROOM_NUMBER) || !columns.Contains((long)BuiltInParameter.ROOM_AREA))
                throw new InvalidOperationException("Show the Number and Area columns in the approved schedule.");
            // A later or unavailable linked model can change the area of host rooms.
            if (new FilteredElementCollector(doc).OfClass(typeof(RevitLinkType)).Any(link =>
                link.get_Parameter(BuiltInParameter.WALL_ATTR_ROOM_BOUNDING)?.AsInteger() == 1))
                throw new InvalidOperationException("The archived host must supply its own room boundaries.");
            var rooms = new List<object>();
            // The view-scoped collector respects this schedule's eligibility and filters.
            foreach (var room in new FilteredElementCollector(doc, view.Id).OfCategory(BuiltInCategory.OST_Rooms).WhereElementIsNotElementType().OfType<Room>())
            {
                check();
                if (rooms.Count >= 200) throw new InvalidOperationException("Limit the pilot schedule to 200 rooms.");
                rooms.Add(new { id = room.UniqueId, number = room.Number?.Trim() ?? "", name = room.get_Parameter(BuiltInParameter.ROOM_NAME)?.AsString() ?? "",
                    area_m2 = room.Location != null && room.Area > 0 ? UnitUtils.ConvertFromInternalUnits(room.Area, UnitTypeId.SquareMeters) : (double?)null });
            }
            return new { digest, schedule_id = id, schedule_name = view.Name, rooms };
        }
        finally
        {
            doc.Close(false);
        }
    }
}
