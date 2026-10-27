using System.Xml.Linq;
using CodexLite.Models;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

var root = args.Single();
var checks = 0;
void Check(bool condition, string label)
{
    if (!condition) throw new Exception(label);
    checks++;
}
var choices = new[] {
    new ProjectImportChoice("/selected/project", "project", 3, "2026-10-27T00:00:00Z"),
    new ProjectImportChoice("/other/project", "project", 5, null)
};
Check(!choices.Any(c => c.IsSelected), "Nothing is selected by default");
var changed = 0;
choices[0].PropertyChanged += (_, e) => { if (e.PropertyName == "IsSelected") changed++; };
choices[0].IsSelected = true;
choices[0].IsSelected = true;
Check(changed == 1, "A checkbox change notifies once");
Check(choices.Where(c => c.IsSelected).Select(c => c.Path).SequenceEqual(new[] { "/selected/project" }),
    "Selection uses exact paths, not matching project names");
Check(choices.Where(c => c.IsSelected).Sum(c => c.ThreadCount) == 3, "Only selected conversations are counted");
Check(!choices[1].IsSelected, "Selecting one project does not select another");
choices[0].IsSelected = false;
Check(!choices.Any(c => c.IsSelected) && changed == 2, "Clear selection returns to zero");
Check(choices[1].LastUsed == "—", "Missing timestamp is displayed explicitly");
var xaml = XDocument.Load(Path.Combine(root, "windows/CodexLite/ProjectHistoryImportDialog.xaml"));
XNamespace presentation = "http://schemas.microsoft.com/winfx/2006/xaml/presentation";
Check(xaml.Descendants(presentation + "CheckBox").Any(), "Candidate selection is a checkbox list");
Check(xaml.Descendants(presentation + "Button").Any(b => (string?)b.Attribute("IsCancel") == "True"),
    "Dialog provides cancellation");
Check(xaml.Descendants(presentation + "Button").Any(b => (string?)b.Attribute("IsEnabled") == "False"),
    "Import is initially disabled");
foreach (var relative in new[] { "MainWindow.xaml.cs", "ProjectHistoryImportDialog.xaml.cs", "Models/ProjectImportChoice.cs" })
{
    var errors = CSharpSyntaxTree.ParseText(File.ReadAllText(Path.Combine(root, "windows/CodexLite", relative)))
        .GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error).ToArray();
    Check(errors.Length == 0, relative + ": " + string.Join("; ", errors.Select(d => d.ToString())));
}
Console.WriteLine($"History import: {checks} checks passed (Windows client not built or launched).");
