using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using Newtonsoft.Json.Linq;
using RevitMCPSDK.API.Interfaces;

namespace Sonne.Revit;

public sealed class GetElementCommand : ElementCommand
{
    public override string CommandName => "sonne_get_element";
    public GetElementCommand(UIApplication app) : base(false) { }
}

public sealed class EditElementCommand : ElementCommand
{
    public override string CommandName => "sonne_edit_element";
    public EditElementCommand(UIApplication app) : base(true) { }
}

public abstract class ElementCommand : IRevitCommand
{
    public abstract string CommandName { get; }
    private readonly ElementHandler handler;
    private readonly ExternalEvent externalEvent;
    private readonly object gate = new();
    private readonly int timeout;

    protected ElementCommand(bool write) : this(write ? 1 : 0) { }
    protected ElementCommand(int kind)
    {
        timeout = kind == 4 ? 110000 : 15000;
        handler = new ElementHandler(kind);
        externalEvent = ExternalEvent.Create(handler);
    }

    public object Execute(JObject parameters, string requestId)
    {
        lock (gate)
        {
            if (handler.Current is { Finished: false }) throw new InvalidOperationException("A previous Revit request is still pending. Inspect the model before retrying.");
            var job = new ElementJob((JObject)parameters.DeepClone());
            handler.Current = job;
            if (externalEvent.Raise() != ExternalEventRequest.Accepted)
            {
                job.Cancelled = true;
                job.Finished = true;
                throw new InvalidOperationException("Revit is busy. The request was not accepted.");
            }
            if (!job.Done.Wait(timeout))
            {
                job.Cancelled = true;
                throw new TimeoutException("Revit did not complete the request in time. Do not repeat an edit without reading the element first.");
            }
            if (job.Error != null) throw new InvalidOperationException(job.Error);
            return job.Result;
        }
    }
}

internal sealed class ElementJob(JObject args)
{
    public readonly JObject Args = args;
    public readonly ManualResetEventSlim Done = new(false);
    public volatile bool Cancelled;
    public volatile bool Finished;
    public object Result;
    public string Error;
}

internal sealed class ElementHandler(int kind) : IExternalEventHandler
{
    public volatile ElementJob Current;
    public string GetName() => "Sonne element/parameter procedure " + kind;
    private static string ModelId(Document doc) => Environment.ProcessId + ":" + doc.GetHashCode();
    private static void CheckDeadline(ElementJob job)
    {
        if (job.Cancelled || (job.Args.Value<long?>("_sonneDeadline") ?? 0) < DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()) throw new InvalidOperationException("Request expired before commit; the transaction was cancelled.");
    }
    internal static object Raw(Parameter p) => p.StorageType switch
    {
        StorageType.String => p.AsString(),
        StorageType.Integer => p.AsInteger(),
        StorageType.Double => p.AsDouble(),
        StorageType.ElementId => p.AsElementId().Value,
        _ => null
    };
    private static XYZ Anchor(Element element)
    {
        var box = element.get_BoundingBox(null) ?? throw new InvalidOperationException("Element has no model bounding box.");
        return (box.Min + box.Max) / 2;
    }
    private static double[] Mm(XYZ point) => [point.X * 304.8, point.Y * 304.8, point.Z * 304.8];
    private static XYZ Vector(JToken token)
    {
        if (token is not JArray a || a.Count != 3) throw new ArgumentException("Expected three coordinates in millimetres.");
        var values = a.Select(v => v.Value<double>()).ToArray();
        if (values.Any(v => !double.IsFinite(v))) throw new ArgumentException("Coordinates must be finite.");
        return new XYZ(values[0] / 304.8, values[1] / 304.8, values[2] / 304.8);
    }
    private static object Inspect(Element element)
    {
        var box = element.get_BoundingBox(null);
        return new
        {
            id = element.Id.Value, uniqueId = element.UniqueId, name = element.Name,
            category = element.Category?.Name, pinned = element.Pinned,
            anchor_mm = box == null ? null : Mm((box.Min + box.Max) / 2),
            parameters = element.Parameters.Cast<Parameter>().Select(p => new
            {
                id = p.Id.Value.ToString(), name = p.Definition.Name, storageType = p.StorageType.ToString(),
                readOnly = p.IsReadOnly, value = Raw(p), displayValue = p.AsValueString()
            }).ToArray()
        };
    }

    public void Execute(UIApplication app)
    {
        var job = Current;
        try
        {
            CheckDeadline(job);
            var doc = app.ActiveUIDocument?.Document ?? throw new InvalidOperationException("No document is open.");
            if (ModelId(doc) != job.Args.Value<string>("_sonneModelId")) throw new InvalidOperationException("The active document changed. List Revit sessions again.");
            if (kind == 2) { job.Result = ParameterProcedures.Audit(doc, app.ActiveUIDocument, job.Args, () => CheckDeadline(job)); return; }
            if (kind == 3) { job.Result = ParameterProcedures.Bulk(doc, job.Args, () => CheckDeadline(job)); return; }
            if (kind == 4) { job.Result = RoomPublication.Read(app.Application, job.Args, () => CheckDeadline(job)); return; }
            var element = doc.GetElement(job.Args.Value<string>("element_unique_id")) ?? throw new InvalidOperationException("Element not found in the selected document.");
            if (kind == 0) { job.Result = Inspect(element); return; }
            if (doc.IsReadOnly || doc.IsModifiable) throw new InvalidOperationException("The document is read-only or another transaction is active.");
            var operation = job.Args.Value<string>("operation");
            using var transaction = new Transaction(doc, "Sonne: " + operation);
            transaction.Start();
            transaction.SetFailureHandlingOptions(transaction.GetFailureHandlingOptions().SetFailuresPreprocessor(new RollBackFailures()).SetClearAfterRollback(true));
            if (operation == "set_parameter")
            {
                string id = job.Args.Value<string>("parameter_id");
                var parameter = element.Parameters.Cast<Parameter>().SingleOrDefault(p => p.Id.Value.ToString() == id) ?? throw new InvalidOperationException("Parameter not found. Inspect the element first.");
                if (parameter.IsReadOnly) throw new InvalidOperationException("Parameter is read-only.");
                var actual = Raw(parameter);
                var expected = job.Args["expected_value"] ?? throw new ArgumentException("expected_value is required, including null for an empty parameter.");
                if (!JToken.DeepEquals(actual == null ? JValue.CreateNull() : JToken.FromObject(actual), expected)) throw new InvalidOperationException("The parameter changed since it was read. Inspect it again.");
                var value = job.Args["value"] ?? throw new ArgumentException("value is required.");
                bool changed = parameter.StorageType switch
                {
                    StorageType.String when value.Type == JTokenType.String => parameter.Set(value.Value<string>()),
                    StorageType.Integer when value.Type == JTokenType.Integer => parameter.Set(value.Value<int>()),
                    StorageType.ElementId when value.Type == JTokenType.Integer => parameter.Set(new ElementId(value.Value<long>())),
                    // Revit parses units from the supplied display string; raw internal doubles are never guessed.
                    StorageType.Double when value.Type == JTokenType.String => parameter.SetValueString(value.Value<string>()),
                    _ => throw new ArgumentException("Use text for String, an integer ID for Integer/ElementId, and a value with units for Double.")
                };
                if (!changed) throw new InvalidOperationException("Revit rejected the parameter value.");
            }
            else if (operation == "move")
            {
                if (Anchor(element).DistanceTo(Vector(job.Args["expected_anchor_mm"])) > 0.01 / 304.8) throw new InvalidOperationException("Element position changed since inspection. Read it again.");
                ElementTransformUtils.MoveElement(doc, element.Id, Vector(job.Args["translation_mm"]));
            }
            else throw new ArgumentException("Only set_parameter and move are enabled.");
            CheckDeadline(job);
            if (transaction.Commit() != TransactionStatus.Committed) throw new InvalidOperationException("Revit rolled back the transaction. No successful edit was confirmed.");
            job.Result = new { status = "committed", operation, element_unique_id = element.UniqueId, operation_id = job.Args.Value<string>("_sonneOperationId") };
        }
        catch (Exception error) { job.Error = error.Message; }
        finally { job.Finished = true; job.Done.Set(); }
    }
}

internal sealed class RollBackFailures : IFailuresPreprocessor
{
    public FailureProcessingResult PreprocessFailures(FailuresAccessor failures) => failures.GetFailureMessages().Count > 0 ? FailureProcessingResult.ProceedWithRollBack : FailureProcessingResult.Continue;
}
