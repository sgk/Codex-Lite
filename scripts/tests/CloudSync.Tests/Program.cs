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
    foreach (var relative in new[] { "MainWindow.xaml.cs", "RemoteSyncFeature.cs", "Services/GoogleOAuthClient.cs", "Services/RemoteAgentClient.cs" })
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
