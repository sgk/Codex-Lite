using System.Text.Json;
using CodexLite;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

var root = args.Single();
var scratch = Path.Combine(Path.GetTempPath(), "codex-lite-cloud-tests-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(scratch);
var count = 0;
void Check(bool value, string label)
{
    if (!value) throw new Exception(label);
    count++;
}
try
{
    var receipt = new CodexLite.Services.SubmissionReceiptTracker();
    var now = DateTimeOffset.UtcNow;
    var posts = 0;
    var reads = 0;
    Task<string?> LostResponse() { posts++; throw new TaskCanceledException("response lost after admission"); }
    Task<string?> ReadReceipt() { reads++; return Task.FromResult<string?>(reads >= 3 ? "accepted-run" : null); }
    bool Uncertain(Exception ex) => ex is TaskCanceledException;
    Check(await receipt.ResolveAsync(LostResponse, ReadReceipt, Uncertain, now) is null,
        "lost response remains uncertain, not rejected");
    Check(receipt.NeedsConfirmation && posts == 1 && reads == 0, "only one POST before confirmation");
    Check(!receipt.CanCheck(now.AddSeconds(1)) && receipt.CanCheck(now.AddSeconds(2)), "receipt reads are throttled");
    var earlyCheckRejected = false;
    try { await receipt.ResolveAsync(LostResponse, ReadReceipt, Uncertain, now.AddSeconds(1)); }
    catch (InvalidOperationException) { earlyCheckRejected = true; }
    Check(earlyCheckRejected && posts == 1 && reads == 0, "early confirmation cannot bypass throttling or resend");
    Check(await receipt.ResolveAsync(LostResponse, ReadReceipt, Uncertain, now.AddSeconds(2)) is null,
        "missing receipt is not proof of rejection");
    Check(await receipt.ResolveAsync(LostResponse, ReadReceipt, Uncertain, now.AddSeconds(7)) is null,
        "late admission still uses read-only confirmation");
    Check(await receipt.ResolveAsync(LostResponse, ReadReceipt, Uncertain, now.AddSeconds(12)) == "accepted-run",
        "late acceptance resolves to the original run");
    Check(posts == 1 && reads == 3, "confirmation never resends content");
    var unavailable = new CodexLite.Services.SubmissionReceiptTracker();
    await unavailable.ResolveAsync<string>(() => Task.FromResult<string?>(null), ReadReceipt, Uncertain, now);
    for (var i = 0; i < 12; i++)
    {
        try
        {
            await unavailable.ResolveAsync<string>(LostResponse, () => throw new IOException("offline"), Uncertain, now.AddSeconds(2 + i * 5));
        }
        catch (IOException) { }
    }
    Check(unavailable.Exhausted && !unavailable.CanCheck(now.AddHours(1)), "receipt errors have a bounded retry count");
    unavailable.ResetChecks();
    Check(await unavailable.ResolveAsync(LostResponse, ReadReceipt, Uncertain, now.AddHours(1)) == "accepted-run"
        && posts == 1, "manual confirmation does not return to POST");
    var rejected = new CodexLite.Services.SubmissionReceiptTracker();
    var rejectedThrown = false;
    try { await rejected.ResolveAsync<string>(() => throw new InvalidOperationException("explicit rejection"), ReadReceipt, Uncertain, now); }
    catch (InvalidOperationException) { rejectedThrown = true; }
    Check(rejectedThrown && !rejected.NeedsConfirmation, "explicit rejection remains a real send error");
    var path = Path.Combine(scratch, "cloud-sync.json");
    Check(CloudSyncConfiguration.Load(path) is null, "missing config disables sync");
    File.WriteAllText(path, "{\"enabled\":false}");
    var disabled = CloudSyncConfiguration.Load(path)!;
    Check(!disabled.Enabled && !disabled.RestoreEnabled(true, "previous"), "disabled configuration wins over preference");
    var fixture = new CloudSyncConfiguration(true,
        new FirebaseConnection("test-project", "test-api-key", "test-app"),
        new CloudOAuthConfiguration("test-client.apps.googleusercontent.com", "test-secret"));
    File.WriteAllText(path, JsonSerializer.Serialize(fixture, new JsonSerializerOptions(JsonSerializerDefaults.Web)));
    var loaded = CloudSyncConfiguration.Load(path)!;
    Check(loaded == fixture, "explicit configuration loaded");
    Check(!loaded.RestoreEnabled(false, loaded.Identity), "off survives restart");
    Check(!loaded.RestoreEnabled(true, null), "legacy preference defaults off");
    Check(loaded.RestoreEnabled(true, loaded.Identity), "on restored for same target");
    foreach (var changed in new[] {
        fixture with { Firebase = fixture.Firebase! with { ProjectId = "different" } },
        fixture with { Firebase = fixture.Firebase! with { AppId = "different" } },
        fixture with { OAuth = fixture.OAuth! with { ClientId = "different" } }
    }) Check(!changed.RestoreEnabled(true, fixture.Identity), "changed target must opt in and reauthenticate");
    Check((fixture with { OAuth = fixture.OAuth! with { ClientSecret = "rotated" } }).Identity == fixture.Identity,
        "secret rotation does not change target identity");
    foreach (var invalid in new[] { "null", "{\"enabled\":true}", "{\"enabled\":\"yes\"}", "not-json" })
    {
        File.WriteAllText(path, invalid);
        var failed = false;
        try { CloudSyncConfiguration.Load(path); }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException) { failed = true; }
        Check(failed, "malformed config rejected");
    }
    foreach (var relative in new[] { "MainWindow.xaml.cs", "RemoteSyncFeature.cs", "Services/GoogleOAuthClient.cs", "Services/RemoteAgentClient.cs", "Services/DaemonClient.cs" })
    {
        var source = File.ReadAllText(Path.Combine(root, "windows/CodexLite", relative));
        var errors = CSharpSyntaxTree.ParseText(source).GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error).ToArray();
        Check(errors.Length == 0, relative + ": " + string.Join("; ", errors.Select(d => d.ToString())));
    }
    var start = new System.Diagnostics.ProcessStartInfo("wsl.exe")
    {
        UseShellExecute = false, RedirectStandardInput = true, RedirectStandardOutput = true,
        RedirectStandardError = true, CreateNoWindow = true
    };
    foreach (var arg in new[] { "-d", "Ubuntu-24.04", "--exec", "/usr/bin/node", "-e",
        "process.stdin.on('end',()=>process.exit(0));process.stdin.resume();setInterval(()=>{},1000);console.log('ready');" }) start.ArgumentList.Add(arg);
    using (var child = System.Diagnostics.Process.Start(start)!)
    {
        var stopped = false;
        try
        {
            var line = await child.StandardOutput.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(10));
            Check(line == "ready", "test WSL child started: " + (line ?? await child.StandardError.ReadToEndAsync()));
            var agent = new CodexLite.Services.RemoteAgentClient();
            typeof(CodexLite.Services.RemoteAgentClient).GetField("_process", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!.SetValue(agent, child);
            await agent.StopAsync();
            stopped = true;
            Check(!agent.IsRunning, "desktop stop completes via EOF without killing a process tree");
        }
        finally { if (!stopped && !child.HasExited) child.Kill(entireProcessTree: true); }
    }
    Console.WriteLine($"Cloud sync configuration: {count} checks passed (Windows client not built or launched).");
}
finally { Directory.Delete(scratch, true); }

// The harness compiles service code, without starting the WPF application or OAuth.
namespace CodexLite
{
    internal static class App
    {
        public static Task<string> WaitForOAuthCallbackAsync(CancellationToken cancellationToken) => throw new NotSupportedException();
    }
}
