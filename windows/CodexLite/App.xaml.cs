using System;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Threading;
using CodexLite.Services;
using Microsoft.Win32;

namespace CodexLite;

public partial class App : System.Windows.Application
{
    private const string OAuthPipeName = "CodexLite-OAuth-Callback";
    private const string DeployPipeName = "CodexLite-Deploy-Control";
    public static event Action<string>? OAuthCallbackReceived;

    protected override void OnStartup(StartupEventArgs e)
    {
        DispatcherUnhandledException += App_DispatcherUnhandledException;
        AppDomain.CurrentDomain.UnhandledException += (_, args) => Debug.WriteLine(args.ExceptionObject);
        TaskScheduler.UnobservedTaskException += (_, args) =>
        {
            Debug.WriteLine(args.Exception);
            args.SetObserved();
        };
        base.OnStartup(e);
        if (RemoteSyncFeature.IsEnabled)
        {
            RegisterOAuthProtocol();
            _ = ListenForOAuthCallbacksAsync();
        }
        _ = ListenForDeployCommandsAsync();
        var redirectScheme = RemoteSyncFeature.IsEnabled ? GoogleOAuthClient.TryGetRedirectScheme() : null;
        var callback = redirectScheme is null
            ? null
            : e.Args.FirstOrDefault(arg => arg.StartsWith($"{redirectScheme}:", StringComparison.OrdinalIgnoreCase));
        if (callback is not null) _ = ForwardOAuthCallbackAsync(callback);
    }

    public static async Task<string> WaitForOAuthCallbackAsync(CancellationToken cancellationToken)
    {
        var completion = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
        void Handler(string value) => completion.TrySetResult(value);
        OAuthCallbackReceived += Handler;
        try { return await completion.Task.WaitAsync(cancellationToken); }
        finally { OAuthCallbackReceived -= Handler; }
    }

    private static async Task ListenForOAuthCallbacksAsync()
    {
        while (true)
        {
            try
            {
                using var server = new NamedPipeServerStream(OAuthPipeName, PipeDirection.In, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
                await server.WaitForConnectionAsync();
                using var reader = new StreamReader(server);
                var value = await reader.ReadLineAsync();
                if (!string.IsNullOrWhiteSpace(value)) OAuthCallbackReceived?.Invoke(value);
            }
            catch { await Task.Delay(250); }
        }
    }

    private static async Task ListenForDeployCommandsAsync()
    {
        while (true)
        {
            try
            {
                using var server = new NamedPipeServerStream(DeployPipeName, PipeDirection.In, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
                await server.WaitForConnectionAsync();
                using var reader = new StreamReader(server);
                var command = await reader.ReadLineAsync();
                if (string.Equals(command, "restart-for-deploy", StringComparison.Ordinal))
                {
                    await Current.Dispatcher.InvokeAsync(() =>
                    {
                        foreach (var window in Current.Windows.OfType<Window>().Where(window => window != Current.MainWindow).ToArray())
                        {
                            window.Close();
                        }
                        Current.MainWindow?.Close();
                    });
                }
            }
            catch
            {
                await Task.Delay(250);
            }
        }
    }

    private static async Task ForwardOAuthCallbackAsync(string callback)
    {
        try
        {
            using var client = new NamedPipeClientStream(".", OAuthPipeName, PipeDirection.Out, PipeOptions.Asynchronous);
            for (var attempt = 0; attempt < 10; attempt++)
            {
                try { await client.ConnectAsync(500); break; }
                catch when (attempt < 9) { await Task.Delay(100); }
            }
            if (!client.IsConnected) return;
            using var writer = new StreamWriter(client) { AutoFlush = true };
            await writer.WriteLineAsync(callback);
        }
        finally { Current.Shutdown(); }
    }

    private static void RegisterOAuthProtocol()
    {
        var executable = Environment.ProcessPath;
        if (string.IsNullOrWhiteSpace(executable) || !executable.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)) return;
        var redirectScheme = GoogleOAuthClient.TryGetRedirectScheme();
        if (string.IsNullOrWhiteSpace(redirectScheme)) return;
        using var key = Registry.CurrentUser.CreateSubKey($@"Software\Classes\{redirectScheme}");
        key?.SetValue("", "URL:Codex Lite OAuth");
        key?.SetValue("URL Protocol", "");
        using var command = key?.CreateSubKey(@"shell\open\command");
        command?.SetValue("", $"\"{executable}\" \"%1\"");
    }

    private static void App_DispatcherUnhandledException(object sender, DispatcherUnhandledExceptionEventArgs e)
    {
        Debug.WriteLine(e.Exception);
        System.Windows.MessageBox.Show(e.Exception.Message, "Codex Lite Error", MessageBoxButton.OK, MessageBoxImage.Error);
        e.Handled = true;
    }
}
