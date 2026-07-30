using System.Collections.Generic;
using System.Globalization;
using System.Windows;

namespace CodexLite.Models;

public sealed record HealthDto(
    bool Ok,
    string Version,
    string DatabasePath,
    string? CodexPath,
    string? CodexVersion,
    string CodexHome);

public sealed record ProjectDto(
    string Id,
    string Name,
    string Path,
    string CreatedAt,
    string UpdatedAt);

public sealed record ProjectCandidateDto(
    string Path,
    string Name,
    int ThreadCount,
    string? LastUsedAt);

public sealed record UsageCapacityDto(
    UsageWindowDto? FiveHour,
    UsageWindowDto? Weekly,
    string? PlanType,
    string? RateLimitReachedType,
    ResetCreditsDto? ResetCredits,
    string FetchedAt);

public sealed record UsageWindowDto(
    double UsedPercent,
    double RemainingPercent,
    int WindowMinutes,
    string? ResetsAt);

public sealed record ResetCreditsDto(int AvailableCount);

public sealed record ChatDto(
    string Id,
    string ProjectId,
    string Title,
    string? CodexSessionId,
    string CreatedAt,
    string UpdatedAt,
    bool CanContinue = true,
    string? ContinueDisabledReason = null);

public sealed record AutomationDto(
    string Id,
    string ProjectId,
    string ChatId,
    string Name,
    string Prompt,
    string ScheduleKind,
    int IntervalMinutes,
    bool Enabled,
    bool Running,
    string? NextRunAt,
    string? LastRunAt,
    string? LastError,
    string CreatedAt,
    string UpdatedAt)
{
    public bool IsDraft => Id.StartsWith("local-automation-draft-", StringComparison.Ordinal);

    public string DisplayNextRunAt => FormatLocalTime(NextRunAt);

    public string DisplayLastRunAt => FormatLocalTime(LastRunAt);

    public string DisplayState => IsDraft ? "新規" : Running ? "実行中" : Enabled ? "有効" : "無効";

    public string DisplayEnabled => Enabled ? "有効" : "無効";

    public string DisplaySchedule => ScheduleKind switch
    {
        "hourly_minute" => $"毎時{IntervalMinutes:D2}分",
        "daily_time" => $"毎日{IntervalMinutes / 60:D2}:{IntervalMinutes % 60:D2}",
        _ => $"{IntervalMinutes}分ごと"
    };

    private static string FormatLocalTime(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return "-";
        }
        if (DateTimeOffset.TryParse(
                value,
                CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
                out var timestamp))
        {
            return timestamp.ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.CurrentCulture);
        }
        return value;
    }
}

public sealed record AutomationRunResultDto(
    AutomationDto Automation,
    MessagePostResult? Run);

public sealed record MessageDto(
    string Id,
    string ChatId,
    string Role,
    string Content,
    string? RunId,
    string CreatedAt,
    string Kind,
    IReadOnlyList<MessageAttachmentDto>? Attachments = null,
    string? ActivityDetails = null,
    double SpacerHeight = 0,
    bool SpacerIsLoading = false)
{
    public bool IsInlineStatus => Role.Equals("status", StringComparison.OrdinalIgnoreCase);

    public bool IsSpacer => Role.Equals("spacer", StringComparison.OrdinalIgnoreCase);

    public bool IsActivityIndicator => Role.Equals("activity", StringComparison.OrdinalIgnoreCase);

    public string EffectiveKind => Kind;

    public bool IsWorkProgressMessage => EffectiveKind.Equals("work", StringComparison.OrdinalIgnoreCase);

    public bool IsUserMessage => EffectiveKind.Equals("instruction", StringComparison.OrdinalIgnoreCase);

    public bool IsAssistantMessage => Role.Equals("assistant", StringComparison.OrdinalIgnoreCase);

    public bool IsWaitingMessage => EffectiveKind.Equals("waiting", StringComparison.OrdinalIgnoreCase);

    public bool IsConclusionMessage => EffectiveKind.Equals("conclusion", StringComparison.OrdinalIgnoreCase);

    public string MessageKindLabel => EffectiveKind switch
    {
        "instruction" => "指示",
        "work" => "作業内容",
        "waiting" => "待機中",
        "conclusion" => "結論",
        "status" => "作業",
        _ => Role
    };

    public Visibility SpacerVisibility => IsSpacer ? Visibility.Visible : Visibility.Collapsed;

    public Visibility MessageVisibility => IsInlineStatus || IsSpacer || IsActivityIndicator ? Visibility.Collapsed : Visibility.Visible;

    public Visibility InlineStatusVisibility => IsInlineStatus && !IsSpacer && !IsActivityIndicator ? Visibility.Visible : Visibility.Collapsed;

    public Visibility ActivityIndicatorVisibility => IsActivityIndicator ? Visibility.Visible : Visibility.Collapsed;

    public string ActivityDetailText => string.IsNullOrWhiteSpace(ActivityDetails) ? Content : ActivityDetails;

    public string DisplayCreatedAt => FormatLocalTime(CreatedAt);

    private static string FormatLocalTime(string value)
    {
        if (DateTimeOffset.TryParse(
                value,
                CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
                out var timestamp))
        {
            return timestamp.ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.CurrentCulture);
        }
        return value;
    }
}

public sealed record MessagePageDto(
    IReadOnlyList<MessageDto> Messages,
    int TotalCount,
    bool HasMoreBefore);

public sealed record MessageAttachmentDto(
    string Path,
    string Name,
    string Kind,
    string? Uri = null)
{
    public Visibility PathVisibility => IsClipboardAttachmentName(Name) ? Visibility.Collapsed : Visibility.Visible;

    private static bool IsClipboardAttachmentName(string name)
    {
        return name.StartsWith("clipboard-", StringComparison.OrdinalIgnoreCase)
            || name.StartsWith("codex-clipboard-", StringComparison.OrdinalIgnoreCase);
    }
}

public sealed record RunDto(
    string Id,
    string ChatId,
    string Status,
    int? Pid,
    int? ExitCode,
    string? StartedAt,
    string? FinishedAt,
    string? LogPath,
    string? Error);

public sealed record MessagePostResult(string MessageId, string RunId);

public sealed record FileEntryDto(
    string Name,
    string Path,
    string Kind,
    long? Size,
    string ModifiedAt,
    bool? IsText,
    string ViewerKind);

public sealed record FileListDto(string Path, IReadOnlyList<FileEntryDto> Entries);

public sealed record FileContentDto(
    string Path,
    string Kind,
    string Encoding,
    long Size,
    string Content);

public sealed record ErrorEnvelope(ErrorBody Error);

public sealed record ErrorBody(string Code, string Message, object? Details);

public sealed record SseEvent(string Event, string Data);

public sealed record AppSettingsDto(
    string PermissionProfile,
    string ApprovalPolicy,
    string Model,
    IReadOnlyList<string> AvailablePermissionProfiles,
    IReadOnlyList<string> AvailableApprovalPolicies,
    IReadOnlyList<string> AvailableModels);
