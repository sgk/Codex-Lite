using System.IO;
using System.Text.RegularExpressions;
using CodexLite.Models;

namespace CodexLite.Services;

public static class MessageAttachmentPresentation
{
    private static readonly Regex ClipboardName = new(@"^clipboard-\d{8}-\d{6}-[0-9a-f]{32}\.png$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
    private static readonly HashSet<string> ImageExtensions = new(StringComparer.OrdinalIgnoreCase) { ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp" };

    public static bool IsGeneratedClipboardName(string name) => ClipboardName.IsMatch(name);

    public static (string Content, IReadOnlyList<MessageAttachmentDto> Attachments) Parse(string content, IReadOnlyList<MessageAttachmentDto>? attachments)
    {
        var normalized = content.Replace("\r\n", "\n");
        const string marker = "\n\nAttachments:\n";
        var markerIndex = normalized.LastIndexOf(marker, StringComparison.Ordinal);
        if (markerIndex < 0) return (content, attachments ?? []);
        var lines = normalized[(markerIndex + marker.Length)..].TrimEnd('\n').Split('\n');
        if (attachments is { Count: > 0 })
        {
            if (lines.Length != attachments.Count) return (content, attachments);
            for (var i = 0; i < lines.Length; i++)
            {
                var a = attachments[i];
                if (lines[i] != $"- {a.Name}" && lines[i] != $"- {a.Name}: {a.Path}") return (content, attachments);
            }
            return (normalized[..markerIndex], attachments);
        }
        var parsed = new List<MessageAttachmentDto>();
        foreach (var line in lines)
        {
            if (!line.StartsWith("- ", StringComparison.Ordinal)) return (content, []);
            var value = line[2..];
            if (ClipboardName.IsMatch(value))
            {
                // Old clipboard messages saved only this generated basename.
                parsed.Add(new MessageAttachmentDto("", value, "image"));
                continue;
            }
            var separator = value.IndexOf(": /", StringComparison.Ordinal);
            if (separator <= 0) return (content, []);
            var name = value[..separator];
            var path = value[(separator + 2)..];
            var kind = ImageExtensions.Contains(Path.GetExtension(name)) ? "image" : "file";
            parsed.Add(new MessageAttachmentDto(path, name, kind));
        }
        return parsed.Count == 0 ? (content, []) : (normalized[..markerIndex], parsed);
    }
}
