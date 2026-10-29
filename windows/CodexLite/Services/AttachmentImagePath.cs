using System.IO;
using CodexLite.Models;

namespace CodexLite.Services;

public static class AttachmentImagePath
{
    public static string Resolve(MessageAttachmentDto attachment, string clipboardDirectory, string wslDistroName)
    {
        var path = (attachment.Path ?? "").Replace('\\', '/');
        if (path.Length == 0 && MessageAttachmentPresentation.IsGeneratedClipboardName(attachment.Name))
            return Path.Combine(clipboardDirectory, attachment.Name);
        if (path.Length >= 7 && path.StartsWith("/mnt/", StringComparison.Ordinal) && char.IsAsciiLetter(path[5]) && path[6] == '/')
            return $"{char.ToUpperInvariant(path[5])}:\\{path[7..].Replace('/', '\\')}";
        if (path.StartsWith("/", StringComparison.Ordinal) && !path.StartsWith("//", StringComparison.Ordinal)
            && !string.IsNullOrWhiteSpace(wslDistroName) && wslDistroName.IndexOfAny(['/', '\\']) < 0)
            return $"\\\\wsl.localhost\\{wslDistroName}{path.Replace('/', '\\')}";
        if (Path.IsPathFullyQualified(path) && !path.StartsWith("//", StringComparison.Ordinal)) return path;
        throw new InvalidOperationException("画像のローカルパスが不正です。");
    }
}
