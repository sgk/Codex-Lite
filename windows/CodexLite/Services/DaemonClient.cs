using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using CodexLite.Models;

namespace CodexLite.Services;

public sealed class DaemonClient
{
    private static readonly TimeSpan DaemonReadyTimeout = TimeSpan.FromSeconds(180);
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);
    private HttpClient _http = CreateHttpClient();
    private readonly StringBuilder _daemonErrors = new();
    private Process? _daemonProcess;

    public event Action<string>? StatusChanged;

    public string CodexHomeMode { get; set; } = "auto";

    public async Task<WslEnvironment> ResolveDefaultWslEnvironmentAsync(CancellationToken cancellationToken = default)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = "wsl.exe",
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true
        };
        startInfo.ArgumentList.Add("--");
        startInfo.ArgumentList.Add("bash");
        // Do not use a login shell here. User startup files may run ssh-add and block on passphrase input.
        startInfo.ArgumentList.Add("-c");
        startInfo.ArgumentList.Add("printf '%s\\n%s\\n' \"$WSL_DISTRO_NAME\" \"$HOME\"");
        using var process = Process.Start(startInfo) ?? throw new InvalidOperationException("failed to inspect the default WSL environment");
        var distroName = (await process.StandardOutput.ReadLineAsync(cancellationToken))?.Trim();
        var homePath = (await process.StandardOutput.ReadLineAsync(cancellationToken))?.Trim();
        await process.WaitForExitAsync(cancellationToken);
        if (process.ExitCode != 0 || string.IsNullOrWhiteSpace(distroName) || string.IsNullOrWhiteSpace(homePath))
        {
            var error = await process.StandardError.ReadToEndAsync(cancellationToken);
            throw new InvalidOperationException($"既定のWSL環境を取得できませんでした。{error}");
        }
        return new WslEnvironment(distroName, homePath);
    }

    public async Task<HealthDto?> TryHealthAsync(CancellationToken cancellationToken = default)
    {
        try
        {
            return await _http.GetFromJsonAsync<HealthDto>("/health", JsonOptions, cancellationToken);
        }
        catch
        {
            return null;
        }
    }

    public async Task<HealthDto?> ProbeHealthAsync(CancellationToken cancellationToken = default)
    {
        var baseAddress = _http.BaseAddress;
        if (baseAddress is null)
        {
            return null;
        }

        try
        {
            using var client = CreateHttpClient();
            client.BaseAddress = baseAddress;
            return await client.GetFromJsonAsync<HealthDto>("/health", JsonOptions, cancellationToken);
        }
        catch
        {
            return null;
        }
    }

    public async Task EnsureDaemonAsync(string distroName, CancellationToken cancellationToken = default)
    {
        if (_http.BaseAddress is not null && await TryHealthAsync(cancellationToken) is not null)
        {
            return;
        }

        DaemonReady ready;
        _daemonProcess = StartDaemonProcess(distroName);
        try
        {
            ready = await ReadDaemonReadyAsync(_daemonProcess, cancellationToken);
        }
        catch
        {
            await StopDaemonProcessAsync(_daemonProcess);
            _daemonProcess = null;
            throw;
        }
        _ = DrainDaemonStdoutAsync(_daemonProcess);
        _http.BaseAddress = new Uri($"http://{ready.Host}:{ready.Port}");

        var deadline = DateTimeOffset.UtcNow + TimeSpan.FromSeconds(20);
        while (DateTimeOffset.UtcNow < deadline)
        {
            using var attemptTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(1));
            using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, attemptTimeout.Token);
            if (await TryHealthAsync(linked.Token) is not null)
            {
                return;
            }
            await Task.Delay(200, cancellationToken);
        }

        throw new InvalidOperationException("daemon did not become healthy within 20 seconds");
    }

    public async Task<bool> EnsureHealthyOrRestartAsync(string distroName, CancellationToken healthCheckCancellationToken = default)
    {
        if (await ProbeHealthAsync(healthCheckCancellationToken) is not null)
        {
            return false;
        }

        await StopDaemonProcessAsync(_daemonProcess);
        _daemonProcess = null;
        ResetHttpClient();
        await EnsureDaemonAsync(distroName);
        return true;
    }

    public async Task ShutdownDaemonAsync()
    {
        var process = _daemonProcess;
        _daemonProcess = null;
        if (_http.BaseAddress is not null)
        {
            try
            {
                using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                await PostAsync<object>("/shutdown", new { }, timeout.Token);
            }
            catch
            {
            }
        }

        await StopDaemonProcessAsync(process);
        ResetHttpClient();
    }

    private static HttpClient CreateHttpClient() => new() { Timeout = TimeSpan.FromSeconds(35) };

    private void ResetHttpClient()
    {
        _http.Dispose();
        _http = CreateHttpClient();
    }

    private Process StartDaemonProcess(string distroName)
    {
        lock (_daemonErrors)
        {
            _daemonErrors.Clear();
        }
        var windowsHome = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        var codexHomeExport = CodexHomeExport(windowsHome);
        var appDirectory = AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        var daemonRunnerPath = Path.Combine(appDirectory, "run-daemon.sh");
        var daemonRootPath = Path.Combine(appDirectory, "daemon");
        if (!File.Exists(daemonRunnerPath))
        {
            throw new FileNotFoundException("bundled daemon runner was not found", daemonRunnerPath);
        }
        if (!Directory.Exists(daemonRootPath))
        {
            throw new DirectoryNotFoundException($"bundled daemon directory was not found: {daemonRootPath}");
        }

        var daemonRunnerWslPath = WindowsPathToWslPath(daemonRunnerPath, distroName);
        var daemonRootWslPath = WindowsPathToWslPath(daemonRootPath, distroName);
        var command = codexHomeExport
            + $"CODEX_LITE_PORT=0 CODEX_LITE_DAEMON_DIR={ShellQuote(daemonRootWslPath)} exec bash {ShellQuote(daemonRunnerWslPath)}";
        var startInfo = new ProcessStartInfo
        {
            FileName = "wsl.exe",
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true
        };
        startInfo.ArgumentList.Add("-d");
        startInfo.ArgumentList.Add(distroName);
        startInfo.ArgumentList.Add("--");
        startInfo.ArgumentList.Add("bash");
        // The daemon itself must start without login-shell side effects. Codex child process env is captured separately.
        startInfo.ArgumentList.Add("-c");
        startInfo.ArgumentList.Add(command);

        var process = Process.Start(startInfo) ?? throw new InvalidOperationException("failed to start daemon process");
        process.ErrorDataReceived += (_, e) =>
        {
            if (e.Data is not null)
            {
                lock (_daemonErrors)
                {
                    _daemonErrors.AppendLine(e.Data);
                }
                PublishStatusForDaemonLine(e.Data);
                Debug.WriteLine(e.Data);
            }
        };
        process.BeginErrorReadLine();
        return process;
    }

    private string CodexHomeExport(string windowsHome)
    {
        var mode = CodexHomeMode is "windows" or "wsl" ? CodexHomeMode : "auto";
        if (string.IsNullOrWhiteSpace(windowsHome))
        {
            return "";
        }

        var windowsCodexHome = Path.Combine(windowsHome, ".codex");
        var useWindows = mode == "windows" || (mode == "auto" && Directory.Exists(windowsCodexHome));
        return useWindows
            ? $"CODEX_LITE_CODEX_HOME=$(wslpath -u {ShellQuote(windowsCodexHome)}); export CODEX_LITE_CODEX_HOME; "
            : "";
    }

    private static string ShellQuote(string value) => $"'{value.Replace("'", "'\\''")}'";

    private static string WindowsPathToWslPath(string path, string distroName)
    {
        var normalized = Path.GetFullPath(path).Replace('\\', '/');
        var wslLocalhostPrefix = $"//wsl.localhost/{distroName}/";
        var wslLegacyPrefix = $"//wsl$/{distroName}/";
        if (normalized.StartsWith(wslLocalhostPrefix, StringComparison.OrdinalIgnoreCase))
        {
            return "/" + normalized[wslLocalhostPrefix.Length..];
        }
        if (normalized.StartsWith(wslLegacyPrefix, StringComparison.OrdinalIgnoreCase))
        {
            return "/" + normalized[wslLegacyPrefix.Length..];
        }
        if (normalized.Length >= 3 && char.IsAsciiLetter(normalized[0]) && normalized[1] == ':' && normalized[2] == '/')
        {
            return $"/mnt/{char.ToLowerInvariant(normalized[0])}/{normalized[3..]}";
        }
        throw new InvalidOperationException($"cannot convert daemon path to WSL path: {path}");
    }

    private async Task<DaemonReady> ReadDaemonReadyAsync(Process process, CancellationToken cancellationToken)
    {
        using var timeout = new CancellationTokenSource(DaemonReadyTimeout);
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, timeout.Token);
        try
        {
            while (!linked.IsCancellationRequested)
            {
                var line = await process.StandardOutput.ReadLineAsync(linked.Token);
                if (line is null)
                {
                    break;
                }

                if (TryParseReady(line, out var ready))
                {
                    return ready;
                }

                lock (_daemonErrors)
                {
                    _daemonErrors.AppendLine(line);
                }
                PublishStatusForDaemonLine(line);
            }
        }
        catch (OperationCanceledException) when (timeout.IsCancellationRequested && !cancellationToken.IsCancellationRequested)
        {
        }

        lock (_daemonErrors)
        {
            throw new InvalidOperationException($"daemon did not report its port within {DaemonReadyTimeout.TotalSeconds:0} seconds. {_daemonErrors}");
        }
    }

    private static async Task DrainDaemonStdoutAsync(Process process)
    {
        try
        {
            while (!process.HasExited)
            {
                var line = await process.StandardOutput.ReadLineAsync();
                if (line is null)
                {
                    return;
                }
            }
        }
        catch
        {
        }
    }

    private void PublishStatusForDaemonLine(string line)
    {
        var status = DaemonStatusFromLine(line);
        if (status is not null)
        {
            StatusChanged?.Invoke(status);
        }
    }

    private static string? DaemonStatusFromLine(string line)
    {
        if (line.Contains("codex-lite-daemon-setup:start", StringComparison.Ordinal))
        {
            return "初期設定中...";
        }
        if (line.Contains("codex-lite-daemon-setup:finish", StringComparison.Ordinal))
        {
            return "デーモンを起動中...";
        }
        return IsPipSetupLine(line) ? "初期設定中..." : null;
    }

    private static bool IsPipSetupLine(string line)
    {
        return line.Contains("Requirement already satisfied", StringComparison.OrdinalIgnoreCase)
            || line.Contains("Collecting ", StringComparison.OrdinalIgnoreCase)
            || line.Contains("Installing ", StringComparison.OrdinalIgnoreCase)
            || line.Contains("Building wheel", StringComparison.OrdinalIgnoreCase)
            || line.Contains("Building wheels", StringComparison.OrdinalIgnoreCase)
            || line.Contains("Preparing metadata", StringComparison.OrdinalIgnoreCase)
            || line.Contains("Getting requirements", StringComparison.OrdinalIgnoreCase)
            || line.Contains("Successfully installed", StringComparison.OrdinalIgnoreCase)
            || line.Contains("Processing ", StringComparison.OrdinalIgnoreCase);
    }

    private static async Task StopDaemonProcessAsync(Process? process)
    {
        if (process is null || process.HasExited)
        {
            return;
        }

        try
        {
            await process.StandardInput.DisposeAsync();
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            await process.WaitForExitAsync(timeout.Token);
        }
        catch
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        finally
        {
            process.Dispose();
        }
    }

    private static bool TryParseReady(string line, out DaemonReady ready)
    {
        try
        {
            var message = JsonSerializer.Deserialize<DaemonReadyMessage>(line, JsonOptions);
            if (message?.Event == "ready" && !string.IsNullOrWhiteSpace(message.Host) && message.Port > 0)
            {
                ready = new DaemonReady(message.Host, message.Port);
                return true;
            }
        }
        catch (JsonException)
        {
        }

        ready = default;
        return false;
    }

    public Task<List<ProjectDto>?> ListProjectsAsync(CancellationToken cancellationToken = default) =>
        _http.GetFromJsonAsync<List<ProjectDto>>("/projects", JsonOptions, cancellationToken);

    public Task<List<ProjectCandidateDto>?> ListProjectCandidatesAsync(CancellationToken cancellationToken = default) =>
        _http.GetFromJsonAsync<List<ProjectCandidateDto>>("/project-candidates", JsonOptions, cancellationToken);

    public Task<UsageCapacityDto?> GetUsageCapacityAsync(CancellationToken cancellationToken = default) =>
        _http.GetFromJsonAsync<UsageCapacityDto>("/usage/capacity", JsonOptions, cancellationToken);

    public Task<List<ProjectDto>?> ImportProjectCandidatesAsync(IEnumerable<string> paths, CancellationToken cancellationToken = default) =>
        PostAsync<List<ProjectDto>>("/project-candidates/import", new { paths }, cancellationToken);

    public Task<ProjectDto?> CreateProjectAsync(string path, string? name, CancellationToken cancellationToken = default) =>
        PostAsync<ProjectDto>("/projects", new { path, name }, cancellationToken);

    public Task<ProjectDto?> RenameProjectAsync(string projectId, string name, CancellationToken cancellationToken = default) =>
        PatchAsync<ProjectDto>($"/projects/{projectId}", new { name }, cancellationToken);

    public async Task DeleteProjectAsync(string projectId, CancellationToken cancellationToken = default)
    {
        using var response = await _http.DeleteAsync($"/projects/{projectId}", cancellationToken);
        if (!response.IsSuccessStatusCode)
        {
            throw new InvalidOperationException(await response.Content.ReadAsStringAsync(cancellationToken));
        }
    }

    public Task<List<ChatDto>?> ListChatsAsync(string projectId, bool sync = false, CancellationToken cancellationToken = default) =>
        _http.GetFromJsonAsync<List<ChatDto>>($"/projects/{projectId}/chats?sync={sync.ToString().ToLowerInvariant()}", JsonOptions, cancellationToken);

    public Task<ChatDto?> CreateChatAsync(string projectId, string title, CancellationToken cancellationToken = default) =>
        PostAsync<ChatDto>($"/projects/{projectId}/chats", new { title }, cancellationToken);

    public Task<ChatDto?> CreateChatAsync(string projectId, string title, string permissionProfile, string approvalPolicy, string model, string reasoningEffort, string approvalsReviewer = "user", CancellationToken cancellationToken = default) =>
        PostAsync<ChatDto>($"/projects/{projectId}/chats", new { title, permissionProfile, approvalPolicy, approvalsReviewer, model, reasoningEffort }, cancellationToken);

    public Task<ChatDto?> RenameChatAsync(string projectId, string chatId, string title, CancellationToken cancellationToken = default) =>
        PatchAsync<ChatDto>($"/projects/{projectId}/chats/{chatId}", new { title }, cancellationToken);

    public Task<ChatDto?> ArchiveChatAsync(string projectId, string chatId, CancellationToken cancellationToken = default) =>
        PostAsync<ChatDto>($"/projects/{projectId}/chats/{chatId}/archive", new { }, cancellationToken);

    public Task<AppSettingsDto?> GetChatSettingsAsync(string projectId, string chatId, CancellationToken cancellationToken = default) =>
        _http.GetFromJsonAsync<AppSettingsDto>($"/projects/{projectId}/chats/{chatId}/settings", JsonOptions, cancellationToken);

    public Task<AppSettingsDto?> UpdateChatSettingsAsync(string projectId, string chatId, string permissionProfile, string approvalPolicy, string model, string reasoningEffort, string approvalsReviewer = "user", CancellationToken cancellationToken = default) =>
        PatchAsync<AppSettingsDto>($"/projects/{projectId}/chats/{chatId}/settings", new { permissionProfile, approvalPolicy, approvalsReviewer, model, reasoningEffort }, cancellationToken);

    public Task<List<AutomationDto>?> ListAutomationsAsync(string projectId, string chatId, CancellationToken cancellationToken = default) =>
        _http.GetFromJsonAsync<List<AutomationDto>>($"/projects/{projectId}/chats/{chatId}/automations", JsonOptions, cancellationToken);

    public Task<AutomationDto?> CreateAutomationAsync(string projectId, string chatId, string name, string prompt, string scheduleKind, int scheduleValue, bool enabled, CancellationToken cancellationToken = default) =>
        PostAsync<AutomationDto>($"/projects/{projectId}/chats/{chatId}/automations", new { name, prompt, schedule_kind = scheduleKind, interval_minutes = scheduleValue, enabled }, cancellationToken);

    public Task<AutomationDto?> UpdateAutomationAsync(string projectId, string chatId, string automationId, bool enabled, CancellationToken cancellationToken = default) =>
        PatchAsync<AutomationDto>($"/projects/{projectId}/chats/{chatId}/automations/{automationId}", new { enabled }, cancellationToken);

    public Task<AutomationDto?> UpdateAutomationAsync(string projectId, string chatId, string automationId, string name, string prompt, string scheduleKind, int scheduleValue, bool enabled, CancellationToken cancellationToken = default) =>
        PatchAsync<AutomationDto>($"/projects/{projectId}/chats/{chatId}/automations/{automationId}", new { name, prompt, schedule_kind = scheduleKind, interval_minutes = scheduleValue, enabled }, cancellationToken);

    public Task<AutomationRunResultDto?> RunAutomationNowAsync(string projectId, string chatId, string automationId, CancellationToken cancellationToken = default) =>
        PostAsync<AutomationRunResultDto>($"/projects/{projectId}/chats/{chatId}/automations/{automationId}/run", new { }, cancellationToken);

    public async Task DeleteAutomationAsync(string projectId, string chatId, string automationId, CancellationToken cancellationToken = default)
    {
        using var response = await _http.DeleteAsync($"/projects/{projectId}/chats/{chatId}/automations/{automationId}", cancellationToken);
        if (!response.IsSuccessStatusCode)
        {
            throw new InvalidOperationException(await response.Content.ReadAsStringAsync(cancellationToken));
        }
    }

    public async Task DeleteChatAsync(string projectId, string chatId, CancellationToken cancellationToken = default)
    {
        using var response = await _http.DeleteAsync($"/projects/{projectId}/chats/{chatId}", cancellationToken);
        if (!response.IsSuccessStatusCode)
        {
            throw new InvalidOperationException(await response.Content.ReadAsStringAsync(cancellationToken));
        }
    }

    public Task<List<MessageDto>?> ListMessagesAsync(string projectId, string chatId, CancellationToken cancellationToken = default) =>
        _http.GetFromJsonAsync<List<MessageDto>>($"/projects/{projectId}/chats/{chatId}/messages", JsonOptions, cancellationToken);

    public Task<MessagePageDto?> ListMessagePageAsync(string projectId, string chatId, int limit, string? beforeCreatedAt = null, string? beforeId = null, CancellationToken cancellationToken = default)
    {
        var path = $"/projects/{projectId}/chats/{chatId}/messages/page?limit={limit}";
        if (!string.IsNullOrWhiteSpace(beforeCreatedAt))
        {
            path += $"&before_created_at={Uri.EscapeDataString(beforeCreatedAt)}";
        }
        if (!string.IsNullOrWhiteSpace(beforeId))
        {
            path += $"&before_id={Uri.EscapeDataString(beforeId)}";
        }
        return _http.GetFromJsonAsync<MessagePageDto>(path, JsonOptions, cancellationToken);
    }

    public Task<MessagePostResult?> SendMessageAsync(string projectId, string chatId, string content, IReadOnlyList<MessageAttachmentDto>? attachments = null, CancellationToken cancellationToken = default) =>
        PostAsync<MessagePostResult>($"/projects/{projectId}/chats/{chatId}/messages", new { content, attachments = NormalizeAttachmentPaths(attachments) }, cancellationToken);

    public Task<RunDto?> CancelRunAsync(string runId, CancellationToken cancellationToken = default) =>
        PostAsync<RunDto>($"/runs/{runId}/cancel", new { }, cancellationToken);

    public Task<RunDto?> SteerRunAsync(string runId, string content, IReadOnlyList<MessageAttachmentDto>? attachments = null, CancellationToken cancellationToken = default) =>
        PostAsync<RunDto>($"/runs/{runId}/steer", new { content, attachments = NormalizeAttachmentPaths(attachments) }, cancellationToken);

    private static IReadOnlyList<MessageAttachmentDto> NormalizeAttachmentPaths(IReadOnlyList<MessageAttachmentDto>? attachments)
    {
        return (attachments ?? Array.Empty<MessageAttachmentDto>())
            .Select(attachment => attachment with { Path = AttachmentPathToWsl(attachment.Path) })
            .ToArray();
    }

    private static string AttachmentPathToWsl(string path)
    {
        var normalized = (path ?? "").Replace('\\', '/');
        const string wslLocalhostPrefix = "//wsl.localhost/";
        const string wslLegacyPrefix = "//wsl$/";
        if (normalized.StartsWith(wslLocalhostPrefix, StringComparison.OrdinalIgnoreCase))
        {
            return StripWslUncDistro(normalized[wslLocalhostPrefix.Length..]);
        }
        if (normalized.StartsWith(wslLegacyPrefix, StringComparison.OrdinalIgnoreCase))
        {
            return StripWslUncDistro(normalized[wslLegacyPrefix.Length..]);
        }
        if (normalized.Length >= 3 && char.IsAsciiLetter(normalized[0]) && normalized[1] == ':' && normalized[2] == '/')
        {
            return $"/mnt/{char.ToLowerInvariant(normalized[0])}/{normalized[3..]}";
        }
        return normalized;
    }

    private static string StripWslUncDistro(string distroAndPath)
    {
        var slashIndex = distroAndPath.IndexOf('/');
        return slashIndex < 0 ? "/" : "/" + distroAndPath[(slashIndex + 1)..];
    }

    public Task<FileListDto?> ListFilesAsync(string projectId, string path, CancellationToken cancellationToken = default) =>
        _http.GetFromJsonAsync<FileListDto>($"/projects/{projectId}/files?path={Uri.EscapeDataString(path)}", JsonOptions, cancellationToken);

    public Task<FileContentDto?> ReadFileAsync(string projectId, string path, CancellationToken cancellationToken = default) =>
        _http.GetFromJsonAsync<FileContentDto>($"/projects/{projectId}/files/content?path={Uri.EscapeDataString(path)}", JsonOptions, cancellationToken);

    public Task<string> GetDiagnosticsJsonAsync(CancellationToken cancellationToken = default) =>
        _http.GetStringAsync("/diagnostics", cancellationToken);

    public Task<AppSettingsDto?> GetSettingsAsync(CancellationToken cancellationToken = default) =>
        _http.GetFromJsonAsync<AppSettingsDto>("/settings", JsonOptions, cancellationToken);

    public Task<ModelListDto?> GetModelsAsync(CancellationToken cancellationToken = default) =>
        _http.GetFromJsonAsync<ModelListDto>("/models", JsonOptions, cancellationToken);

    public Task<AppSettingsDto?> UpdateSettingsAsync(string permissionProfile, string approvalPolicy, string model, string reasoningEffort = "", string approvalsReviewer = "user", CancellationToken cancellationToken = default) =>
        PatchAsync<AppSettingsDto>("/settings", new { permissionProfile, approvalPolicy, approvalsReviewer, model, reasoningEffort }, cancellationToken);

    public async IAsyncEnumerable<SseEvent> StreamRunEventsAsync(string runId, [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        using var response = await _http.GetAsync($"/runs/{runId}/events", HttpCompletionOption.ResponseHeadersRead, cancellationToken);
        response.EnsureSuccessStatusCode();
        await using var stream = await response.Content.ReadAsStreamAsync(cancellationToken);
        using var reader = new StreamReader(stream, Encoding.UTF8);
        string? eventName = null;
        string? data = null;
        while (!cancellationToken.IsCancellationRequested)
        {
            var line = await reader.ReadLineAsync(cancellationToken);
            if (line is null)
            {
                break;
            }
            if (string.IsNullOrEmpty(line))
            {
                if (eventName is not null && data is not null)
                {
                    yield return new SseEvent(eventName, data);
                }
                eventName = null;
                data = null;
                continue;
            }
            if (line.StartsWith("event: ", StringComparison.Ordinal))
            {
                eventName = line[7..];
            }
            else if (line.StartsWith("data: ", StringComparison.Ordinal))
            {
                data = line[6..];
            }
        }
    }

    private async Task<T?> PostAsync<T>(string path, object body, CancellationToken cancellationToken)
    {
        using var response = await _http.PostAsJsonAsync(path, body, JsonOptions, cancellationToken);
        if (!response.IsSuccessStatusCode)
        {
            var text = await response.Content.ReadAsStringAsync(cancellationToken);
            throw new InvalidOperationException(text);
        }
        return await response.Content.ReadFromJsonAsync<T>(JsonOptions, cancellationToken);
    }

    private async Task<T?> PatchAsync<T>(string path, object body, CancellationToken cancellationToken)
    {
        using var response = await _http.PatchAsJsonAsync(path, body, JsonOptions, cancellationToken);
        if (!response.IsSuccessStatusCode)
        {
            var text = await response.Content.ReadAsStringAsync(cancellationToken);
            throw new InvalidOperationException(text);
        }
        return await response.Content.ReadFromJsonAsync<T>(JsonOptions, cancellationToken);
    }
}

public sealed record WslEnvironment(string DistroName, string HomePath);

internal readonly record struct DaemonReady(string Host, int Port);

internal sealed record DaemonReadyMessage(string Event, string Host, int Port);
