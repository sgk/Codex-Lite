using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using System.IO;
using System.Reflection;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using CodexLite;

var root = args.Single();
var source = File.ReadAllText(Path.Combine(root, "windows/CodexLite/MainWindow.xaml.cs"));
var tree = CSharpSyntaxTree.ParseText(source);
var names = new HashSet<string>(StringComparer.Ordinal) {
    "MarkdownViewer_ImageClicked", "AttachmentImage_Loaded", "AttachmentImage_DataContextChanged",
    "AttachmentImage_MouseLeftButtonUp", "ReadAttachmentImage", "ShowExpandedImage"
};
var methods = tree.GetRoot().DescendantNodes().OfType<MethodDeclarationSyntax>()
    .Where(method => names.Contains(method.Identifier.Text)).Select(method => method.ToFullString()).ToArray();
if (methods.Length != names.Count) throw new Exception("Missing actual image handlers");
var stub = """
using System;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using CodexLite.Models;
using CodexLite.Services;
using Button = System.Windows.Controls.Button;
namespace CodexLite;
public class ImageHandlerTestWindow : Window
{
    private string _wslDistroName = "Ubuntu";
    private TextBlock StatusText = new();
    private static string AttachmentDirectory() => "";
    private static string LogText(string text) => text;
    private static string ShortError(Exception ex) => ex.Message;
    private static void WritePerformanceLog(string name, string message) { }
""" + string.Join("\n", methods) + "\n}";
var references = ((string)AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES")!).Split(Path.PathSeparator)
    .Append(typeof(Window).Assembly.Location).Append(typeof(BitmapImage).Assembly.Location)
    .Append(typeof(MarkdownViewer).Assembly.Location).Distinct(StringComparer.OrdinalIgnoreCase)
    .Select(path => MetadataReference.CreateFromFile(path));
var compilation = CSharpCompilation.Create("ImageHandlerSemanticTest", [CSharpSyntaxTree.ParseText(stub)], references,
    new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary));
using var output = new MemoryStream();
var result = compilation.Emit(output);
if (!result.Success) throw new Exception(string.Join("\n", result.Diagnostics.Where(d => d.Severity == DiagnosticSeverity.Error)));
var handlerType = Assembly.Load(output.ToArray()).GetType("CodexLite.ImageHandlerTestWindow")!;
var readImage = handlerType.GetMethod("ReadAttachmentImage", BindingFlags.Static | BindingFlags.NonPublic)!;
Exception? failure = null;
var thread = new Thread(() =>
{
    var path = Path.GetTempFileName();
    try
    {
        var bitmap = BitmapSource.Create(2, 2, 96, 96, PixelFormats.Bgra32, null, new byte[16], 8);
        bitmap.Freeze();
        using (var stream = File.Create(path))
        {
            var encoder = new PngBitmapEncoder();
            encoder.Frames.Add(BitmapFrame.Create(bitmap));
            encoder.Save(stream);
        }
        var thumbnail = (BitmapImage)readImage.Invoke(null, [path, 480])!;
        if (!thumbnail.IsFrozen || thumbnail.PixelWidth <= 0) throw new Exception("Attachment thumbnail was not loaded safely");
        var expanded = (BitmapImage)readImage.Invoke(null, [path, 1920])!;
        if (!expanded.IsFrozen || expanded.PixelWidth <= 0) throw new Exception("Attachment enlargement was not loaded safely");

        var viewer = new MarkdownViewer { ImageMaxWidth = 240, ImageMaxHeight = 160 };
        viewer.ImageRequested += (_, request) => request.Source = bitmap;
        ImageSource? clicked = null;
        viewer.ImageClicked += (_, image) => clicked = image.Source;
        var paragraph = new Paragraph();
        typeof(MarkdownViewer).GetMethod("AddImage", BindingFlags.Instance | BindingFlags.NonPublic)!
            .Invoke(viewer, [paragraph, "図", "sample"]);
        var panel = (StackPanel)((InlineUIContainer)paragraph.Inlines.FirstInline!).Child;
        var control = panel.Children.OfType<System.Windows.Controls.Image>().Single();
        if (control.MaxWidth != 240 || control.MaxHeight != 160) throw new Exception("Assistant image is not thumbnail-sized");
        control.RaiseEvent(new MouseButtonEventArgs(Mouse.PrimaryDevice, 0, MouseButton.Left) { RoutedEvent = UIElement.MouseLeftButtonUpEvent });
        if (!ReferenceEquals(clicked, bitmap)) throw new Exception("Assistant image click did not expose the image for enlargement");
    }
    catch (Exception ex) { failure = ex; }
    finally { File.Delete(path); }
});
thread.SetApartmentState(ApartmentState.STA);
thread.Start();
thread.Join();
if (failure is not null) throw new Exception("WPF image component test failed", failure);
Console.WriteLine("WPF image control and actual image handlers compile; thumbnail loading and image click pass (Windows client not built or launched).");
