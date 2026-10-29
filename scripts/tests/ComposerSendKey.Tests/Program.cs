using CodexLite.Services;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using System.Reflection;
using System.Text.Json;

var root = args.Single();
var checks = 0;
void Check(bool result, string label) { if (!result) throw new Exception(label); checks++; }
Check(ComposerSendKey.Normalize(null) == ComposerSendKey.CtrlEnter, "default is Ctrl+Enter");
Check(!ComposerSendKey.ShouldSend("unexpected", true, true, false, false, false, false), "invalid saved mode does not silently select a send key");
Check(ComposerSendKey.Normalize("enter") == ComposerSendKey.Enter, "Enter mode persists");
foreach (var mode in new[] { ComposerSendKey.CtrlEnter, ComposerSendKey.Enter })
{
    Check(!ComposerSendKey.ShouldSend(mode, true, mode == ComposerSendKey.CtrlEnter, false, false, false, true), "IME composition must not send");
    Check(!ComposerSendKey.ShouldSend(mode, true, false, true, false, false, false), "Shift+Enter is not send");
    Check(!ComposerSendKey.ShouldSend(mode, true, false, false, true, false, false), "Alt+Enter is not send");
    Check(!ComposerSendKey.ShouldSend(mode, true, false, false, false, true, false), "Meta+Enter is not send");
    Check(!ComposerSendKey.ShouldSend(mode, false, false, false, false, false, false), "Other keys do not send");
}
Check(ComposerSendKey.ShouldSend(ComposerSendKey.CtrlEnter, true, true, false, false, false, false), "Ctrl+Enter sends in Ctrl mode");
Check(!ComposerSendKey.ShouldSend(ComposerSendKey.CtrlEnter, true, true, true, false, false, false), "Ctrl+Shift+Enter does not send");
Check(!ComposerSendKey.ShouldSend(ComposerSendKey.CtrlEnter, true, false, false, false, false, false), "Enter remains newline in Ctrl mode");
Check(ComposerSendKey.ShouldSend(ComposerSendKey.Enter, true, false, false, false, false, false), "Enter sends in Enter mode");
Check(!ComposerSendKey.ShouldSend(ComposerSendKey.Enter, true, true, false, false, false, false), "Ctrl+Enter does not send in Enter mode");
var source = File.ReadAllText(Path.Combine(root, "windows/CodexLite/MainWindow.xaml.cs"));
var syntax = CSharpSyntaxTree.ParseText(source);
var errors = syntax.GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error).ToArray();
Check(errors.Length == 0, "MainWindow syntax: " + string.Join("; ", errors.Select(d => d.ToString())));
// Compile the actual persisted records independently of WPF to test both
// round-trip and backward compatibility with existing UI-state files.
var records = syntax.GetRoot().DescendantNodes().OfType<RecordDeclarationSyntax>()
    .Where(r => r.Identifier.Text is "UiState" or "WindowPlacementState").Select(r => r.ToFullString()).ToArray();
Check(records.Length == 2, "UI state records found");
var testTree = CSharpSyntaxTree.ParseText("using System.Collections.Generic; namespace CodexLite;\n" + string.Join("\n", records));
var references = ((string)AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES")!).Split(Path.PathSeparator)
    .Select(path => MetadataReference.CreateFromFile(path));
var compilation = CSharpCompilation.Create("UiStateSerializationTest", new[] { testTree }, references,
    new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary));
using var assemblyStream = new MemoryStream();
var emitted = compilation.Emit(assemblyStream);
Check(emitted.Success, "UI state record compile: " + string.Join("; ", emitted.Diagnostics.Where(d => d.Severity == DiagnosticSeverity.Error)));
var stateType = Assembly.Load(assemblyStream.ToArray()).GetType("CodexLite.UiState")!;
var jsonOptions = new JsonSerializerOptions(JsonSerializerDefaults.Web);
var state = JsonSerializer.Deserialize("{\"expandedProjectIds\":[],\"composerSendKey\":\"enter\"}", stateType, jsonOptions)!;
var restored = JsonSerializer.Deserialize(JsonSerializer.Serialize(state, stateType, jsonOptions), stateType, jsonOptions)!;
Check((string?)stateType.GetProperty("ComposerSendKey")!.GetValue(restored) == "enter", "Saved mode survives UI-state round trip");
var oldState = JsonSerializer.Deserialize("{\"expandedProjectIds\":[]}", stateType, jsonOptions)!;
Check(ComposerSendKey.Normalize((string?)stateType.GetProperty("ComposerSendKey")!.GetValue(oldState)) == ComposerSendKey.CtrlEnter,
    "Existing UI state remains Ctrl+Enter by default");
Console.WriteLine($"Composer send key: {checks} checks passed (Windows client not built or launched).");
