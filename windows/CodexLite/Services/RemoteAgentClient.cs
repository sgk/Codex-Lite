using System.Diagnostics;
using System.IO;

namespace CodexLite.Services;

public sealed class RemoteAgentClient
{
    private Process? _process;

    public event Action<string>? OutputLineReceived;
    public event Action<string>? ErrorLineReceived;
    public event Action? Exited;

    public bool IsRunning => _process is { HasExited: false };

    public Task StartAsync(string distroName, string accessToken, CancellationToken cancellationToken = default)
    {
        if (IsRunning) return Task.CompletedTask;
        if (string.IsNullOrWhiteSpace(distroName)) throw new InvalidOperationException("WSLディストリビューションが未確定です。");
        if (string.IsNullOrWhiteSpace(accessToken)) throw new InvalidOperationException("Google認証トークンが空です。");

        var remoteDirectory = Path.Combine(AppContext.BaseDirectory, "remote-agent");
        var remoteEntryPoint = Path.Combine(remoteDirectory, "index.js");
        if (!File.Exists(remoteEntryPoint)) throw new FileNotFoundException("同梱Remote Agentが見つかりません。", remoteEntryPoint);

        var remoteWslPath = WindowsPathToWslPath(remoteDirectory, distroName);
        var command = $"IFS= read -r CODEX_LITE_REMOTE_GOOGLE_ACCESS_TOKEN || exit 1; export CODEX_LITE_REMOTE_GOOGLE_ACCESS_TOKEN; cd {ShellQuote(remoteWslPath)}; exec node index.js";
        var startInfo = new ProcessStartInfo
        {
            FileName = "wsl.exe",
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        startInfo.ArgumentList.Add("-d");
        startInfo.ArgumentList.Add(distroName);
        startInfo.ArgumentList.Add("--");
        startInfo.ArgumentList.Add("bash");
        startInfo.ArgumentList.Add("-c");
        startInfo.ArgumentList.Add(command);

        _process = Process.Start(startInfo) ?? throw new InvalidOperationException("Remote Agentを起動できませんでした。");
        _process.EnableRaisingEvents = true;
        _process.Exited += (_, _) =>
        {
            _process?.Dispose();
            _process = null;
            Exited?.Invoke();
        };
        _ = DrainAsync(_process.StandardOutput, cancellationToken, line => OutputLineReceived?.Invoke(line));
        _ = DrainAsync(_process.StandardError, cancellationToken, line => ErrorLineReceived?.Invoke(line));
        return WriteTokenAsync(_process, accessToken, cancellationToken);
    }

    public async Task StopAsync()
    {
        var process = _process;
        _process = null;
        if (process is null) return;
        try
        {
            if (!process.HasExited) process.Kill(entireProcessTree: true);
            await process.WaitForExitAsync();
        }
        catch
        {
        }
        finally
        {
            process.Dispose();
        }
    }

    private static async Task WriteTokenAsync(Process process, string accessToken, CancellationToken cancellationToken)
    {
        await process.StandardInput.WriteLineAsync(accessToken.AsMemory(), cancellationToken);
        await process.StandardInput.FlushAsync(cancellationToken);
        process.StandardInput.Close();
    }

    private static async Task DrainAsync(StreamReader reader, CancellationToken cancellationToken, Action<string>? lineReceived = null)
    {
        try
        {
            while (await reader.ReadLineAsync(cancellationToken) is { } line)
            {
                lineReceived?.Invoke(line);
            }
        }
        catch
        {
        }
    }

    private static string ShellQuote(string value) => $"'{value.Replace("'", "'\\''")}'";

    private static string WindowsPathToWslPath(string path, string distroName)
    {
        var normalized = Path.GetFullPath(path).Replace('\\', '/');
        var localhostPrefix = $"//wsl.localhost/{distroName}/";
        var legacyPrefix = $"//wsl$/{distroName}/";
        if (normalized.StartsWith(localhostPrefix, StringComparison.OrdinalIgnoreCase)) return "/" + normalized[localhostPrefix.Length..];
        if (normalized.StartsWith(legacyPrefix, StringComparison.OrdinalIgnoreCase)) return "/" + normalized[legacyPrefix.Length..];
        if (normalized.Length >= 3 && char.IsAsciiLetter(normalized[0]) && normalized[1] == ':' && normalized[2] == '/') return $"/mnt/{char.ToLowerInvariant(normalized[0])}/{normalized[3..]}";
        throw new InvalidOperationException($"Remote AgentのパスをWSLへ変換できません。{path}");
    }
}
