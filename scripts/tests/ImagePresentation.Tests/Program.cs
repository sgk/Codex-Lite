using System.Xml.Linq;
using CodexLite.Models;
using CodexLite.Services;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

var root = args.Single();
var checks = 0;
void Check(bool condition, string label) { if (!condition) throw new Exception(label); checks++; }
var name = "clipboard-20261029-164954-1234567890abcdef1234567890abcdef.png";
var path = "/mnt/c/Users/me/AppData/Local/CodexLite/attachments/" + name;
var image = new MessageAttachmentDto(path, name, "image");
var document = new MessageAttachmentDto("/home/me/report.pdf", "report.pdf", "file");
var saved = new MessageDto("m1", "c", "user", $"ここ？\n\nAttachments:\n- {name}\n- report.pdf: /home/me/report.pdf", "r", "2026-10-29T00:00:00Z", "instruction", [image, document]);
Check(saved.DisplayContent == "ここ？", "Stored attachment footer is hidden from message text");
Check(saved.DisplayAttachments.Count == 2 && saved.DisplayAttachments[0].Path == path, "Stored metadata is used without losing non-image attachments");
var immediate = saved with { Content = "ここ？" };
Check(immediate.DisplayContent == "ここ？" && immediate.DisplayAttachments.Count == 2, "Optimistic local messages retain their images");
var legacy = saved with { Content = $"ここ？\n\nAttachments:\n- {name}", Attachments = null };
Check(legacy.DisplayContent == "ここ？" && legacy.DisplayAttachments.Single().Name == name, "Old clipboard-name-only messages are reconstructed");
Check(legacy.DisplayAttachments[0].Path == "", "Legacy clipboard path is derived only from the generated name");
Check(AttachmentImagePath.Resolve(legacy.DisplayAttachments[0], @"C:\attachments", "Ubuntu") == @"C:\attachments\" + name,
    "Legacy clipboard image resolves inside the known attachment directory");
Check(AttachmentImagePath.Resolve(image, @"C:\attachments", "Ubuntu") == @"C:\Users\me\AppData\Local\CodexLite\attachments\" + name,
    "WSL-mounted Windows path resolves to a Windows file");
Check(AttachmentImagePath.Resolve(new MessageAttachmentDto("/home/me/pic.png", "pic.png", "image"), @"C:\attachments", "Ubuntu-24.04") == @"\\wsl.localhost\Ubuntu-24.04\home\me\pic.png",
    "WSL image path resolves to the selected distribution");
foreach (var invalid in new[] { new MessageAttachmentDto("", "../" + name, "image"), new MessageAttachmentDto("https://example.invalid/a.png", "a.png", "image"), new MessageAttachmentDto("//server/a.png", "a.png", "image") })
{
    var failed = false;
    try { AttachmentImagePath.Resolve(invalid, @"C:\attachments", "Ubuntu"); } catch (InvalidOperationException) { failed = true; }
    Check(failed, "Untrusted or missing image paths do not trigger network or guessed-file access");
}
var ordinary = saved with { Content = "普通の文\n\nAttachments:\n- 任意の文章", Attachments = null };
Check(ordinary.DisplayContent == ordinary.Content && ordinary.DisplayAttachments.Count == 0, "Ordinary user text is not mistaken for attachment metadata");
var mismatch = saved with { Content = "普通の文\n\nAttachments:\n- 別の内容" };
Check(mismatch.DisplayContent == mismatch.Content, "A nonmatching footer is never removed");
var assistant = saved with { Role = "assistant", Kind = "conclusion", Content = $"![図](data:image/png;base64,AA==)" };
Check(assistant.DisplayContent == assistant.Content, "Assistant Markdown images are retained");
var xaml = XDocument.Load(Path.Combine(root, "windows/CodexLite/MainWindow.xaml"));
XNamespace x = "http://schemas.microsoft.com/winfx/2006/xaml";
Check(xaml.Descendants().Any(element => (string?)element.Attribute("MouseLeftButtonUp") == "AttachmentImage_MouseLeftButtonUp"), "Attachment thumbnails have an expand handler");
Check(xaml.Descendants().Any(element => (string?)element.Attribute("ImageClicked") == "MarkdownViewer_ImageClicked"), "Assistant Markdown images have an expand handler");
foreach (var relative in new[] { "MainWindow.xaml.cs", "MarkdownViewer.cs", "Models/Dto.cs" })
{
    var errors = CSharpSyntaxTree.ParseText(File.ReadAllText(Path.Combine(root, "windows/CodexLite", relative)))
        .GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error).ToArray();
    Check(errors.Length == 0, relative + ": " + string.Join("; ", errors.Select(d => d.ToString())));
}
Console.WriteLine($"Image presentation: {checks} checks passed (Windows client not built or launched).");

namespace System.Windows { public enum Visibility { Visible, Hidden, Collapsed } }
