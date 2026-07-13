using System;
using System.Diagnostics;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Threading;

namespace CodexLite;

public partial class App : System.Windows.Application
{
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
    }

    private static void App_DispatcherUnhandledException(object sender, DispatcherUnhandledExceptionEventArgs e)
    {
        Debug.WriteLine(e.Exception);
        System.Windows.MessageBox.Show(e.Exception.Message, "Codex Lite Error", MessageBoxButton.OK, MessageBoxImage.Error);
        e.Handled = true;
    }
}
