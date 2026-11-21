using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using Newtonsoft.Json.Linq;

namespace Sonne.Revit;

public sealed class AuditParametersCommand : ElementCommand
{
    public override string CommandName => "sonne_audit_parameters";
    public AuditParametersCommand(UIApplication app) : base(2) { }
}
public sealed class BulkSetParametersCommand : ElementCommand
{
    public override string CommandName => "sonne_bulk_set_parameters";
    public BulkSetParametersCommand(UIApplication app) : base(3) { }
}

internal static class ParameterProcedures
{
    private sealed record Row(string Uid, long Id, string Name, long ParameterId, JToken Value, bool HasValue);
    private sealed record Plan(string Model, DateTimeOffset At, string Parameter, List<Row> Rows);
    // Bound cached previews; audits still scan the entire requested scope.
    private static readonly Dictionary<string, Plan> Plans = new();
    private static string Model(Document doc) => Environment.ProcessId + ":" + doc.GetHashCode();
    private static JToken Value(Parameter p) => JToken.FromObject(ElementHandler.Raw(p) ?? JValue.CreateNull());
    private static Parameter Resolve(Element element, string key)
    {
        if (long.TryParse(key, out var id)) return element.Parameters.Cast<Parameter>().SingleOrDefault(p => p.Id.Value == id);
        if (Guid.TryParse(key, out var guid)) return element.get_Parameter(guid);
        if (Enum.TryParse<BuiltInParameter>(key, out var builtIn) && Enum.IsDefined(builtIn)) return element.get_Parameter(builtIn);
        var parameters = element.GetParameters(key);
        if (parameters.Count > 1) throw new InvalidOperationException("Ambiguous parameter name; use its ID or shared GUID.");
        return parameters.SingleOrDefault();
    }
    private static JToken Desired(Document doc, Parameter p, JToken value)
    {
        switch (p.StorageType)
        {
            case StorageType.String when value?.Type == JTokenType.String: return value.DeepClone();
            case StorageType.Integer when value?.Type == JTokenType.Integer: return new JValue(value.Value<int>());
            case StorageType.ElementId when value?.Type == JTokenType.Integer: return new JValue(value.Value<long>());
            case StorageType.Double when value?.Type == JTokenType.String:
                if (UnitFormatUtils.TryParse(doc.GetUnits(), p.Definition.GetDataType(), value.Value<string>(), out var number) && double.IsFinite(number)) return new JValue(number);
                throw new ArgumentException("Cannot parse the number and its units.");
            default: throw new ArgumentException("Use text for String, an integer for Integer/ElementId, and text with units for Double.");
        }
    }
    private static bool Equal(Parameter p, JToken actual, JToken expected) => p.StorageType == StorageType.Double
        ? Math.Abs(actual.Value<double>() - expected.Value<double>()) <= 1e-9 * Math.Max(1, Math.Abs(expected.Value<double>()))
        : JToken.DeepEquals(actual, expected);
    private static IEnumerable<Element> Elements(Document doc, UIDocument ui, JObject args)
    {
        var scope = args.Value<string>("scope") ?? "selection";
        if (scope == "selection")
        {
            var ids = ui.Selection.GetElementIds();
            if (ids.Count == 0) throw new InvalidOperationException("Select elements in Revit first, or explicitly choose category/model/elements scope.");
            return ids.Select(doc.GetElement);
        }
        if (scope == "elements")
        {
            var ids = args["element_ids"]?.ToObject<string[]>();
            if (ids == null || ids.Length == 0 || ids.Length > 10000) throw new ArgumentException("Provide 1-10000 element UniqueIds or numeric IDs.");
            return ids.Distinct().Select(id => (long.TryParse(id, out var number) ? doc.GetElement(new ElementId(number)) : doc.GetElement(id)) ?? throw new InvalidOperationException("An explicitly requested element is missing: " + id));
        }
        var collector = new FilteredElementCollector(doc).WhereElementIsNotElementType();
        if (scope == "category")
        {
            if (!Enum.TryParse<BuiltInCategory>(args.Value<string>("category"), out var category) || !Enum.IsDefined(category) || category == BuiltInCategory.INVALID) throw new ArgumentException("Choose an exact built-in category such as OST_DuctCurves.");
            collector.OfCategory(category);
        }
        else if (scope != "model") throw new ArgumentException("Unknown audit scope.");
        return collector;
    }
    internal static object Audit(Document doc, UIDocument ui, JObject args, Action check)
    {
        var key = args.Value<string>("parameter");
        if (string.IsNullOrWhiteSpace(key)) throw new ArgumentException("Choose one parameter name, ID, built-in name or shared GUID.");
        var rule = args.Value<string>("rule") ?? "empty";
        if (!new[] { "empty", "equals", "not_equals", "all" }.Contains(rule)) throw new ArgumentException("Unknown audit rule.");
        if (rule is "equals" or "not_equals" && args["expected"] == null) throw new ArgumentException("This rule requires expected.");
        var rows = new List<Row>();
        var sample = new List<object>();
        int scanned = 0, found = 0, missing = 0, ambiguous = 0, readOnly = 0, errors = 0, matching = 0, editable = 0, typesSkipped = 0, sampleCandidates = 0;
        Element first = null;
        foreach (var element in Elements(doc, ui, args).DistinctBy(e => e.UniqueId))
        {
            check();
            if (element is ElementType) { typesSkipped++; continue; }
            first ??= element; scanned++;
            Parameter p;
            try { p = Resolve(element, key); }
            catch (InvalidOperationException) { ambiguous++; sampleCandidates++; if (sample.Count < 12) sample.Add(new { id = element.Id.Value, name = element.Name, status = "ambiguous" }); continue; }
            if (p == null) { missing++; sampleCandidates++; if (sample.Count < 12) sample.Add(new { id = element.Id.Value, name = element.Name, status = "missing" }); continue; }
            found++;
            if (p.IsReadOnly) readOnly++;
            var current = Value(p);
            bool matches;
            try
            {
                var wanted = rule is "equals" or "not_equals" ? Desired(doc, p, args["expected"]) : null;
                matches = rule switch
                {
                    "empty" => !p.HasValue || current.Type == JTokenType.Null || p.StorageType == StorageType.String && string.IsNullOrWhiteSpace(p.AsString()),
                    "all" => true,
                    "equals" => p.HasValue && Equal(p, current, wanted),
                    _ => !p.HasValue || !Equal(p, current, wanted)
                };
            }
            catch (ArgumentException error) { errors++; sampleCandidates++; if (sample.Count < 12) sample.Add(new { id = element.Id.Value, name = element.Name, status = "comparison_error", error = error.Message }); continue; }
            if (matches) matching++;
            var writable = !p.IsReadOnly && p.StorageType != StorageType.None;
            if (matches && writable)
            {
                editable++;
                if (rows.Count <= 10000) rows.Add(new Row(element.UniqueId, element.Id.Value, element.Name, p.Id.Value, current.DeepClone(), p.HasValue));
            }
            if (matches || p.IsReadOnly) { sampleCandidates++; if (sample.Count < 12) sample.Add(new { id = element.Id.Value, uniqueId = element.UniqueId, name = element.Name, parameter_id = p.Id.Value.ToString(), parameter_name = p.Definition.Name, value = current, display_value = p.AsValueString(), storage_type = p.StorageType.ToString(), writable, matches }); }
        }
        check();
        foreach (var old in Plans.Where(pair => DateTimeOffset.UtcNow - pair.Value.At > TimeSpan.FromMinutes(5)).Select(pair => pair.Key).ToArray()) Plans.Remove(old);
        if (Plans.Count >= 32) Plans.Remove(Plans.First().Key);
        string auditId = null;
        if (editable <= 10000)
        {
            auditId = "aud-" + Guid.NewGuid().ToString("N")[..12];
            Plans.Add(auditId, new Plan(Model(doc), DateTimeOffset.UtcNow, key, rows));
        }
        return new
        {
            audit_id = auditId, complete = true, scope = args.Value<string>("scope") ?? "selection", parameter = key, rule,
            scanned, found, missing, ambiguous, read_only = readOnly, comparison_errors = errors, matching, editable_matching = editable, type_elements_skipped = typesSkipped,
            bulk_available = auditId != null && editable > 0, bulk_limit = 10000,
            sample, sample_truncated = sampleCandidates > sample.Count,
            available_parameters = found == 0 && first != null ? first.Parameters.Cast<Parameter>().Take(16).Select(p => new { id = p.Id.Value.ToString(), name = p.Definition.Name, storage_type = p.StorageType.ToString(), writable = !p.IsReadOnly }).ToArray() : null,
            note = "Bulk set changes only editable_matching elements captured by this audit. Missing, ambiguous and read-only parameters are skipped. Audit expires in 5 minutes and is single-use."
        };
    }
    internal static object Bulk(Document doc, JObject args, Action check)
    {
        var id = args.Value<string>("audit_id") ?? "";
        if (!Plans.Remove(id, out var plan) || DateTimeOffset.UtcNow - plan.At > TimeSpan.FromMinutes(5)) throw new InvalidOperationException("Audit expired or already used. Run a fresh audit.");
        if (plan.Model != Model(doc)) throw new InvalidOperationException("Audit belongs to another document.");
        if (doc.IsReadOnly || doc.IsModifiable) throw new InvalidOperationException("Document is not available for editing.");
        var value = args["value"] ?? throw new ArgumentException("A new value is required.");
        var changes = new List<(Parameter Parameter, JToken Desired, Row Row)>();
        foreach (var row in plan.Rows)
        {
            check();
            var element = doc.GetElement(row.Uid) ?? throw new InvalidOperationException("Audited element was deleted. Run a fresh audit.");
            var p = Resolve(element, row.ParameterId.ToString());
            if (p == null || p.IsReadOnly || p.HasValue != row.HasValue || !JToken.DeepEquals(Value(p), row.Value)) throw new InvalidOperationException("Parameter changed since audit on element " + row.Id + ". Entire bulk operation rejected; run a fresh audit.");
            changes.Add((p, Desired(doc, p, value), row));
        }
        var changed = changes.Where(c => c.Parameter.StorageType != StorageType.String && !c.Parameter.HasValue || !Equal(c.Parameter, Value(c.Parameter), c.Desired)).ToArray();
        check();
        if (changed.Length == 0) return new { status = "no_change", updated = 0, unchanged = changes.Count, verified = true, audit_id = id };
        using var group = new TransactionGroup(doc, "Sonne: bulk parameter procedure");
        group.Start();
        using (var transaction = new Transaction(doc, "Sonne: set " + plan.Parameter))
        {
            transaction.Start();
            transaction.SetFailureHandlingOptions(transaction.GetFailureHandlingOptions().SetFailuresPreprocessor(new RollBackFailures()).SetClearAfterRollback(true));
            foreach (var change in changed)
            {
                check();
                bool accepted = change.Parameter.StorageType switch
                {
                    StorageType.String => change.Parameter.Set(value.Value<string>()),
                    StorageType.Integer => change.Parameter.Set(value.Value<int>()),
                    StorageType.ElementId => change.Parameter.Set(new ElementId(value.Value<long>())),
                    StorageType.Double => change.Parameter.SetValueString(value.Value<string>()),
                    _ => false
                };
                if (!accepted) throw new InvalidOperationException("Revit rejected a parameter value; entire bulk operation rolled back.");
            }
            check();
            if (transaction.Commit() != TransactionStatus.Committed) throw new InvalidOperationException("Revit rolled back the bulk transaction.");
        }
        foreach (var change in changes) if (change.Parameter.StorageType != StorageType.String && !change.Parameter.HasValue || !Equal(change.Parameter, Value(change.Parameter), change.Desired)) throw new InvalidOperationException("Readback did not match; entire bulk operation rolled back.");
        check();
        if (group.Assimilate() != TransactionStatus.Committed) throw new InvalidOperationException("Bulk transaction group was not committed.");
        return new { status = "committed", updated = changed.Length, unchanged = changes.Count - changed.Length, verified = true, audit_id = id, parameter = plan.Parameter, value, sample = changed.Take(12).Select(c => new { id = c.Row.Id, uniqueId = c.Row.Uid, before = c.Row.Value, after = Value(c.Parameter) }).ToArray() };
    }
}
