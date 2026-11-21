using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using Newtonsoft.Json.Linq;
using RevitMCPSDK.API.Interfaces;

namespace Sonne.Upstream;

// Keep the upstream handlers and schemas, but guard their ExternalEvent boundary.
public abstract class ExternalEventCommandBase : IRevitCommand
{
    public abstract string CommandName { get; }
    public abstract object Execute(JObject parameters, string requestId);
    protected IExternalEventHandler Handler { get; }
    private readonly GuardedHandler guarded;
    private readonly ExternalEvent externalEvent;
    private static readonly object Gate = new();
    private static readonly HashSet<string> Seen = new();
    private static readonly Queue<string> SeenOrder = new();
    private static readonly HashSet<string> Reads = new() { "get_current_view_info", "get_current_view_elements", "get_selected_elements", "get_available_family_types", "ai_element_filter", "analyze_model_statistics", "export_room_data", "get_material_quantities" };

    protected ExternalEventCommandBase(IExternalEventHandler handler, UIApplication app)
    {
        Handler = handler;
        guarded = new GuardedHandler(handler);
        externalEvent = ExternalEvent.Create(guarded);
    }

    protected IDisposable BeginSonneRequest(JObject args, string requestId)
    {
        if (!Monitor.TryEnter(Gate, 1000)) throw new InvalidOperationException("Another MCP command is being dispatched.");
        try
        {
            if (guarded.Current is { Finished: false }) throw new InvalidOperationException("Previous request is still pending; inspect before retrying.");
            var heartbeat = CommandName == "get_current_view_info" && args["_sonneModelId"] == null;
            var model = args.Value<string>("_sonneModelId");
            var deadline = args.Value<long?>("_sonneDeadline") ?? 0;
            var operation = args.Value<string>("_sonneOperationId");
            if (!heartbeat && (string.IsNullOrEmpty(model) || string.IsNullOrEmpty(operation) || deadline <= DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()))
                throw new InvalidOperationException("Missing document guard or expired request.");
            if (!heartbeat && !Seen.Add(operation)) throw new InvalidOperationException("Operation already received; no replay is allowed.");
            if (!heartbeat)
            {
                SeenOrder.Enqueue(operation);
                if (SeenOrder.Count > 4096) Seen.Remove(SeenOrder.Dequeue());
            }
            var job = new Job(model, heartbeat ? DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() + 15000 : deadline, !Reads.Contains(CommandName));
            guarded.Current = job;
            return new Scope(job);
        }
        catch { Monitor.Exit(Gate); throw; }
    }

    protected bool RaiseAndWaitForCompletion(int timeoutMilliseconds)
    {
        var job = guarded.Current ?? throw new InvalidOperationException("Missing guarded request.");
        job.Dispatched = true;
        if (externalEvent.Raise() != ExternalEventRequest.Accepted)
        {
            job.Finished = true;
            throw new InvalidOperationException("Revit did not accept the request.");
        }
        if (!job.Done.Wait(Math.Min(timeoutMilliseconds, 22000)))
        {
            job.Cancelled = true;
            throw new TimeoutException("Revit request timed out; inspect before retrying.");
        }
        if (job.Error != null) throw new InvalidOperationException(job.Error);
        return true;
    }

    private sealed class Scope(Job job) : IDisposable
    {
        public void Dispose() { if (!job.Finished) job.Cancelled = true; if (!job.Dispatched) job.Finished = true; Monitor.Exit(Gate); }
    }
    private sealed class Job(string model, long deadline, bool write)
    {
        public readonly string Model = model;
        public readonly long Deadline = deadline;
        public readonly bool Write = write;
        public readonly ManualResetEventSlim Done = new(false);
        public volatile bool Cancelled, Finished, Dispatched;
        public string Error;
        public void Check(UIApplication app)
        {
            if (Cancelled || DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() >= Deadline) throw new InvalidOperationException("Request expired; changes were rolled back.");
            if (Model != null && (app.ActiveUIDocument?.Document is not { } doc || Environment.ProcessId + ":" + doc.GetHashCode() != Model))
                throw new InvalidOperationException("Active document changed; request rejected.");
        }
    }
    private sealed class GuardedHandler(IExternalEventHandler inner) : IExternalEventHandler
    {
        public volatile Job Current;
        public string GetName() => "Sonne guarded: " + inner.GetName();
        public void Execute(UIApplication app)
        {
            var job = Current;
            try
            {
                job.Check(app);
                var doc = app.ActiveUIDocument?.Document;
                if (job.Write && (doc == null || doc.IsReadOnly || doc.IsModifiable)) throw new InvalidOperationException("Document is not available for a transaction.");
                using var group = job.Write ? new TransactionGroup(doc, "Sonne MCP") : null;
                group?.Start();
                inner.Execute(app);
                job.Check(app);
                foreach (var property in inner.GetType().GetProperties().Where(p => p.Name is "Result" or "ResultInfo" or "TaggingResults" or "ColoringResults" or "IsSuccess"))
                {
                    var result = property.GetValue(inner);
                    if (result == null) continue;
                    var data = JToken.FromObject(result);
                    var success = data is JObject obj ? obj.GetValue("success", StringComparison.OrdinalIgnoreCase) : data;
                    if (success?.Type == JTokenType.Boolean && !success.Value<bool>()) throw new InvalidOperationException("Upstream operation failed; model transaction group rolled back. " + data.ToString(Newtonsoft.Json.Formatting.None));
                }
                if (group != null && group.Assimilate() != TransactionStatus.Committed) throw new InvalidOperationException("Transaction group was not committed.");
            }
            catch (Exception error) { job.Error = error.Message; }
            finally { job.Finished = true; job.Done.Set(); }
        }
    }

    public static void ConfigureTransaction(Transaction transaction)
    {
        transaction.SetFailureHandlingOptions(transaction.GetFailureHandlingOptions().SetFailuresPreprocessor(new SilentFailures()).SetClearAfterRollback(true));
    }
    public static TaskDialogResult ReportError(string title, string message) => throw new InvalidOperationException(title + ": " + message);
    private sealed class SilentFailures : IFailuresPreprocessor
    {
        public FailureProcessingResult PreprocessFailures(FailuresAccessor failures)
        {
            foreach (var failure in failures.GetFailureMessages())
            {
                if (failure.GetSeverity() != FailureSeverity.Warning) return FailureProcessingResult.ProceedWithRollBack;
                failures.DeleteWarning(failure);
            }
            return FailureProcessingResult.Continue;
        }
    }
}
