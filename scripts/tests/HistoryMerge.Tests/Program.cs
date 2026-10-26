using System.Collections.ObjectModel;
using CodexLite.Services;

var checks = 0;
void Check(bool condition, string label)
{
    if (!condition) throw new Exception(label);
    checks++;
}

var live = new Message("continue-id", "つづけて");
var old = new Message("old-id", "earlier");
var saved = new Message("continue-id", "つづけて");
var output = new Message("output-id", "working");
var spacer = new Message("spacer", "");
var target = new ObservableCollection<Message> { spacer, live, output };
var notifications = 0;
target.CollectionChanged += (_, _) => notifications++;
Check(HistoryMessageMerge.PrependMissing(target, new[] { old, saved }, m => m.Id, 1) == 1,
    "live instruction arriving before history is inserted only once");
Check(target.Select(m => m.Id).SequenceEqual(new[] { "spacer", "old-id", "continue-id", "output-id" }),
    "spacer and history order are preserved");
Check(ReferenceEquals(target[2], live) && ReferenceEquals(target[3], output),
    "live objects and output are not replaced or cleared");
Check(notifications == 1, "duplicate does not trigger an extra UI collection notification");
Check(HistoryMessageMerge.PrependMissing(target, new[] { old, saved }, m => m.Id, 1) == 0,
    "repeated history result is idempotent");
Check(notifications == 1, "repeated result does not disturb rendering or scrolling");

var initial = new ObservableCollection<Message>();
Check(HistoryMessageMerge.PrependMissing(initial, new[] { old, saved }, m => m.Id, 0) == 2,
    "history-first startup keeps every distinct ID");
// The live receiver's existing ID check must now find the item loaded first.
if (!initial.Any(m => m.Id == live.Id)) initial.Add(live);
Check(initial.Count(m => m.Id == live.Id) == 1, "history then live also displays one instruction");
var distinctContinue = new Message("another-id", "つづけて");
HistoryMessageMerge.PrependMissing(initial, new[] { distinctContinue }, m => m.Id, 0);
Check(initial.Count(m => m.Content == "つづけて") == 2,
    "different real instructions with identical text are not collapsed");
var repeatedBatch = new ObservableCollection<Message>();
HistoryMessageMerge.PrependMissing(repeatedBatch, new[] { saved, saved }, m => m.Id, 0);
Check(repeatedBatch.Count == 1, "duplicate IDs inside a batch are inserted only once");
Console.WriteLine($"History merge: {checks} checks passed (Windows client not built or launched).");

sealed record Message(string Id, string Content);
