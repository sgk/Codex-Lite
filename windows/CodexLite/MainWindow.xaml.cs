using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using CodexLite.Models;
using CodexLite.Services;
using CodexLite.ViewModels;
using Microsoft.Win32;
using Binding = System.Windows.Data.Binding;
using Button = System.Windows.Controls.Button;
using DrawingIcon = System.Drawing.Icon;
using DragDropEffects = System.Windows.DragDropEffects;
using Key = System.Windows.Input.Key;
using Orientation = System.Windows.Controls.Orientation;
using Point = System.Windows.Point;
using RadioButton = System.Windows.Controls.RadioButton;
using TextBox = System.Windows.Controls.TextBox;

namespace CodexLite;

public partial class MainWindow : Window
{
    private const string StartingResponseText = "送信中...";
    private const string WaitingForResponseText = "応答待ち...";
    private const int InitialMessagePageSize = 200;
    private const int OlderMessagePageSize = 100;
    private const double EstimatedMessageItemHeight = 96;
    private const string HistorySpacerMessageId = "local-history-spacer";
    private static readonly TimeSpan ExternalProcessingWindow = TimeSpan.FromMinutes(30);
    private const int StreamingCharactersPerTick = 512;
    private static readonly TimeSpan StreamingTextInterval = TimeSpan.FromMilliseconds(120);
    private static readonly TimeSpan AutoScrollInterval = TimeSpan.FromMilliseconds(250);

    private sealed record ActiveUiRun(string RunId, string ProjectId, string ChatId, CancellationTokenSource Cancellation);
    private static readonly TimeSpan BackgroundMessagePollTimeout = TimeSpan.FromSeconds(5);
    private static readonly TimeSpan UsageCapacityTimeout = TimeSpan.FromSeconds(10);
    private static readonly TimeSpan SlowUiPhaseThreshold = TimeSpan.FromMilliseconds(250);
    private static readonly TimeSpan UiStallThreshold = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan DaemonHealthCheckInterval = TimeSpan.FromSeconds(10);
    private static readonly TimeSpan AttachmentRetention = TimeSpan.FromDays(7);
    private const int ComposerHistoryLimit = 100;
    private readonly DaemonClient _client = new();
    private readonly ObservableCollection<ProjectTreeItem> _projectTree = new();
    private readonly ObservableCollection<MessageDto> _messages = new();
    private readonly ObservableCollection<AutomationDto> _automations = new();
    private readonly ObservableCollection<RunProgressEntry> _runProgress = new();
    private readonly ObservableCollection<FileTreeItem> _files = new();
    private readonly ObservableCollection<MessageAttachmentDto> _pendingAttachments = new();
    private readonly Dictionary<string, StreamingTextState> _streamingText = new();
    private readonly Dictionary<string, int> _chatLoadVersions = new();
    private readonly Dictionary<string, int> _runActivityDepthByChat = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _runProgressTextByChat = new(StringComparer.Ordinal);
    private readonly DispatcherTimer _streamingTextTimer = new() { Interval = StreamingTextInterval };
    private readonly DispatcherTimer _messageRefreshTimer = new() { Interval = TimeSpan.FromSeconds(3) };
    private readonly DispatcherTimer _uiHeartbeatTimer = new() { Interval = TimeSpan.FromMilliseconds(500) };
    private readonly DispatcherTimer _daemonHealthTimer = new() { Interval = DaemonHealthCheckInterval };
    private readonly JsonSerializerOptions _json = new(JsonSerializerDefaults.Web) { WriteIndented = true };
    private readonly Stopwatch _performanceClock = Stopwatch.StartNew();
    private readonly object _performanceLogLock = new();
    private readonly string _uiStatePath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "CodexLite",
        "ui-state.json");
    private readonly string _performanceLogPath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "CodexLite",
        "ui-performance.log");
    private readonly CancellationTokenSource _uiWatchdogCts = new();
    private readonly HashSet<string> _persistedExpandedProjectIds = new();
    private readonly HashSet<string> _chatsWithUnloadedHistory = new(StringComparer.Ordinal);
    private readonly List<string> _persistedProjectOrderIds = new();
    private readonly List<string> _composerHistory = new();
    private string _textSizeSetting = "small";
    private string _codexHomeMode = DefaultCodexHomeMode();
    private bool _wrapFileText;
    private readonly Dictionary<string, ActiveUiRun> _activeRunsByChat = new(StringComparer.Ordinal);
    private CancellationTokenSource? _usageCapacityCts;
    private DateTimeOffset _nextBackgroundMessagePollAt = DateTimeOffset.MinValue;
    private UsageWindowDto? _fiveHourUsageWindow;
    private UsageWindowDto? _weeklyUsageWindow;
    private System.Windows.Point? _projectDragStart;
    private ProjectTreeItem? _projectDragItem;
    private ProjectDto? _selectedProject;
    private ChatDto? _selectedChat;
    private string _currentFilePath = "";
    private string _currentDirectoryPath = "";
    private string _wslDistroName = "";
    private string _wslHomePath = "";
    private DateTimeOffset _lastMessageAutoScrollAt = DateTimeOffset.MinValue;
    private double _savedMessageVerticalOffset;
    private long _lastUiHeartbeatMs;
    private long _lastUiStallLogMs;
    private string _currentUiPhase = "startup";
    private bool _isClosing;
    private bool _suppressTreeSelectionChanged;
    private bool _isRefreshingTree;
    private bool _isLoadingMessages;
    private bool _isLoadingOlderMessages;
    private bool _isPollingMessages;
    private bool _isRestartingDaemon;
    private bool _isRefreshingUsageCapacity;
    private bool _isRefreshingAutomations;
    private bool _isSavingAutomation;
    private bool _isRunningAutomationNow;
    private bool _isLoadingAutomationSelection;
    private bool _isComposerTextCompositionActive;
    private bool _isRestoringMessageScrollOffset;
    private bool _pendingMessageScrollOffsetRestore;
    private bool _isCheckingDaemonHealth;
    private bool _isApplyingComposerHistory;
    private string _composerHistoryDraft = "";
    private int _automationDraftCounter;
    private int? _composerHistoryIndex;
    private int _daemonHealthFailureCount;
    private int _busyDepth;
    private int _activityDepth;
    private int _treeLoadingDepth;
    private bool _isLoadingUiState = true;
    private bool _hasCompletedInitialProjectRestore;
    private bool _hasMoreOlderMessages;
    private int _messageTotalCount;
    private string? _persistedSelectedProjectId;
    private string? _persistedSelectedChatId;
    private string? _persistedSelectedTab;

    public MainWindow()
    {
        InitializeComponent();
        InitializeCommandButtonIcons();
        UpdateCommandButtonState();
        LoadUiState();
        _client.CodexHomeMode = _codexHomeMode;
        _client.StatusChanged += Client_StatusChanged;
        ProjectTree.ItemsSource = _projectTree;
        RegisterComposerImeHandlers(MessageBox);
        RegisterComposerImeHandlers(NewChatMessageBox);
        _streamingTextTimer.Tick += StreamingTextTimer_Tick;
        _messageRefreshTimer.Tick += MessageRefreshTimer_Tick;
        _messageRefreshTimer.Start();
        _daemonHealthTimer.Tick += DaemonHealthTimer_Tick;
        StartUiWatchdog();
        ProjectTree.AddHandler(TreeViewItem.ExpandedEvent, new RoutedEventHandler(ProjectTreeItem_Expanded));
        ProjectTree.AddHandler(TreeViewItem.CollapsedEvent, new RoutedEventHandler(ProjectTreeItem_Collapsed));
        MessagesList.ItemsSource = _messages;
        AutomationsGrid.ItemsSource = _automations;
        UpdateAutomationButtonState();
        FilesTree.ItemsSource = _files;
        FilesTree.AddHandler(TreeViewItem.ExpandedEvent, new RoutedEventHandler(FilesTreeItem_Expanded));
        PendingAttachmentsList.ItemsSource = _pendingAttachments;
        NewChatPendingAttachmentsList.ItemsSource = _pendingAttachments;
        CleanupOldAttachmentFiles();
        LocationChanged += (_, _) => SaveUiState();
        SizeChanged += (_, _) => SaveUiState();
        StateChanged += (_, _) => SaveUiState();
        Activated += MainWindow_Activated;
        Closing += MainWindow_Closing;
        _ = InitializeAsync();
    }

    private void Client_StatusChanged(string message)
    {
        _ = Dispatcher.InvokeAsync(() =>
        {
            if (_isClosing)
            {
                return;
            }
            if (_activityDepth > 0)
            {
                SetBusyMessage(message);
            }
            else
            {
                StatusText.Text = message;
            }
        });
    }

    private async void MainWindow_Activated(object? sender, EventArgs e)
    {
        if (_treeLoadingDepth > 0 || _selectedProject is null || _selectedChat is not null)
        {
            return;
        }

        await RefreshUsageCapacityAsync();
    }

    private async void DaemonHealthTimer_Tick(object? sender, EventArgs e)
    {
        if (_isClosing || _isCheckingDaemonHealth)
        {
            return;
        }

        _isCheckingDaemonHealth = true;
        try
        {
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(2));
            if (await _client.ProbeHealthAsync(timeout.Token) is not null)
            {
                _daemonHealthFailureCount = 0;
                return;
            }

            _daemonHealthFailureCount++;
            if (_daemonHealthFailureCount < 3)
            {
                return;
            }

            var restarted = await _client.EnsureHealthyOrRestartAsync(_wslDistroName);
            _daemonHealthFailureCount = 0;
            StatusText.Text = "daemon restarted";
            await RefreshProjectsAsync();
            await RefreshDiagnosticsAsync();
        }
        catch (Exception ex)
        {
            _daemonHealthFailureCount++;
            StatusText.Text = $"daemon health error | {ShortError(ex)}";
        }
        finally
        {
            _isCheckingDaemonHealth = false;
        }
    }

    private async void MainWindow_Closing(object? sender, CancelEventArgs e)
    {
        if (_isClosing)
        {
            return;
        }

        e.Cancel = true;
        _isClosing = true;
        _uiWatchdogCts.Cancel();
        _uiHeartbeatTimer.Stop();
        _daemonHealthTimer.Stop();
        PersistExpandedStateFromTree();
        await _client.ShutdownDaemonAsync();
        Close();
    }

    private async Task InitializeAsync()
    {
        try
        {
            await RunBusyAsync("デーモンを起動中...", async () =>
            {
                SetBusyMessage("デーモンを起動中...");
                var wsl = await _client.ResolveDefaultWslEnvironmentAsync();
                _wslDistroName = wsl.DistroName;
                _wslHomePath = wsl.HomePath;
                await _client.EnsureDaemonAsync(_wslDistroName);
                SetBusyMessage("プロジェクトを読み込み中...");
                await RefreshProjectsAsync();
                SetBusyMessage("診断情報を読み込み中...");
                await RefreshDiagnosticsAsync();
                StatusText.Text = "待機中 | daemon ok";
                _daemonHealthTimer.Start();
            });
        }
        catch (Exception ex)
        {
            StatusText.Text = $"daemon error | {ex.Message}";
        }
    }

    private void StartUiWatchdog()
    {
        _lastUiHeartbeatMs = _performanceClock.ElapsedMilliseconds;
        _uiHeartbeatTimer.Tick += (_, _) =>
        {
            Interlocked.Exchange(ref _lastUiHeartbeatMs, _performanceClock.ElapsedMilliseconds);
        };
        _uiHeartbeatTimer.Start();
        _ = Task.Run(async () =>
        {
            while (!_uiWatchdogCts.IsCancellationRequested)
            {
                try
                {
                    await Task.Delay(1000, _uiWatchdogCts.Token);
                    var now = _performanceClock.ElapsedMilliseconds;
                    var lastHeartbeat = Interlocked.Read(ref _lastUiHeartbeatMs);
                    var gap = TimeSpan.FromMilliseconds(now - lastHeartbeat);
                    if (gap < UiStallThreshold)
                    {
                        continue;
                    }

                    var lastLog = Interlocked.Read(ref _lastUiStallLogMs);
                    if (now - lastLog < 1000)
                    {
                        continue;
                    }

                    Interlocked.Exchange(ref _lastUiStallLogMs, now);
                    WritePerformanceLog("ui-stall", $"gapMs={gap.TotalMilliseconds:F0}");
                }
                catch (OperationCanceledException)
                {
                    return;
                }
                catch
                {
                }
            }
        });
    }

    private IDisposable EnterUiPhase(string phase)
    {
        var previous = _currentUiPhase;
        _currentUiPhase = phase;
        return new UiPhaseScope(this, phase, previous);
    }

    private void LeaveUiPhase(string phase, string previous, TimeSpan elapsed)
    {
        _currentUiPhase = previous;
        if (elapsed >= SlowUiPhaseThreshold)
        {
            WritePerformanceLog("phase-duration", $"name={LogText(phase)} elapsedMs={elapsed.TotalMilliseconds:F0}");
        }
    }

    private void WritePerformanceLog(string eventName, string details)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_performanceLogPath)!);
            var line = $"{DateTimeOffset.Now:O}\t{_performanceClock.ElapsedMilliseconds}\t{eventName}\tphase={LogText(_currentUiPhase)}\t{LogText(details, 12000)}{Environment.NewLine}";
            lock (_performanceLogLock)
            {
                File.AppendAllText(_performanceLogPath, line, Encoding.UTF8);
            }
        }
        catch
        {
        }
    }

    private static string LogText(string? value, int maxLength = 4000)
    {
        if (string.IsNullOrEmpty(value))
        {
            return "";
        }

        var sanitized = RedactSensitiveText(value)
            .Replace("\r", "\\r", StringComparison.Ordinal)
            .Replace("\n", "\\n", StringComparison.Ordinal)
            .Replace("\t", "\\t", StringComparison.Ordinal);
        return sanitized.Length <= maxLength
            ? sanitized
            : sanitized[..maxLength] + $"...(truncated {sanitized.Length - maxLength} chars)";
    }

    private static string RedactSensitiveText(string value)
    {
        var redacted = Regex.Replace(
            value,
            @"(?i)\b(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|cookie|password|secret)\b\s*[:=]\s*([^\s,;]+)",
            "$1=<redacted>");
        return Regex.Replace(
            redacted,
            @"(?i)(bearer|token)\s+[A-Za-z0-9._~+/=-]{16,}",
            "$1 <redacted>");
    }

    private static string AttachmentLogText(IEnumerable<MessageAttachmentDto> attachments)
    {
        return string.Join(
            " | ",
            attachments.Select(attachment =>
                $"name={LogText(attachment.Name)} kind={LogText(attachment.Kind)} path={LogText(attachment.Path)} uri={LogText(attachment.Uri)}"));
    }

    private void CleanupOldAttachmentFiles()
    {
        var directory = AttachmentDirectory();
        if (!Directory.Exists(directory))
        {
            return;
        }

        var cutoff = DateTimeOffset.Now - AttachmentRetention;
        var deleted = 0;
        var failed = 0;
        try
        {
            foreach (var file in Directory.EnumerateFiles(directory))
            {
                try
                {
                    var lastWrite = File.GetLastWriteTime(file);
                    if (lastWrite > cutoff.LocalDateTime)
                    {
                        continue;
                    }
                    File.Delete(file);
                    deleted++;
                }
                catch
                {
                    failed++;
                }
            }
            if (deleted > 0 || failed > 0)
            {
                WritePerformanceLog("attachment-cleanup", $"deleted={deleted} failed={failed} retentionDays={AttachmentRetention.TotalDays:F0}");
            }
        }
        catch (Exception ex)
        {
            WritePerformanceLog("attachment-cleanup-error", $"type={LogText(ex.GetType().Name)} message={LogText(ex.Message)}");
        }
    }

    private static string AttachmentDirectory()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "CodexLite", "attachments");
    }

    private async Task RunBusyAsync(string message, Func<Task> action)
    {
        BeginBusy(message);
        try
        {
            await action();
        }
        finally
        {
            EndBusy();
        }
    }

    private async Task RunActivityAsync(string message, Func<Task> action)
    {
        BeginActivity(message);
        try
        {
            await action();
        }
        finally
        {
            EndActivity();
        }
    }

    private async Task<T?> RunActivityAsync<T>(string message, Func<Task<T?>> action)
    {
        BeginActivity(message);
        try
        {
            return await action();
        }
        finally
        {
            EndActivity();
        }
    }

    private void BeginBusy(string message)
    {
        _busyDepth++;
        BeginActivity(message);
    }

    private void BeginActivity(string message)
    {
        _activityDepth++;
        SetBusyMessage(message);
        UpdateActivityProgressVisibility();
    }

    private void SetBusyMessage(string message)
    {
        BusyText.Text = message;
        StatusText.Text = message;
    }

    private void EndBusy()
    {
        _busyDepth = Math.Max(0, _busyDepth - 1);
        if (_busyDepth == 0)
        {
            BusyOverlay.Visibility = Visibility.Collapsed;
        }
        EndActivity();
    }

    private void EndActivity()
    {
        _activityDepth = Math.Max(0, _activityDepth - 1);
        UpdateActivityProgressVisibility();
    }

    private void BeginRunActivity(string chatId, string message)
    {
        _runActivityDepthByChat[chatId] = _runActivityDepthByChat.TryGetValue(chatId, out var depth) ? depth + 1 : 1;
        UpdateChatRunningIndicator(chatId);
        if (_selectedChat?.Id == chatId)
        {
            SetBusyMessage(message);
        }
        UpdateActivityProgressVisibility();
    }

    private void EndRunActivity(string chatId)
    {
        if (_runActivityDepthByChat.TryGetValue(chatId, out var depth) && depth > 1)
        {
            _runActivityDepthByChat[chatId] = depth - 1;
        }
        else
        {
            _runActivityDepthByChat.Remove(chatId);
        }
        UpdateChatRunningIndicator(chatId);
        UpdateActivityProgressVisibility();
    }

    private void UpdateActivityProgressVisibility()
    {
        var hasSelectedRunActivity = _selectedChat is not null
            && (_runActivityDepthByChat.ContainsKey(_selectedChat.Id) || ActiveRunForChat(_selectedChat.Id) is not null);
        ActivityProgress.Visibility = _activityDepth > 0 || hasSelectedRunActivity
            ? Visibility.Visible
            : Visibility.Collapsed;
    }

    private async Task RefreshProjectsAsync()
    {
        await RefreshProjectsAsync(null);
    }

    private async Task RefreshProjectsAsync(string? preferredChatId)
    {
        BeginProjectTreeLoading("プロジェクトを読み込み中...");
        var projectTreeLoadingEnded = false;
        var selectedProjectId = _selectedProject?.Id ?? _persistedSelectedProjectId;
        var selectedChatId = preferredChatId ?? _selectedChat?.Id ?? (selectedProjectId == _persistedSelectedProjectId ? _persistedSelectedChatId : null);
        var expandedProjectIds = GetExpandedProjectIds();
        expandedProjectIds.UnionWith(_persistedExpandedProjectIds);
        if (selectedProjectId is not null && selectedChatId is not null)
        {
            expandedProjectIds.Add(selectedProjectId);
        }
        _isRefreshingTree = true;
        try
        {
            _projectTree.Clear();
            _selectedProject = null;
            _selectedChat = null;
            _messages.Clear();
            UpdateCommandButtonState();
            var projects = OrderProjects(await _client.ListProjectsAsync() ?? []);
            foreach (var project in projects)
            {
                var projectItem = new ProjectTreeItem(project, expandedProjectIds.Contains(project.Id));
                _projectTree.Add(projectItem);
                if (project.Id == selectedProjectId)
                {
                    _selectedProject ??= project;
                }
            }
            if (_selectedProject is null && _projectTree.Count > 0)
            {
                selectedProjectId = null;
                selectedChatId = null;
                _selectedProject = _projectTree[0].Project;
            }
            UpdateCommandButtonState();
            UpdateRightPaneVisibility();
            await Dispatcher.Yield(DispatcherPriority.Background);
            EndProjectTreeLoading();
            projectTreeLoadingEnded = true;

            var selectedProjectItem = selectedProjectId is null
                ? null
                : _projectTree.FirstOrDefault(item => item.Project.Id == selectedProjectId);
            if (selectedProjectItem is not null && selectedChatId is not null)
            {
                await LoadProjectChatsAsync(selectedProjectItem, selectedChatId);
            }
            if (selectedProjectId is not null && selectedChatId is not null)
            {
                var selectedChatItem = _projectTree
                    .FirstOrDefault(item => item.Project.Id == selectedProjectId)?
                    .Chats
                    .FirstOrDefault(item => item.Chat.Id == selectedChatId);
                if (selectedChatItem is not null)
                {
                    _selectedProject = selectedChatItem.Project;
                    _selectedChat = selectedChatItem.Chat;
                }
            }
            await RefreshMessagesAsync();
            await RefreshFilesAsync("");
            await RestoreProjectTreeStateAsync(expandedProjectIds, selectedProjectId, selectedChatId);
            if (selectedProjectId is null && selectedChatId is null && _projectTree.FirstOrDefault() is ProjectTreeItem firstProject)
            {
                await FocusProjectItemInTreeAsync(firstProject);
                await SelectProjectAsync(firstProject.Project);
            }
            ApplySavedTabSelection();
            _hasCompletedInitialProjectRestore = true;
            SaveUiState();
            _ = LoadProjectChatsForTreeInBackgroundAsync(selectedChatId, selectedProjectItem?.Project.Id);
        }
        finally
        {
            _isRefreshingTree = false;
            if (!projectTreeLoadingEnded)
            {
                EndProjectTreeLoading();
            }
        }
    }

    private void BeginProjectTreeLoading(string message)
    {
        _treeLoadingDepth++;
        ProjectTreeLoadingText.Text = message;
        ProjectTreeLoadingOverlay.Visibility = Visibility.Visible;
        ProjectTree.IsEnabled = false;
        ProjectTree.Opacity = 0.55;
        UpdateRightPaneVisibility();
    }

    private void EndProjectTreeLoading()
    {
        _treeLoadingDepth = Math.Max(0, _treeLoadingDepth - 1);
        if (_treeLoadingDepth > 0)
        {
            return;
        }
        ProjectTreeLoadingOverlay.Visibility = Visibility.Collapsed;
        ProjectTree.IsEnabled = true;
        ProjectTree.Opacity = 1;
        UpdateRightPaneVisibility();
        if (_selectedProject is not null && _selectedChat is null)
        {
            _ = RefreshUsageCapacityAsync();
        }
    }

    private async Task<List<ChatDto>?> GetProjectChatsAsync(ProjectTreeItem projectItem, Task<List<ChatDto>?> task)
    {
        try
        {
            return await task;
        }
        catch (Exception ex)
        {
            StatusText.Text = $"chat list error | {projectItem.Name} | {ex.Message}";
            return [];
        }
    }

    private async Task LoadProjectChatsAsync(ProjectTreeItem projectItem, string? preferredChatId = null, bool sync = false)
    {
        var projectId = projectItem.Project.Id;
        var version = _chatLoadVersions.TryGetValue(projectId, out var previousVersion) ? previousVersion + 1 : 1;
        _chatLoadVersions[projectId] = version;
        var chats = await GetProjectChatsAsync(projectItem, _client.ListChatsAsync(projectId, sync)) ?? [];
        if (!_chatLoadVersions.TryGetValue(projectId, out var currentVersion) || currentVersion != version)
        {
            return;
        }

        projectItem.Chats.Clear();
        foreach (var chat in chats)
        {
            if (!ChatMatchesFilter(chat))
            {
                continue;
            }
            var chatItem = new ChatTreeItem(projectItem.Project, chat);
            chatItem.HasUnloadedHistory = _chatsWithUnloadedHistory.Contains(chat.Id);
            chatItem.IsRunning = IsChatRunning(chat.Id);
            projectItem.Chats.Add(chatItem);
            if (chat.Id == preferredChatId)
            {
                _selectedProject = projectItem.Project;
                _selectedChat = chat;
            }
        }
    }

    private async Task LoadProjectChatsForTreeAsync(string? preferredChatId, string? skipProjectId = null, bool sync = false)
    {
        using var gate = new SemaphoreSlim(6);
        var tasks = _projectTree
            .Where(projectItem => projectItem.Project.Id != skipProjectId)
            .Select(async projectItem =>
            {
                await gate.WaitAsync();
                try
                {
                    await LoadProjectChatsAsync(projectItem, preferredChatId, sync);
                }
                finally
                {
                    gate.Release();
                }
            })
            .ToArray();
        await Task.WhenAll(tasks);
    }

    private async Task LoadProjectChatsForTreeInBackgroundAsync(string? preferredChatId, string? skipProjectId = null)
    {
        try
        {
            await LoadProjectChatsForTreeAsync(preferredChatId, skipProjectId);
            await LoadProjectChatsForTreeAsync(preferredChatId, null, sync: true);
        }
        catch (Exception ex)
        {
            WritePerformanceLog("background-chat-list-error", $"type={LogText(ex.GetType().Name)} message={LogText(ex.Message)}");
        }
    }

    private bool ChatMatchesFilter(ChatDto chat)
    {
        var filter = ChatFilterBox.Text.Trim();
        return filter.Length == 0 || chat.Title.Contains(filter, StringComparison.CurrentCultureIgnoreCase);
    }

    private async Task RefreshCurrentProjectAsync()
    {
        await RefreshCurrentProjectAsync(null);
    }

    private async Task RefreshCurrentProjectAsync(string? preferredChatId)
    {
        if (_selectedProject is null)
        {
            return;
        }
        var selectedProjectId = _selectedProject.Id;
        await RefreshProjectsAsync(preferredChatId);
        _selectedProject ??= _projectTree.FirstOrDefault(item => item.Project.Id == selectedProjectId)?.Project;
    }

    private ProjectTreeItem AddProjectToTree(ProjectDto project, bool isExpanded)
    {
        var existing = FindProjectItem(project.Id);
        if (existing is not null)
        {
            existing.IsExpanded = isExpanded || existing.IsExpanded;
            return existing;
        }

        var item = new ProjectTreeItem(project, isExpanded);
        _projectTree.Insert(0, item);
        _persistedProjectOrderIds.Remove(project.Id);
        _persistedProjectOrderIds.Insert(0, project.Id);
        if (isExpanded)
        {
            _persistedExpandedProjectIds.Add(project.Id);
        }
        SaveUiState();
        return item;
    }

    private ProjectTreeItem AddPendingProjectToTree(ProjectDto project)
    {
        var item = new ProjectTreeItem(project, isExpanded: true) { IsPending = true };
        _projectTree.Insert(0, item);
        return item;
    }

    private ProjectTreeItem ReplacePendingProject(ProjectTreeItem pendingItem, ProjectDto project)
    {
        pendingItem.SetProject(project);
        pendingItem.IsExpanded = true;
        _persistedProjectOrderIds.Remove(project.Id);
        _persistedProjectOrderIds.Insert(0, project.Id);
        _persistedExpandedProjectIds.Add(project.Id);
        SaveUiState();
        return pendingItem;
    }

    private static string ProjectNameFromPath(string path)
    {
        var normalized = path.TrimEnd('/', '\\');
        if (normalized.Length == 0)
        {
            return path;
        }
        var name = Path.GetFileName(normalized);
        return string.IsNullOrWhiteSpace(name) ? normalized : name;
    }

    private async Task RemoveProjectFromTreeAsync(string projectId)
    {
        var removedIndex = _projectTree.ToList().FindIndex(item => item.Project.Id == projectId);
        if (removedIndex < 0)
        {
            return;
        }

        var removedWasSelected = _selectedProject?.Id == projectId;
        _projectTree.RemoveAt(removedIndex);
        _persistedExpandedProjectIds.Remove(projectId);
        _persistedProjectOrderIds.Remove(projectId);
        if (!removedWasSelected)
        {
            SaveUiState();
            return;
        }

        _selectedProject = null;
        _selectedChat = null;
        _messages.Clear();
        _files.Clear();
        var nextItem = _projectTree.Count == 0
            ? null
            : _projectTree[Math.Min(removedIndex, _projectTree.Count - 1)];
        if (nextItem is not null)
        {
            await SelectProjectInTreeAsync(nextItem.Project.Id);
        }
        else
        {
            UpdateCommandButtonState();
            UpdateRightPaneVisibility();
        }
        SaveUiState();
    }

    private HashSet<string> GetExpandedProjectIds()
    {
        return _projectTree.Where(item => item.IsExpanded).Select(item => item.Project.Id).ToHashSet();
    }

    private List<ProjectDto> OrderProjects(List<ProjectDto> projects)
    {
        if (_persistedProjectOrderIds.Count == 0)
        {
            return projects;
        }

        var order = _persistedProjectOrderIds
            .Select((id, index) => new { id, index })
            .ToDictionary(item => item.id, item => item.index);
        return projects
            .Select((project, index) => new { project, index })
            .OrderBy(item => order.TryGetValue(item.project.Id, out var savedIndex) ? savedIndex : int.MaxValue)
            .ThenBy(item => item.index)
            .Select(item => item.project)
            .ToList();
    }

    private async Task RestoreProjectTreeStateAsync(HashSet<string> expandedProjectIds, string? selectedProjectId, string? selectedChatId)
    {
        for (var attempt = 0; attempt < 3; attempt++)
        {
            await Dispatcher.InvokeAsync(() =>
            {
                _suppressTreeSelectionChanged = true;
                try
                {
                    ProjectTree.UpdateLayout();
                    foreach (var projectItem in _projectTree)
                    {
                        if (ProjectTree.ItemContainerGenerator.ContainerFromItem(projectItem) is not TreeViewItem container)
                        {
                            continue;
                        }

                        container.IsExpanded = projectItem.IsExpanded;
                        if (projectItem.Project.Id == selectedProjectId && selectedChatId is null)
                        {
                            container.IsSelected = true;
                            container.Focus();
                        }

                        if (projectItem.Project.Id != selectedProjectId || selectedChatId is null)
                        {
                            continue;
                        }

                        projectItem.IsExpanded = true;
                        container.IsExpanded = true;
                        container.UpdateLayout();
                        foreach (var chatItem in projectItem.Chats)
                        {
                            if (chatItem.Chat.Id == selectedChatId && container.ItemContainerGenerator.ContainerFromItem(chatItem) is TreeViewItem chatContainer)
                            {
                                chatContainer.IsSelected = true;
                                chatContainer.Focus();
                                break;
                            }
                        }
                    }
                }
                finally
                {
                    _suppressTreeSelectionChanged = false;
                }
            }, DispatcherPriority.Loaded);
        }

        if (selectedProjectId is null)
        {
            return;
        }
        if (selectedChatId is not null)
        {
            var selectedProject = _projectTree.FirstOrDefault(item => item.Project.Id == selectedProjectId);
            var selectedChat = selectedProject?.Chats.FirstOrDefault(item => item.Chat.Id == selectedChatId);
            if (selectedChat is not null)
            {
                await SelectTreeItemAsync(selectedChat);
                return;
            }
        }
        if (_projectTree.FirstOrDefault(item => item.Project.Id == selectedProjectId) is ProjectTreeItem projectItem)
        {
            await SelectTreeItemAsync(projectItem);
        }
    }

    private async void ProjectTreeItem_Expanded(object sender, RoutedEventArgs e)
    {
        if (_isRefreshingTree)
        {
            return;
        }

        if (e.OriginalSource is TreeViewItem { DataContext: ProjectTreeItem item })
        {
            item.IsExpanded = true;
            _persistedExpandedProjectIds.Add(item.Project.Id);
            SaveUiState();
            await LoadProjectChatsAsync(item);
        }
    }

    private void ProjectTreeItem_Collapsed(object sender, RoutedEventArgs e)
    {
        if (_isRefreshingTree)
        {
            return;
        }

        if (e.OriginalSource is TreeViewItem { DataContext: ProjectTreeItem item })
        {
            item.IsExpanded = false;
            _persistedExpandedProjectIds.Remove(item.Project.Id);
            SaveUiState();
        }
    }

    private void LoadUiState()
    {
        _isLoadingUiState = true;
        try
        {
            if (!File.Exists(_uiStatePath))
            {
                return;
            }

            var state = JsonSerializer.Deserialize<UiState>(File.ReadAllText(_uiStatePath), _json);
            _persistedExpandedProjectIds.Clear();
            foreach (var projectId in state?.ExpandedProjectIds ?? [])
            {
                _persistedExpandedProjectIds.Add(projectId);
            }
            _persistedProjectOrderIds.Clear();
            foreach (var projectId in state?.ProjectOrderIds ?? [])
            {
                _persistedProjectOrderIds.Add(projectId);
            }
            _textSizeSetting = NormalizeTextSize(state?.TextSize);
            ApplyTextSizeSetting(_textSizeSetting);
            _codexHomeMode = NormalizeCodexHomeMode(state?.CodexHomeMode) ?? DefaultCodexHomeMode();
            _wrapFileText = state?.WrapFileText ?? false;
            _persistedSelectedProjectId = string.IsNullOrWhiteSpace(state?.SelectedProjectId) ? null : state.SelectedProjectId;
            _persistedSelectedChatId = string.IsNullOrWhiteSpace(state?.SelectedChatId) ? null : state.SelectedChatId;
            _persistedSelectedTab = NormalizeMainTabName(state?.SelectedTab);
            ApplyFileTextWrapping();
            ApplyWindowPlacement(state?.Window);
            ApplyTreePaneWidth(state?.TreePaneWidth);
        }
        catch
        {
            _persistedExpandedProjectIds.Clear();
            _persistedProjectOrderIds.Clear();
            _textSizeSetting = "small";
            ApplyTextSizeSetting(_textSizeSetting);
            _codexHomeMode = DefaultCodexHomeMode();
            _wrapFileText = false;
            _persistedSelectedProjectId = null;
            _persistedSelectedChatId = null;
            _persistedSelectedTab = null;
            ApplyFileTextWrapping();
        }
        finally
        {
            _isLoadingUiState = false;
        }
    }

    private void SaveUiState()
    {
        if (_isLoadingUiState)
        {
            return;
        }
        var selectedProjectId = _selectedProject?.Id ?? (!_hasCompletedInitialProjectRestore ? _persistedSelectedProjectId : null);
        var selectedChatId = _selectedChat?.Id ?? (!_hasCompletedInitialProjectRestore ? _persistedSelectedChatId : null);
        var selectedTab = CurrentMainTabName();
        if (!_hasCompletedInitialProjectRestore && _selectedProject is null && _persistedSelectedTab is not null)
        {
            selectedTab = _persistedSelectedTab;
        }
        _persistedSelectedProjectId = selectedProjectId;
        _persistedSelectedChatId = selectedChatId;
        _persistedSelectedTab = selectedTab;
        Directory.CreateDirectory(Path.GetDirectoryName(_uiStatePath)!);
        if (_projectTree.Count > 0)
        {
            _persistedProjectOrderIds.Clear();
            _persistedProjectOrderIds.AddRange(_projectTree.Select(item => item.Project.Id));
        }
        var state = new UiState(
            _persistedExpandedProjectIds.OrderBy(id => id).ToList(),
            _persistedProjectOrderIds.ToList(),
            CaptureWindowPlacement(),
            _textSizeSetting,
            _wrapFileText,
            CaptureTreePaneWidth(),
            _codexHomeMode,
            selectedProjectId,
            selectedChatId,
            selectedTab);
        File.WriteAllText(_uiStatePath, JsonSerializer.Serialize(state, _json));
    }

    private WindowPlacementState CaptureWindowPlacement()
    {
        var bounds = WindowState == WindowState.Normal ? new Rect(Left, Top, Width, Height) : RestoreBounds;
        return new WindowPlacementState(
            bounds.Left,
            bounds.Top,
            bounds.Width,
            bounds.Height,
            WindowState == WindowState.Maximized);
    }

    private void ApplyWindowPlacement(WindowPlacementState? state)
    {
        if (state is null)
        {
            return;
        }
        if (state.Width >= MinWidth)
        {
            Width = state.Width;
        }
        if (state.Height >= MinHeight)
        {
            Height = state.Height;
        }
        if (IsReasonableScreenCoordinate(state.Left, state.Top))
        {
            Left = state.Left;
            Top = state.Top;
            WindowStartupLocation = WindowStartupLocation.Manual;
        }
        WindowState = state.IsMaximized ? WindowState.Maximized : WindowState.Normal;
    }

    private double CaptureTreePaneWidth()
    {
        var width = TreePaneColumn.ActualWidth > 0 ? TreePaneColumn.ActualWidth : TreePaneColumn.Width.Value;
        return Math.Clamp(width, TreePaneColumn.MinWidth, Math.Max(TreePaneColumn.MinWidth, ActualWidth - 260));
    }

    private void ApplyTreePaneWidth(double? width)
    {
        if (width is not { } value || double.IsNaN(value) || double.IsInfinity(value))
        {
            return;
        }

        var maxWidth = Math.Max(TreePaneColumn.MinWidth, SystemParameters.WorkArea.Width - 260);
        TreePaneColumn.Width = new GridLength(Math.Clamp(value, TreePaneColumn.MinWidth, maxWidth), GridUnitType.Pixel);
    }

    private void MainGridSplitter_DragCompleted(object sender, System.Windows.Controls.Primitives.DragCompletedEventArgs e)
    {
        SaveUiState();
    }

    private string CurrentMainTabName()
    {
        if (ReferenceEquals(MainTabs.SelectedItem, FilesTab))
        {
            return "files";
        }
        if (ReferenceEquals(MainTabs.SelectedItem, AutomationTab))
        {
            return "automation";
        }
        if (ReferenceEquals(MainTabs.SelectedItem, DiagnosticsTab))
        {
            return "diagnostics";
        }
        return "conversation";
    }

    private static string? NormalizeMainTabName(string? value)
    {
        return value is "conversation" or "files" or "automation" or "diagnostics" ? value : null;
    }

    private void ApplySavedTabSelection()
    {
        var tab = NormalizeMainTabName(_persistedSelectedTab) ?? "conversation";
        if (tab == "automation" && _selectedChat is null)
        {
            tab = "conversation";
        }
        MainTabs.SelectedItem = tab switch
        {
            "files" => FilesTab,
            "automation" => AutomationTab,
            "diagnostics" => DiagnosticsTab,
            _ => ConversationTab
        };
    }

    private static bool IsReasonableScreenCoordinate(double left, double top)
    {
        return left > -10000
            && top > -10000
            && left < SystemParameters.VirtualScreenLeft + SystemParameters.VirtualScreenWidth
            && top < SystemParameters.VirtualScreenTop + SystemParameters.VirtualScreenHeight;
    }

    private void PersistExpandedStateFromTree()
    {
        SaveUiState();
    }

    private void Settings_Click(object sender, RoutedEventArgs e)
    {
        ShowSettingsWindow();
    }

    private void TextSizeRadio_Checked(object sender, RoutedEventArgs e)
    {
        if (sender is not RadioButton { Tag: string size })
        {
            return;
        }
        _textSizeSetting = NormalizeTextSize(size);
        ApplyTextSizeSetting(_textSizeSetting);
        SaveUiState();
    }

    private void ApplyTextSizeSetting(string size)
    {
        FontSize = size switch
        {
            "large" => 16,
            "medium" => 14,
            _ => 12
        };
    }

    private void FileWrapCheckBox_Changed(object sender, RoutedEventArgs e)
    {
        if (_isLoadingUiState || !FileWrapCheckBox.IsEnabled)
        {
            return;
        }

        _wrapFileText = FileWrapCheckBox.IsChecked == true;
        ApplyFileTextWrapping();
        SaveUiState();
    }

    private void ApplyFileTextWrapping()
    {
        if (FileContentBox is null || FileWrapCheckBox is null)
        {
            return;
        }

        FileWrapCheckBox.IsChecked = _wrapFileText;
        FileContentBox.TextWrapping = _wrapFileText ? TextWrapping.Wrap : TextWrapping.NoWrap;
        FileContentBox.HorizontalScrollBarVisibility = _wrapFileText ? ScrollBarVisibility.Disabled : ScrollBarVisibility.Auto;
    }

    private static string NormalizeTextSize(string? value)
    {
        return value is "medium" or "large" ? value : "small";
    }

    private static string? NormalizeCodexHomeMode(string? value)
    {
        return value is "windows" or "wsl" ? value : null;
    }

    private static string DefaultCodexHomeMode()
    {
        var path = WindowsCodexHomePath();
        return !string.IsNullOrWhiteSpace(path) && Directory.Exists(path)
            ? "windows"
            : "wsl";
    }

    private static string WindowsCodexHomePath()
    {
        var windowsHome = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        return string.IsNullOrWhiteSpace(windowsHome) ? "" : Path.Combine(windowsHome, ".codex");
    }

    private void ShowSettingsWindow()
    {
        var dialog = new Window
        {
            Owner = this,
            Title = "設定",
            Width = 440,
            Height = 260,
            MinWidth = 320,
            MinHeight = 220,
            WindowStartupLocation = WindowStartupLocation.CenterOwner,
            ResizeMode = ResizeMode.NoResize,
            FontSize = FontSize
        };
        var panel = new DockPanel { Margin = new Thickness(14) };
        var close = new Button { Content = "閉じる", IsDefault = true, MinWidth = 80 };
        close.Click += (_, _) => dialog.Close();
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = System.Windows.HorizontalAlignment.Right };
        buttons.Children.Add(close);
        DockPanel.SetDock(buttons, Dock.Bottom);
        panel.Children.Add(buttons);

        var content = new StackPanel();
        content.Children.Add(new TextBlock { Text = "文字サイズ", FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 0, 0, 8) });
        var choices = new StackPanel { Orientation = Orientation.Horizontal };
        foreach (var (label, value) in new[] { ("小", "small"), ("中", "medium"), ("大", "large") })
        {
            var radio = new RadioButton
            {
                Content = label,
                Tag = value,
                GroupName = "AppTextSize",
                IsChecked = _textSizeSetting == value,
                Margin = new Thickness(0, 0, 16, 0)
            };
            radio.Checked += TextSizeRadio_Checked;
            choices.Children.Add(radio);
        }
        content.Children.Add(choices);
        content.Children.Add(new TextBlock { Text = "CODEX_HOME", FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 18, 0, 8) });
        var codexChoices = new StackPanel();
        foreach (var (label, value, description) in new[]
        {
            ("Windows側 .codex", "windows", WindowsCodexHomePath()),
            ("WSL側 .codex", "wsl", "$HOME/.codex")
        })
        {
            var radio = new RadioButton
            {
                Content = $"{label}  {description}",
                Tag = value,
                GroupName = "CodexHomeMode",
                IsChecked = _codexHomeMode == value,
                Margin = new Thickness(0, 0, 0, 6)
            };
            radio.Checked += CodexHomeModeRadio_Checked;
            codexChoices.Children.Add(radio);
        }
        content.Children.Add(codexChoices);
        panel.Children.Add(content);
        dialog.Content = panel;
        dialog.ShowDialog();
    }

    private async void CodexHomeModeRadio_Checked(object sender, RoutedEventArgs e)
    {
        if (_isLoadingUiState || sender is not RadioButton { Tag: string mode })
        {
            return;
        }

        var normalized = NormalizeCodexHomeMode(mode);
        if (normalized is null || normalized == _codexHomeMode)
        {
            return;
        }

        _codexHomeMode = normalized;
        _client.CodexHomeMode = _codexHomeMode;
        SaveUiState();
        await RestartDaemonAndReloadAsync();
    }

    private async Task RestartDaemonAndReloadAsync()
    {
        try
        {
            _isRestartingDaemon = true;
            _messageRefreshTimer.Stop();
            await RunBusyAsync("CODEX_HOMEを切り替え中...", async () =>
            {
                await _client.ShutdownDaemonAsync();
                _projectTree.Clear();
                _selectedProject = null;
                _selectedChat = null;
                _messages.Clear();
                _files.Clear();
                _automations.Clear();
                UpdateCommandButtonState();
                UpdateAutomationButtonState();
                UpdateRightPaneVisibility();
                await InitializeAsync();
            });
        }
        catch (Exception ex)
        {
            StatusText.Text = $"codex home error | {ShortError(ex)}";
        }
        finally
        {
            _isRestartingDaemon = false;
            _messageRefreshTimer.Start();
        }
    }

    private async Task SelectProjectAsync(ProjectDto project)
    {
        _selectedProject = project;
        _selectedChat = null;
        _messages.Clear();
        _automations.Clear();
        UpdateAutomationButtonState();
        if (FindProjectItem(project.Id) is ProjectTreeItem projectItem)
        {
            await LoadProjectChatsAsync(projectItem);
        }
        UpdateCommandButtonState();
        UpdateRightPaneVisibility();
        _ = RefreshUsageCapacityAsync();
        await RefreshFilesAsync("");
        StatusText.Text = $"project | {project.Name} | {project.Path}";
        NewChatMessageBox.Focus();
        SaveUiState();
    }

    private async Task SelectChatAsync(ChatTreeItem item)
    {
        if (FindProjectItem(item.Project.Id) is ProjectTreeItem projectItem)
        {
            var currentItem = projectItem.Chats.FirstOrDefault(chatItem => chatItem.Chat.Id == item.Chat.Id);
            if (currentItem is null)
            {
                _selectedProject = projectItem.Project;
                _selectedChat = null;
                _messages.Clear();
                _automations.Clear();
                UpdateCommandButtonState();
                UpdateAutomationButtonState();
                UpdateRightPaneVisibility();
                StatusText.Text = $"chat is no longer active | {item.Chat.Title}";
                return;
            }
            item = currentItem;
        }
        _selectedProject = item.Project;
        _selectedChat = item.Chat;
        UpdateCommandButtonState();
        UpdateRightPaneVisibility();
        MarkSelectedConversationSeen();
        await RefreshMessagesAsync();
        await RefreshFilesAsync("");
        await RefreshAutomationsAsync();
        StatusText.Text = item.Chat.CanContinue
            ? $"chat | {item.Project.Name} | {item.Chat.Title}"
            : $"chat | {item.Project.Name} | {item.Chat.Title} | read-only | {item.Chat.ContinueDisabledReason}";
        SaveUiState();
    }

    private void UpdateRightPaneVisibility()
    {
        if (_treeLoadingDepth > 0)
        {
            MainTabs.Visibility = Visibility.Collapsed;
            ProjectComposerPanel.Visibility = Visibility.Collapsed;
            ChatConversationPanel.Visibility = Visibility.Collapsed;
            NoChatPlaceholder.Visibility = Visibility.Collapsed;
            UpdateUsageRefreshButtonState();
            return;
        }

        var hasProject = _selectedProject is not null;
        var hasChat = _selectedChat is not null;
        MainTabs.Visibility = hasProject ? Visibility.Visible : Visibility.Collapsed;
        ProjectComposerPanel.Visibility = hasProject && !hasChat ? Visibility.Visible : Visibility.Collapsed;
        ChatConversationPanel.Visibility = hasChat ? Visibility.Visible : Visibility.Collapsed;
        AutomationTab.IsEnabled = hasChat;
        if (!hasChat && ReferenceEquals(MainTabs.SelectedItem, AutomationTab))
        {
            ConversationTab.IsSelected = true;
        }
        NoChatPlaceholder.Visibility = hasProject ? Visibility.Collapsed : Visibility.Visible;
        if (!hasProject || hasChat)
        {
            SetUsageCapacityText("未取得", "未取得");
        }
        UpdateUsageRefreshButtonState();
    }

    private async Task RefreshUsageCapacityAsync()
    {
        if (_isRefreshingUsageCapacity || _treeLoadingDepth > 0 || _selectedProject is null || _selectedChat is not null)
        {
            return;
        }
        _isRefreshingUsageCapacity = true;
        UpdateUsageRefreshButtonState();
        _usageCapacityCts?.Cancel();
        _usageCapacityCts?.Dispose();
        var cts = new CancellationTokenSource(UsageCapacityTimeout);
        _usageCapacityCts = cts;
        SetUsageCapacityText("取得中...", "取得中...");
        try
        {
            var usage = await _client.GetUsageCapacityAsync(cts.Token);
            if (cts.IsCancellationRequested || _selectedProject is null || _selectedChat is not null)
            {
                return;
            }
            _fiveHourUsageWindow = usage?.FiveHour;
            _weeklyUsageWindow = usage?.Weekly;
            SetUsageCapacityText(FormatUsageWindow(_fiveHourUsageWindow), FormatUsageWindow(_weeklyUsageWindow));
            RedrawUsageGraphs();
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception ex)
        {
            if (!cts.IsCancellationRequested)
            {
                SetUsageCapacityText("取得不可", "取得不可");
                _fiveHourUsageWindow = null;
                _weeklyUsageWindow = null;
                RedrawUsageGraphs();
                StatusText.Text = $"usage error | {ShortError(ex)}";
            }
        }
        finally
        {
            if (ReferenceEquals(_usageCapacityCts, cts))
            {
                _usageCapacityCts = null;
            }
            _isRefreshingUsageCapacity = false;
            UpdateUsageRefreshButtonState();
            cts.Dispose();
        }
    }

    private async void RefreshUsage_Click(object sender, RoutedEventArgs e)
    {
        await RefreshUsageCapacityAsync();
    }

    private void UpdateUsageRefreshButtonState()
    {
        RefreshUsageButton.IsEnabled = !_isRefreshingUsageCapacity
            && _treeLoadingDepth == 0
            && _selectedProject is not null
            && _selectedChat is null;
    }

    private void SetUsageCapacityText(string fiveHour, string weekly)
    {
        FiveHourUsageText.Text = fiveHour;
        WeeklyUsageText.Text = weekly;
        if (fiveHour is "未取得" or "取得中..." or "取得不可")
        {
            _fiveHourUsageWindow = null;
        }
        if (weekly is "未取得" or "取得中..." or "取得不可")
        {
            _weeklyUsageWindow = null;
        }
        RedrawUsageGraphs();
    }

    private void UsageGraph_SizeChanged(object sender, SizeChangedEventArgs e)
    {
        RedrawUsageGraphs();
    }

    private void RedrawUsageGraphs()
    {
        DrawUsageGraph(FiveHourUsageGraph, _fiveHourUsageWindow);
        DrawUsageGraph(WeeklyUsageGraph, _weeklyUsageWindow);
    }

    private static void DrawUsageGraph(Canvas canvas, UsageWindowDto? window)
    {
        canvas.Children.Clear();
        var width = canvas.ActualWidth;
        var height = canvas.ActualHeight;
        if (width <= 0 || height <= 0)
        {
            return;
        }

        const double left = 36;
        const double rightPadding = 12;
        const double top = 8;
        const double bottom = 22;
        var plotWidth = Math.Max(1, width - left - rightPadding);
        var plotHeight = Math.Max(1, height - top - bottom);
        var x0 = left;
        var y0 = top + plotHeight;
        var x1 = left + plotWidth;
        var y1 = top;

        AddLine(canvas, x0, y0, x1, y0, "#C6CEDA", 1);
        AddLine(canvas, x0, y0, x0, y1, "#C6CEDA", 1);
        AddText(canvas, "100%", 0, y1 - 7, "#667085", 10);
        AddText(canvas, "0%", 12, y0 - 7, "#667085", 10);

        if (window is null)
        {
            AddText(canvas, "未取得", left + plotWidth / 2 - 18, top + plotHeight / 2 - 8, "#667085", 12);
            return;
        }

        AddLine(canvas, x0, y1, x1, y0, "#AAB4C3", 1.2);

        var now = DateTimeOffset.Now;
        var resetsAt = ParseUsageTime(window.ResetsAt);
        var startAt = resetsAt?.AddMinutes(-window.WindowMinutes);
        if (resetsAt is null || startAt is null)
        {
            AddText(canvas, "時刻なし", left + plotWidth / 2 - 24, top + plotHeight / 2 - 8, "#667085", 12);
            return;
        }

        var totalSeconds = Math.Max(1, (resetsAt.Value - startAt.Value).TotalSeconds);
        var elapsedRatio = Math.Clamp((now - startAt.Value).TotalSeconds / totalSeconds, 0, 1);
        var remainingRatio = Math.Clamp(window.RemainingPercent / 100.0, 0, 1);
        var currentX = x0 + plotWidth * elapsedRatio;
        var currentY = y0 - plotHeight * remainingRatio;

        AddLine(canvas, currentX, y1, currentX, y0, "#D0D5DD", 1, dash: true);
        AddLine(canvas, x0, currentY, currentX, currentY, "#D0D5DD", 1, dash: true);
        AddPoint(canvas, currentX, currentY);

        AddText(canvas, FormatUsageAxisTime(startAt.Value), x0, y0 + 5, "#667085", 10);
        AddText(canvas, FormatUsageAxisTime(resetsAt.Value), Math.Max(left, x1 - 54), y0 + 5, "#667085", 10);
    }

    private static DateTimeOffset? ParseUsageTime(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return null;
        }
        return DateTimeOffset.TryParse(
            value,
            CultureInfo.InvariantCulture,
            DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
            out var timestamp)
            ? timestamp.ToLocalTime()
            : null;
    }

    private static string FormatUsageAxisTime(DateTimeOffset value) =>
        value.ToString("MM/dd HH:mm", CultureInfo.CurrentCulture);

    private static void AddLine(Canvas canvas, double x1, double y1, double x2, double y2, string color, double thickness, bool dash = false)
    {
        var line = new System.Windows.Shapes.Line
        {
            X1 = x1,
            Y1 = y1,
            X2 = x2,
            Y2 = y2,
            Stroke = (System.Windows.Media.Brush)new BrushConverter().ConvertFromString(color)!,
            StrokeThickness = thickness,
            SnapsToDevicePixels = true
        };
        if (dash)
        {
            line.StrokeDashArray = new DoubleCollection { 3, 3 };
        }
        canvas.Children.Add(line);
    }

    private static void AddPoint(Canvas canvas, double x, double y)
    {
        var ellipse = new System.Windows.Shapes.Ellipse
        {
            Width = 9,
            Height = 9,
            Fill = System.Windows.Media.Brushes.White,
            Stroke = System.Windows.Media.Brushes.DodgerBlue,
            StrokeThickness = 2
        };
        Canvas.SetLeft(ellipse, x - ellipse.Width / 2);
        Canvas.SetTop(ellipse, y - ellipse.Height / 2);
        canvas.Children.Add(ellipse);
    }

    private static void AddText(Canvas canvas, string text, double x, double y, string color, double fontSize)
    {
        var block = new TextBlock
        {
            Text = text,
            Foreground = (System.Windows.Media.Brush)new BrushConverter().ConvertFromString(color)!,
            FontSize = fontSize
        };
        Canvas.SetLeft(block, x);
        Canvas.SetTop(block, y);
        canvas.Children.Add(block);
    }

    private static string FormatUsageWindow(UsageWindowDto? window)
    {
        if (window is null)
        {
            return "未取得";
        }
        var remaining = Math.Round(window.RemainingPercent);
        var used = Math.Round(window.UsedPercent);
        var reset = FormatResetTime(window.ResetsAt);
        return reset.Length > 0
            ? $"残り {remaining:0}%（使用 {used:0}%） リセット {reset}"
            : $"残り {remaining:0}%（使用 {used:0}%）";
    }

    private static string FormatResetTime(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return "";
        }
        return DateTimeOffset.TryParse(
            value,
            CultureInfo.InvariantCulture,
            DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
            out var timestamp)
            ? timestamp.ToLocalTime().ToString("MM/dd HH:mm", CultureInfo.CurrentCulture)
            : "";
    }

    private void UpdateCommandButtonState()
    {
        var hasProjectContext = _selectedProject is not null;
        var canContinueChat = _selectedChat is { CanContinue: true };
        var canStartNewChat = _selectedProject is not null && _selectedChat is null;
        var selectedRun = SelectedActiveRun();
        var hasLiteRun = _activeRunsByChat.Count > 0;
        var isViewingActiveRunChat = selectedRun is not null;
        var hasChatComposerContent = HasChatComposerContent();
        var hasNewChatComposerContent = HasNewChatComposerContent();
        var externalProcessing = IsLikelyExternalProcessing();
        var canSendOrSteer = canContinueChat
            && hasChatComposerContent
            && !externalProcessing
            && (_selectedChat is null || selectedRun is null || isViewingActiveRunChat);
        var canCreateAndSend = canStartNewChat
            && hasNewChatComposerContent;
        OpenProjectExplorerButton.IsEnabled = hasProjectContext;
        OpenProjectCodeButton.IsEnabled = hasProjectContext;
        OpenFileInCodeButton.IsEnabled = hasProjectContext && _currentFilePath.Length > 0;
        OpenFileExternalButton.IsEnabled = hasProjectContext && _currentFilePath.Length > 0;
        MessageBox.IsEnabled = canContinueChat;
        SendButton.Content = isViewingActiveRunChat ? "追加指示" : "送信";
        SendButton.IsEnabled = canSendOrSteer;
        SendButton.ToolTip = !canContinueChat
            ? "このチャットはCodex Liteから継続できません。"
            : externalProcessing
            ? "他のアプリで開始された処理中のため、Codex Liteからは送信できません。"
            : !hasChatComposerContent
            ? "メッセージを入力するか、ファイルを添付してください。"
            : isViewingActiveRunChat
            ? "Codex Liteで開始した応答へ追加指示を送ります。"
            : "新しいメッセージを送信します。Codexアプリ側で開始した処理への追加指示にはなりません。";
        NewChatMessageBox.IsEnabled = canStartNewChat;
        NewChatSendButton.IsEnabled = canCreateAndSend;
        NewChatSendButton.ToolTip = !canStartNewChat
            ? "プロジェクトを選択してください。"
            : !hasNewChatComposerContent
            ? "メッセージを入力するか、ファイルを添付してください。"
            : "最初のメッセージで新しいチャットを作成して送信します。";
        CancelButton.IsEnabled = isViewingActiveRunChat;
        CancelButton.ToolTip = isViewingActiveRunChat
            ? "Codex Liteで開始した応答を停止します。"
            : hasLiteRun
            ? "別チャットでCodex Liteの応答が実行中です。停止するにはそのチャットを選択してください。"
            : "Codex Liteで開始した応答中のみ停止できます。Codexアプリ側で開始した処理はここから停止できません。";
        UpdateComposerHint(canContinueChat, canStartNewChat, hasChatComposerContent, hasNewChatComposerContent, externalProcessing, hasLiteRun, isViewingActiveRunChat);
        SyncSelectedRunProgress(selectedRun);
    }

    private ActiveUiRun? SelectedActiveRun()
    {
        return _selectedChat is null ? null : ActiveRunForChat(_selectedChat.Id);
    }

    private ActiveUiRun? ActiveRunForChat(string chatId)
    {
        return _activeRunsByChat.TryGetValue(chatId, out var run) ? run : null;
    }

    private void SyncSelectedRunProgress(ActiveUiRun? activeRun)
    {
        var selectedChatId = _selectedChat?.Id;
        if (selectedChatId is not null
            && (activeRun is not null || _runActivityDepthByChat.ContainsKey(selectedChatId))
            && _runProgressTextByChat.TryGetValue(selectedChatId, out var progressText))
        {
            ShowRunProgress(progressText);
        }
        else
        {
            HideRunProgress();
        }
        UpdateActivityProgressVisibility();
    }

    private bool HasComposerContent()
    {
        return _selectedChat is null ? HasNewChatComposerContent() : HasChatComposerContent();
    }

    private bool HasChatComposerContent()
    {
        return !string.IsNullOrWhiteSpace(MessageBox.Text) || _pendingAttachments.Count > 0;
    }

    private bool HasNewChatComposerContent()
    {
        return !string.IsNullOrWhiteSpace(NewChatMessageBox.Text) || _pendingAttachments.Count > 0;
    }

    private bool IsLikelyExternalProcessing()
    {
        // Do not infer external processing from transcript shape. A recent user
        // message without a visible assistant reply can also mean a failed,
        // interrupted, or delayed sync, and disabling input makes recovery hard.
        return false;
    }

    private void UpdateComposerHint(bool canContinueChat, bool canStartNewChat, bool hasChatComposerContent, bool hasNewChatComposerContent, bool externalProcessing, bool hasLiteRun, bool isViewingActiveRunChat)
    {
        if (_selectedProject is null)
        {
            RemoveComposerHint();
            RemoveNewChatComposerHint();
            return;
        }
        if (canStartNewChat)
        {
            RemoveComposerHint();
            if (!hasNewChatComposerContent)
            {
                SetNewChatComposerHint("何か入力してください");
            }
            else
            {
                RemoveNewChatComposerHint();
            }
            return;
        }
        RemoveNewChatComposerHint();
        if (!canContinueChat)
        {
            RemoveComposerHint();
            return;
        }
        if (externalProcessing)
        {
            SetComposerHint("他のアプリで処理中です");
            return;
        }
        if (!hasChatComposerContent)
        {
            SetComposerHint("何か入力してください");
            return;
        }
        RemoveComposerHint();
    }

    private void SetComposerHint(string content)
    {
        ComposerHintText.Text = content;
        ComposerHintText.Visibility = Visibility.Visible;
    }

    private void RemoveComposerHint()
    {
        ComposerHintText.Text = "";
        ComposerHintText.Visibility = Visibility.Hidden;
    }

    private void SetNewChatComposerHint(string content)
    {
        NewChatComposerHintText.Text = content;
        NewChatComposerHintText.Visibility = Visibility.Visible;
    }

    private void RemoveNewChatComposerHint()
    {
        NewChatComposerHintText.Text = "";
        NewChatComposerHintText.Visibility = Visibility.Hidden;
    }

    private void ShowRunProgress(string message)
    {
        RunProgressPanel.Visibility = Visibility.Visible;
        RunProgressText.Text = message;
    }

    private void ShowRunProgressForChat(string chatId, string message)
    {
        _runProgressTextByChat[chatId] = message;
        if (_selectedChat?.Id != chatId || ActiveRunForChat(chatId) is null)
        {
            return;
        }
        ShowRunProgress(message);
    }

    private void ShowPendingRunProgressForChat(string chatId, string message)
    {
        _runProgressTextByChat[chatId] = message;
        if (_selectedChat?.Id == chatId)
        {
            ShowRunProgress(message);
        }
    }

    private void HideRunProgress()
    {
        RunProgressPanel.Visibility = Visibility.Collapsed;
        RunProgressText.Text = "";
        _runProgress.Clear();
    }

    private void HideRunProgressForChat(string chatId)
    {
        _runProgressTextByChat.Remove(chatId);
        if (_selectedChat?.Id == chatId)
        {
            HideRunProgress();
        }
    }

    private async void ProjectTree_SelectedItemChanged(object sender, RoutedPropertyChangedEventArgs<object> e)
    {
        if (_suppressTreeSelectionChanged)
        {
            return;
        }

        try
        {
            if (e.NewValue is ProjectTreeItem projectItem)
            {
                await SelectProjectAsync(projectItem.Project);
            }
            else if (e.NewValue is ChatTreeItem chatItem)
            {
                await SelectChatAsync(chatItem);
            }
        }
        catch (Exception ex)
        {
            StatusText.Text = $"selection error | {ShortError(ex)}";
        }
    }

    private void ProjectTree_PreviewMouseDown(object sender, MouseButtonEventArgs e)
    {
        _projectDragStart = e.GetPosition(ProjectTree);
        _projectDragItem = ProjectItemFromOriginalSource(e.OriginalSource);
        ProjectTree.Focus();
    }

    private void ProjectTree_PreviewMouseMove(object sender, System.Windows.Input.MouseEventArgs e)
    {
        if (e.LeftButton != MouseButtonState.Pressed || _projectDragStart is not Point start || _projectDragItem is null)
        {
            return;
        }

        var current = e.GetPosition(ProjectTree);
        if (Math.Abs(current.X - start.X) < SystemParameters.MinimumHorizontalDragDistance &&
            Math.Abs(current.Y - start.Y) < SystemParameters.MinimumVerticalDragDistance)
        {
            return;
        }

        DragDrop.DoDragDrop(ProjectTree, _projectDragItem.Project.Id, DragDropEffects.Move);
        ClearProjectDropIndicators();
        _projectDragStart = null;
        _projectDragItem = null;
    }

    private void ProjectTree_DragOver(object sender, System.Windows.DragEventArgs e)
    {
        var sourceProjectId = e.Data.GetData(typeof(string)) as string;
        var targetProject = ProjectDropTargetFromOriginalSource(e.OriginalSource);
        var isProjectHeaderTarget = ProjectItemFromOriginalSource(e.OriginalSource) is not null;
        ClearProjectDropIndicators();
        var placement = ProjectDropPlacement(e, targetProject, isProjectHeaderTarget);
        if (targetProject is not null && placement is not null)
        {
            if (isProjectHeaderTarget)
            {
                targetProject.ShowDropBefore = placement == ProjectDropPlacementKind.Before;
                targetProject.ShowDropAfter = placement == ProjectDropPlacementKind.After;
            }
            else if (targetProject.Chats.LastOrDefault() is ChatTreeItem lastChat)
            {
                lastChat.ShowDropAfter = true;
            }
        }
        e.Effects = sourceProjectId is not null && targetProject is not null && targetProject.Project.Id != sourceProjectId
            ? DragDropEffects.Move
            : DragDropEffects.None;
        e.Handled = true;
    }

    private void ProjectTree_Drop(object sender, System.Windows.DragEventArgs e)
    {
        var sourceProjectId = e.Data.GetData(typeof(string)) as string;
        var targetProject = ProjectDropTargetFromOriginalSource(e.OriginalSource);
        var isProjectHeaderTarget = ProjectItemFromOriginalSource(e.OriginalSource) is not null;
        if (sourceProjectId is null || targetProject is null || targetProject.Project.Id == sourceProjectId)
        {
            return;
        }

        var sourceIndex = _projectTree.ToList().FindIndex(item => item.Project.Id == sourceProjectId);
        var targetIndex = _projectTree.ToList().FindIndex(item => item.Project.Id == targetProject.Project.Id);
        var placement = ProjectDropPlacement(e, targetProject, isProjectHeaderTarget);
        ClearProjectDropIndicators();
        if (sourceIndex < 0 || targetIndex < 0 || placement is null)
        {
            return;
        }

        var insertIndex = placement == ProjectDropPlacementKind.Before ? targetIndex : targetIndex + 1;
        if (sourceIndex < insertIndex)
        {
            insertIndex--;
        }
        if (sourceIndex == insertIndex)
        {
            return;
        }

        _projectTree.Move(sourceIndex, Math.Clamp(insertIndex, 0, _projectTree.Count - 1));
        SaveUiState();
        StatusText.Text = "project order updated";
        e.Handled = true;
    }

    private ProjectDropPlacementKind? ProjectDropPlacement(System.Windows.DragEventArgs e, ProjectTreeItem? targetProject, bool isProjectHeaderTarget)
    {
        if (targetProject is null)
        {
            return null;
        }
        if (!isProjectHeaderTarget)
        {
            return ProjectDropPlacementKind.After;
        }

        var container = ProjectTree.ItemContainerGenerator.ContainerFromItem(targetProject) as TreeViewItem;
        if (container is null)
        {
            return ProjectDropPlacementKind.Before;
        }

        var position = e.GetPosition(container);
        return position.Y < container.ActualHeight / 2 ? ProjectDropPlacementKind.Before : ProjectDropPlacementKind.After;
    }

    private void ClearProjectDropIndicators()
    {
        foreach (var item in _projectTree)
        {
            item.ShowDropBefore = false;
            item.ShowDropAfter = false;
            foreach (var chat in item.Chats)
            {
                chat.ShowDropAfter = false;
            }
        }
    }

    private async Task RefreshMessagesAsync()
    {
        using var phase = EnterUiPhase("RefreshMessages");
        _messages.Clear();
        _hasMoreOlderMessages = false;
        _messageTotalCount = 0;
        _runProgress.Clear();
        HideRunProgress();
        await Dispatcher.Yield(DispatcherPriority.Background);
        ScrollMessagesToEnd();
        if (_selectedProject is not ProjectDto project || _selectedChat is not ChatDto chat)
        {
            return;
        }
        _isLoadingMessages = true;
        var startedAt = DateTimeOffset.Now;
        var heartbeat = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        heartbeat.Tick += (_, _) =>
        {
            var elapsed = DateTimeOffset.Now - startedAt;
            SetBusyMessage($"Loading history... | {elapsed:mm\\:ss} | {chat.Title}");
        };
        BeginActivity($"Loading history... | {chat.Title}");
        heartbeat.Start();
        try
        {
            var page = await _client.ListMessagePageAsync(project.Id, chat.Id, InitialMessagePageSize);
            if (_selectedProject?.Id != project.Id || _selectedChat?.Id != chat.Id)
            {
                return;
            }
            var loadedMessages = OrderMessages(page?.Messages ?? []).ToList();
            _hasMoreOlderMessages = page?.HasMoreBefore ?? false;
            _messageTotalCount = page?.TotalCount ?? loadedMessages.Count;
            SetChatUnloadedHistoryIndicator(chat.Id, _hasMoreOlderMessages);
            MarkSelectedConversationSeen();
            SetHistorySpacer(Math.Max(0, _messageTotalCount - loadedMessages.Count));
            await AddLoadedMessagesInBatchesAsync(loadedMessages, startedAt, chat.Title);
            UpdateHistorySpacer();
            ScrollMessagesToEnd();
            var elapsed = DateTimeOffset.Now - startedAt;
            StatusText.Text = $"history loaded | {LoadedHistoryMessageCount()}/{_messageTotalCount} message(s) | {elapsed.TotalSeconds:F1}s";
            UpdateCommandButtonState();
        }
        catch (Exception ex)
        {
            StatusText.Text = $"message load error | {ShortError(ex)}";
            UpdateCommandButtonState();
        }
        finally
        {
            heartbeat.Stop();
            _isLoadingMessages = false;
            EndActivity();
        }
    }

    private async Task AddLoadedMessagesInBatchesAsync(IReadOnlyList<MessageDto> messages, DateTimeOffset startedAt, string chatTitle)
    {
        using var phase = EnterUiPhase("AddLoadedMessagesInBatches");
        const int batchSize = 30;
        var insertIndex = _messages.Any(message => message.Id == HistorySpacerMessageId) ? 1 : 0;
        var added = 0;
        for (var index = messages.Count - 1; index >= 0; index--)
        {
            _messages.Insert(insertIndex, messages[index]);
            added++;
            if (added % batchSize != 0)
            {
                continue;
            }

            var elapsed = DateTimeOffset.Now - startedAt;
            SetBusyMessage($"Loading history... | {elapsed:mm\\:ss} | {added}/{messages.Count} | {chatTitle}");
            ScrollMessagesToEnd();
            await Dispatcher.Yield(DispatcherPriority.Background);
        }
    }

    private async void MessageRefreshTimer_Tick(object? sender, EventArgs e)
    {
        if (_isRestartingDaemon || _isPollingMessages || _isLoadingMessages || SelectedActiveRun() is not null)
        {
            return;
        }
        if (DateTimeOffset.Now < _nextBackgroundMessagePollAt)
        {
            return;
        }
        if (_selectedProject is not ProjectDto project || _selectedChat is not ChatDto chat)
        {
            return;
        }

        _isPollingMessages = true;
        try
        {
            var projectId = project.Id;
            var chatId = chat.Id;
            using var timeout = new CancellationTokenSource(BackgroundMessagePollTimeout);
            var page = await _client.ListMessagePageAsync(projectId, chatId, InitialMessagePageSize, cancellationToken: timeout.Token);
            if (_selectedProject?.Id != projectId || _selectedChat?.Id != chatId)
            {
                return;
            }
            _hasMoreOlderMessages = page?.HasMoreBefore ?? _hasMoreOlderMessages;
            _messageTotalCount = page?.TotalCount ?? _messageTotalCount;

            var shouldFollowNewMessages = IsMessagesScrolledNearEnd();
            var added = AppendNewMessages(page?.Messages ?? []);
            if (added > 0)
            {
                MarkChatUnreadIfConversationNotVisible(chatId);
                StatusText.Text = $"history updated | +{added} message(s)";
                if (shouldFollowNewMessages)
                {
                    ScrollMessagesToEnd();
                }
            }
            UpdateHistorySpacer();
            UpdateCommandButtonState();
        }
        catch (OperationCanceledException)
        {
            _nextBackgroundMessagePollAt = DateTimeOffset.Now.AddSeconds(30);
            WritePerformanceLog("history-refresh-timeout", $"chatId={LogText(_selectedChat?.Id ?? "")} backoffSeconds=30");
            UpdateCommandButtonState();
        }
        catch (Exception ex)
        {
            _nextBackgroundMessagePollAt = DateTimeOffset.Now.AddSeconds(15);
            WritePerformanceLog("history-refresh-error", $"type={LogText(ex.GetType().Name)} message={LogText(ex.Message)} backoffSeconds=15");
            UpdateCommandButtonState();
        }
        finally
        {
            _isPollingMessages = false;
        }
    }

    private int AppendNewMessages(IEnumerable<MessageDto> messages)
    {
        using var phase = EnterUiPhase("AppendNewMessages");
        RemoveComposerHint();
        var existingIds = _messages.Select(message => message.Id).ToHashSet(StringComparer.Ordinal);
        var existingLocalSignatures = _messages
            .Where(message => message.Id.StartsWith("local-", StringComparison.Ordinal))
            .Select(MessageSignature)
            .ToHashSet(StringComparer.Ordinal);
        var added = 0;
        foreach (var message in OrderMessages(messages))
        {
            if (existingIds.Contains(message.Id))
            {
                continue;
            }
            if (LocalMessageAlreadyRepresents(message, existingLocalSignatures))
            {
                continue;
            }
            InsertMessageInChronologicalOrder(message);
            existingIds.Add(message.Id);
            added++;
        }
        return added;
    }

    private bool LocalMessageAlreadyRepresents(MessageDto message, HashSet<string> existingLocalSignatures)
    {
        if (message.Id.StartsWith("local-", StringComparison.Ordinal))
        {
            return false;
        }
        if (existingLocalSignatures.Contains(MessageSignature(message)))
        {
            WritePerformanceLog("message-local-skip", MessageDebugText(message, "signature"));
            return true;
        }

        var localMessages = _messages
            .Where(item => item.Id.StartsWith("local-", StringComparison.Ordinal) &&
                           item.Role.Equals(message.Role, StringComparison.OrdinalIgnoreCase))
            .ToList();
        if (localMessages.Count == 0)
        {
            return false;
        }
        var incomingSignature = MessageContentSignature(message.Content);
        if (string.IsNullOrWhiteSpace(incomingSignature))
        {
            return false;
        }
        if (localMessages.Any(item => MessageContentSignature(item.Content) == incomingSignature))
        {
            WritePerformanceLog("message-local-skip", MessageDebugText(message, "content"));
            return true;
        }
        if (!string.IsNullOrWhiteSpace(message.RunId) &&
            localMessages.Any(item => item.RunId == message.RunId))
        {
            WritePerformanceLog("message-local-content-mismatch", MessageDebugText(message, "run"));
        }
        return false;
    }

    private static string MessageDebugText(MessageDto message, string reason)
    {
        return $"reason={LogText(reason)} id={LogText(message.Id)} role={LogText(message.Role)} kind={LogText(message.Kind)} runId={LogText(message.RunId)} chars={message.Content.Length}";
    }

    private int LoadedHistoryMessageCount() => _messages.Count(IsRealHistoryMessage);

    private MessageDto HistorySpacerMessage(double height) => new(
        HistorySpacerMessageId,
        _selectedChat?.Id ?? "",
        "spacer",
        "",
        null,
        DateTimeOffset.MinValue.ToString("O"),
        "spacer",
        SpacerHeight: height,
        SpacerIsLoading: _isLoadingOlderMessages);

    private void SetHistorySpacer(int unloadedMessageCount)
    {
        var height = _hasMoreOlderMessages && unloadedMessageCount > 0
            ? unloadedMessageCount * EstimatedMessageItemHeight
            : 0;
        var existingIndex = _messages.ToList().FindIndex(message => message.Id == HistorySpacerMessageId);
        if (height <= 0)
        {
            if (existingIndex >= 0)
            {
                _messages.RemoveAt(existingIndex);
            }
            return;
        }

        var spacer = HistorySpacerMessage(height);
        if (existingIndex >= 0)
        {
            _messages[existingIndex] = spacer;
        }
        else
        {
            _messages.Insert(0, spacer);
        }
    }

    private void UpdateHistorySpacer()
    {
        SetHistorySpacer(Math.Max(0, _messageTotalCount - LoadedHistoryMessageCount()));
    }

    private double HistorySpacerHeight()
    {
        return _messages.FirstOrDefault(message => message.Id == HistorySpacerMessageId)?.SpacerHeight ?? 0;
    }

    private static string MessageSignature(MessageDto message)
    {
        return $"{message.Role}\u001f{MessageContentSignature(message.Content)}";
    }

    private static bool IsRealHistoryMessage(MessageDto message)
    {
        return !message.Id.StartsWith("local-", StringComparison.Ordinal)
            && !message.Role.Equals("spacer", StringComparison.OrdinalIgnoreCase);
    }

    private static string MessageContentSignature(string content)
    {
        var text = content.Split(["\n\nAttachments:"], 2, StringSplitOptions.None)[0];
        return string.Join(" ", text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
    }

    private static IEnumerable<MessageDto> OrderMessages(IEnumerable<MessageDto> messages)
    {
        return messages.OrderBy(MessageCreatedAt).ThenBy(message => message.Id, StringComparer.Ordinal);
    }

    private static DateTimeOffset MessageCreatedAt(MessageDto message)
    {
        return DateTimeOffset.TryParse(
            message.CreatedAt,
            CultureInfo.InvariantCulture,
            DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
            out var timestamp)
            ? timestamp
            : DateTimeOffset.MinValue;
    }

    private void ScrollMessagesToEnd()
    {
        if (_messages.Count == 0)
        {
            return;
        }
        Dispatcher.BeginInvoke(() =>
        {
            if (FindVisualChild<ScrollViewer>(MessagesList, _ => true) is ScrollViewer scrollViewer)
            {
                scrollViewer.ScrollToEnd();
                return;
            }
            if (_messages.Count > 0)
            {
                MessagesList.ScrollIntoView(_messages[^1]);
            }
        }, System.Windows.Threading.DispatcherPriority.Background);
    }

    private void ScrollMessagesToEndThrottled()
    {
        var now = DateTimeOffset.Now;
        if (now - _lastMessageAutoScrollAt < AutoScrollInterval)
        {
            return;
        }

        _lastMessageAutoScrollAt = now;
        ScrollMessagesToEnd();
    }

    private void MainTabs_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!ReferenceEquals(e.OriginalSource, MainTabs))
        {
            return;
        }

        if (e.RemovedItems.Contains(ConversationTab))
        {
            SaveMessagesScrollOffset();
        }
        if (ReferenceEquals(MainTabs.SelectedItem, ConversationTab))
        {
            MarkSelectedConversationSeen();
            RestoreMessagesScrollOffset();
        }
        SaveUiState();
    }

    private void SaveMessagesScrollOffset()
    {
        if (FindVisualChild<ScrollViewer>(MessagesList, _ => true) is ScrollViewer scrollViewer)
        {
            _savedMessageVerticalOffset = scrollViewer.VerticalOffset;
        }
    }

    private void RestoreMessagesScrollOffset()
    {
        _pendingMessageScrollOffsetRestore = true;
        var targetOffset = _savedMessageVerticalOffset;
        Dispatcher.BeginInvoke(() =>
        {
            Dispatcher.BeginInvoke(() =>
            {
                if (FindVisualChild<ScrollViewer>(MessagesList, _ => true) is not ScrollViewer scrollViewer)
                {
                    _pendingMessageScrollOffsetRestore = false;
                    return;
                }
                _isRestoringMessageScrollOffset = true;
                try
                {
                    scrollViewer.ScrollToVerticalOffset(Math.Max(0, Math.Min(targetOffset, scrollViewer.ScrollableHeight)));
                }
                finally
                {
                    _isRestoringMessageScrollOffset = false;
                    _pendingMessageScrollOffsetRestore = false;
                }
            }, System.Windows.Threading.DispatcherPriority.ContextIdle);
        }, System.Windows.Threading.DispatcherPriority.Loaded);
    }

    private bool IsMessagesScrolledNearEnd()
    {
        if (FindVisualChild<ScrollViewer>(MessagesList, _ => true) is not ScrollViewer scrollViewer)
        {
            return false;
        }
        if (scrollViewer.ExtentHeight <= 0)
        {
            return false;
        }
        return scrollViewer.VerticalOffset + scrollViewer.ViewportHeight >= scrollViewer.ExtentHeight - 1;
    }

    private async void MessagesList_PreviewKeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        if (e.Key == Key.Home)
        {
            e.Handled = true;
            if (_messages.Count > 0)
            {
                MessagesList.ScrollIntoView(_messages[0]);
            }
            if (FindVisualChild<ScrollViewer>(MessagesList, _ => true) is ScrollViewer scrollViewer)
            {
                await LoadOlderMessagesIfNeededAsync(scrollViewer);
            }
        }
        else if (e.Key == Key.End)
        {
            e.Handled = true;
            ScrollMessagesToEnd();
        }
        else if (e.Key == Key.PageUp)
        {
            e.Handled = true;
            ScrollMessagesByPage(-1);
            if (FindVisualChild<ScrollViewer>(MessagesList, _ => true) is ScrollViewer scrollViewer)
            {
                await LoadOlderMessagesIfNeededAsync(scrollViewer);
            }
        }
        else if (e.Key == Key.PageDown)
        {
            e.Handled = true;
            ScrollMessagesByPage(1);
        }
    }

    private async void MessagesList_PreviewMouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (FindVisualChild<ScrollViewer>(MessagesList, _ => true) is not ScrollViewer scrollViewer)
        {
            return;
        }

        scrollViewer.ScrollToVerticalOffset(scrollViewer.VerticalOffset - e.Delta);
        e.Handled = true;
        await LoadOlderMessagesIfNeededAsync(scrollViewer);
    }

    private async void MessagesList_ScrollChanged(object sender, ScrollChangedEventArgs e)
    {
        if (FindVisualChild<ScrollViewer>(MessagesList, _ => true) is ScrollViewer scrollViewer)
        {
            if (!_isRestoringMessageScrollOffset && !_pendingMessageScrollOffsetRestore && ConversationTab.IsSelected)
            {
                _savedMessageVerticalOffset = scrollViewer.VerticalOffset;
            }
            if (_isRestoringMessageScrollOffset || _pendingMessageScrollOffsetRestore)
            {
                return;
            }
            await LoadOlderMessagesIfNeededAsync(scrollViewer);
        }
    }

    private async Task LoadOlderMessagesIfNeededAsync(ScrollViewer scrollViewer)
    {
        if (!_hasMoreOlderMessages || _isLoadingOlderMessages || _isLoadingMessages)
        {
            return;
        }
        if (!IsUnloadedHistoryNearViewport(scrollViewer))
        {
            return;
        }
        if (_selectedProject is not ProjectDto project || _selectedChat is not ChatDto chat)
        {
            return;
        }
        var oldest = _messages.FirstOrDefault(IsRealHistoryMessage);
        if (oldest is null)
        {
            return;
        }

        _isLoadingOlderMessages = true;
        UpdateHistorySpacer();
        var oldExtent = scrollViewer.ExtentHeight;
        var oldOffset = scrollViewer.VerticalOffset;
        try
        {
            StatusText.Text = $"older history loading | {LoadedHistoryMessageCount()}/{_messageTotalCount}";
            var page = await _client.ListMessagePageAsync(project.Id, chat.Id, OlderMessagePageSize, oldest.CreatedAt, oldest.Id);
            if (_selectedProject?.Id != project.Id || _selectedChat?.Id != chat.Id)
            {
                return;
            }
            _hasMoreOlderMessages = page?.HasMoreBefore ?? false;
            _messageTotalCount = page?.TotalCount ?? _messageTotalCount;
            if (!_hasMoreOlderMessages)
            {
                SetChatUnloadedHistoryIndicator(chat.Id, false);
            }
            var existingIds = _messages.Select(message => message.Id).ToHashSet(StringComparer.Ordinal);
            var olderMessages = OrderMessages(page?.Messages ?? [])
                .Where(message => !existingIds.Contains(message.Id))
                .ToList();
            var insertIndex = _messages.Any(message => message.Id == HistorySpacerMessageId) ? 1 : 0;
            for (var index = olderMessages.Count - 1; index >= 0; index--)
            {
                _messages.Insert(insertIndex, olderMessages[index]);
            }
            UpdateHistorySpacer();
            await Dispatcher.Yield(DispatcherPriority.Background);
            var delta = scrollViewer.ExtentHeight - oldExtent;
            scrollViewer.ScrollToVerticalOffset(Math.Max(0, oldOffset + delta));
            StatusText.Text = $"history loaded | {LoadedHistoryMessageCount()}/{_messageTotalCount} message(s)";
        }
        catch (Exception ex)
        {
            StatusText.Text = $"older history error | {ShortError(ex)}";
        }
        finally
        {
            _isLoadingOlderMessages = false;
            UpdateHistorySpacer();
        }
    }

    private bool IsUnloadedHistoryNearViewport(ScrollViewer scrollViewer)
    {
        var spacerHeight = HistorySpacerHeight();
        if (spacerHeight <= 0)
        {
            return false;
        }

        return scrollViewer.VerticalOffset <= spacerHeight + Math.Max(160, scrollViewer.ViewportHeight * 0.5);
    }

    private void ScrollMessagesByPage(int direction)
    {
        if (FindVisualChild<ScrollViewer>(MessagesList, _ => true) is not ScrollViewer scrollViewer)
        {
            return;
        }

        var page = scrollViewer.ViewportHeight > 0 ? scrollViewer.ViewportHeight : 10;
        scrollViewer.ScrollToVerticalOffset(scrollViewer.VerticalOffset + direction * page);
    }

    private void MessagesList_PreviewMouseDown(object sender, MouseButtonEventArgs e)
    {
        MessagesList.Focus();
    }

    private async Task RefreshFilesAsync(string path)
    {
        _files.Clear();
        _currentDirectoryPath = path;
        _currentFilePath = "";
        FilePathText.Text = path.Length == 0 ? "/" : path;
        ClearFilePreview();
        UpdateCommandButtonState();
        if (_selectedProject is not ProjectDto project)
        {
            return;
        }
        BeginActivity(path.Length == 0 ? "ファイルを読み込み中..." : $"ファイルを読み込み中... | {path}");
        try
        {
            foreach (var entry in (await _client.ListFilesAsync(project.Id, path))?.Entries ?? [])
            {
                _files.Add(new FileTreeItem(entry));
            }
        }
        catch (Exception ex)
        {
            StatusText.Text = $"file list error | {ShortError(ex)}";
        }
        finally
        {
            EndActivity();
        }
    }

    private async Task RefreshDiagnosticsAsync()
    {
        await RunActivityAsync("診断情報を読み込み中...", async () =>
        {
            var settings = await _client.GetSettingsAsync();
            if (settings is not null)
            {
                ApplyRuntimeSettingsSelection(settings);
            }
            var text = await _client.GetDiagnosticsJsonAsync();
            using var document = JsonDocument.Parse(text);
            UpdateDiagnosticsSummary(document.RootElement);
            DiagnosticsBox.Text = JsonSerializer.Serialize(JsonSerializer.Deserialize<object>(text), _json);
        });
    }

    private async Task RefreshAutomationsAsync()
    {
        _isRefreshingAutomations = true;
        UpdateAutomationButtonState();
        _automations.Clear();
        if (_selectedProject is not ProjectDto project || _selectedChat is not ChatDto chat)
        {
            _isRefreshingAutomations = false;
            UpdateAutomationButtonState();
            return;
        }
        try
        {
            var items = await _client.ListAutomationsAsync(project.Id, chat.Id) ?? [];
            foreach (var item in items.OrderBy(item => item.CreatedAt, StringComparer.Ordinal))
            {
                _automations.Add(item);
            }
        }
        catch (Exception ex)
        {
            StatusText.Text = $"automation load error | {ShortError(ex)}";
        }
        finally
        {
            _isRefreshingAutomations = false;
            UpdateAutomationButtonState();
        }
    }

    private async void RefreshAutomations_Click(object sender, RoutedEventArgs e)
    {
        await RefreshAutomationsAsync();
    }

    private void AutomationInput_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (_isLoadingAutomationSelection)
        {
            return;
        }
        UpdateAutomationButtonState();
    }

    private void AutomationEnabledBox_Changed(object sender, RoutedEventArgs e)
    {
        UpdateAutomationButtonState();
    }

    private void AutomationsGrid_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (AutomationsGrid.SelectedItem is AutomationDto automation)
        {
            _isLoadingAutomationSelection = true;
            try
            {
                AutomationNameBox.Text = automation.Name;
                AutomationIntervalBox.Text = automation.IntervalMinutes.ToString(CultureInfo.InvariantCulture);
                AutomationEnabledBox.IsChecked = automation.Enabled;
                AutomationPromptBox.Text = automation.Prompt;
            }
            finally
            {
                _isLoadingAutomationSelection = false;
            }
        }
        UpdateAutomationButtonState();
    }

    private void UpdateAutomationButtonState()
    {
        if (CreateAutomationButton is null)
        {
            return;
        }

        var hasChat = _selectedProject is not null && _selectedChat is not null;
        var canContinue = _selectedChat?.CanContinue == true;
        var hasValidInterval = int.TryParse(AutomationIntervalBox.Text.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var interval)
            && interval >= 1;
        var hasInput = !string.IsNullOrWhiteSpace(AutomationNameBox.Text)
            && !string.IsNullOrWhiteSpace(AutomationPromptBox.Text)
            && hasValidInterval;
        var selectedAutomation = AutomationsGrid.SelectedItem as AutomationDto;
        var hasSelection = selectedAutomation is not null;
        var isDraft = selectedAutomation?.IsDraft == true;
        var hasChanges = selectedAutomation is not null && AutomationEditorHasChanges(selectedAutomation, interval);
        var busy = _isRefreshingAutomations || _isSavingAutomation || _isRunningAutomationNow;

        RefreshAutomationsButton.IsEnabled = hasChat && !busy;
        CreateAutomationButton.IsEnabled = hasChat && canContinue && !busy;
        SaveAutomationButton.IsEnabled = hasChat && canContinue && hasSelection && hasInput && hasChanges && !busy;
        RunAutomationNowButton.IsEnabled = hasChat && canContinue && !isDraft && selectedAutomation?.Enabled == true && selectedAutomation.Running == false && !busy;
        ToggleAutomationButton.IsEnabled = hasChat && hasSelection && !isDraft && selectedAutomation?.Running == false && !busy;
        DeleteAutomationButton.IsEnabled = hasChat && hasSelection && selectedAutomation?.Running == false && !busy;

        CreateAutomationButton.ToolTip = !hasChat
            ? "チャットを選択してください"
            : !canContinue
                ? "このチャットでは継続できないため作成できません"
                : null;
        SaveAutomationButton.ToolTip = !hasSelection
            ? "オートメーションを選択してください"
            : !canContinue
                ? "このチャットでは継続できないため保存できません"
                : !hasInput
                    ? "名前、実行間隔（分）、指示を入力してください"
                    : !hasChanges
                        ? "変更がありません"
                        : null;
        RunAutomationNowButton.ToolTip = !hasSelection
            ? "オートメーションを選択してください"
            : isDraft
                ? "保存前のオートメーションは実行できません"
            : selectedAutomation?.Enabled != true
                ? "無効なオートメーションは実行できません"
                : selectedAutomation.Running
                    ? "実行中です"
                    : null;
    }

    private void NewAutomation_Click(object sender, RoutedEventArgs e)
    {
        if (_selectedProject is null || _selectedChat is not ChatDto chat || !chat.CanContinue)
        {
            return;
        }
        var now = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture);
        var draft = new AutomationDto(
            $"local-automation-draft-{++_automationDraftCounter}",
            _selectedProject.Id,
            chat.Id,
            "",
            "",
            "interval_minutes",
            60,
            true,
            false,
            null,
            null,
            null,
            now,
            now);
        _automations.Add(draft);
        AutomationsGrid.SelectedItem = draft;
        AutomationsGrid.ScrollIntoView(draft);
        StatusText.Text = "automation draft created";
    }

    private async Task CreateAutomationFromEditorAsync(ProjectDto project, ChatDto chat, AutomationDto? draft)
    {
        if (!TryReadAutomationEditor(out var name, out var prompt, out var interval))
        {
            return;
        }
        try
        {
            var created = await _client.CreateAutomationAsync(project.Id, chat.Id, name, prompt, interval, AutomationEnabledBox.IsChecked == true);
            if (created is not null)
            {
                if (draft is not null)
                {
                    _automations.Remove(draft);
                }
                _automations.Add(created);
                AutomationsGrid.SelectedItem = created;
            }
            StatusText.Text = "automation created";
            UpdateAutomationButtonState();
        }
        catch (Exception ex)
        {
            StatusText.Text = $"automation create error | {ShortError(ex)}";
            UpdateAutomationButtonState();
        }
    }

    private async void ToggleAutomation_Click(object sender, RoutedEventArgs e)
    {
        if (_selectedProject is not ProjectDto project || _selectedChat is not ChatDto chat || AutomationsGrid.SelectedItem is not AutomationDto automation)
        {
            return;
        }
        try
        {
            var updated = await _client.UpdateAutomationAsync(project.Id, chat.Id, automation.Id, !automation.Enabled);
            if (updated is not null)
            {
                ReplaceAutomation(updated);
            }
            StatusText.Text = updated?.Enabled == true ? "automation enabled" : "automation disabled";
            UpdateAutomationButtonState();
        }
        catch (Exception ex)
        {
            StatusText.Text = $"automation update error | {ShortError(ex)}";
            UpdateAutomationButtonState();
        }
    }

    private async void SaveAutomation_Click(object sender, RoutedEventArgs e)
    {
        if (_selectedProject is not ProjectDto project || _selectedChat is not ChatDto chat || AutomationsGrid.SelectedItem is not AutomationDto automation)
        {
            return;
        }
        if (!TryReadAutomationEditor(out var name, out var prompt, out var interval))
        {
            return;
        }
        _isSavingAutomation = true;
        UpdateAutomationButtonState();
        try
        {
            if (automation.IsDraft)
            {
                await CreateAutomationFromEditorAsync(project, chat, automation);
                return;
            }
            var updated = await _client.UpdateAutomationAsync(project.Id, chat.Id, automation.Id, name, prompt, interval, AutomationEnabledBox.IsChecked == true);
            if (updated is not null)
            {
                ReplaceAutomation(updated);
            }
            StatusText.Text = "automation saved";
        }
        catch (Exception ex)
        {
            StatusText.Text = $"automation save error | {ShortError(ex)}";
        }
        finally
        {
            _isSavingAutomation = false;
            UpdateAutomationButtonState();
        }
    }

    private async void RunAutomationNow_Click(object sender, RoutedEventArgs e)
    {
        if (_selectedProject is not ProjectDto project || _selectedChat is not ChatDto chat || AutomationsGrid.SelectedItem is not AutomationDto automation)
        {
            return;
        }
        var runCts = new CancellationTokenSource();
        var runStarted = false;
        var localMessageId = $"local-automation-user-{Guid.NewGuid():N}";
        var assistantMessageId = $"local-assistant-automation-{Guid.NewGuid():N}";
        var sendStartedAt = DateTimeOffset.Now;
        _isRunningAutomationNow = true;
        UpdateAutomationButtonState();
        BeginRunActivity(chat.Id, "オートメーションを開始中...");
        ShowPendingRunProgressForChat(chat.Id, "オートメーション開始待ち");
        var localPromptMessage = new MessageDto(
            localMessageId,
            chat.Id,
            "user",
            automation.Prompt,
            null,
            DateTimeOffset.UtcNow.ToString("O"),
            "instruction");
        var localWaitingMessage = new MessageDto(
            assistantMessageId,
            chat.Id,
            "assistant",
            StartingResponseText,
            null,
            DateTimeOffset.UtcNow.ToString("O"),
            "waiting");
        if (_selectedChat?.Id == chat.Id)
        {
            AppendMessage(localPromptMessage, scrollToEnd: true);
            AppendMessage(localWaitingMessage, scrollToEnd: true);
            _savedMessageVerticalOffset = double.MaxValue;
        }
        try
        {
            var result = await _client.RunAutomationNowAsync(project.Id, chat.Id, automation.Id, runCts.Token);
            if (result?.Automation is not null)
            {
                ReplaceAutomation(result.Automation);
            }
            if (result?.Run is null)
            {
                RemoveMessageById(chat.Id, localMessageId);
                RemoveMessageById(chat.Id, assistantMessageId);
                StatusText.Text = string.IsNullOrWhiteSpace(result?.Automation?.LastError)
                    ? "automation not started"
                    : $"automation not started | {result.Automation.LastError}";
                return;
            }
            ReplaceMessageById(chat.Id, localMessageId, localPromptMessage with { Id = result.Run.MessageId, RunId = result.Run.RunId });
            ReplaceMessageById(chat.Id, assistantMessageId, localWaitingMessage with { RunId = result.Run.RunId });
            _savedMessageVerticalOffset = double.MaxValue;
            _activeRunsByChat[chat.Id] = new ActiveUiRun(result.Run.RunId, project.Id, chat.Id, runCts);
            runStarted = true;
            UpdateCommandButtonState();
            StatusText.Text = "automation started";
            await StreamRunAsync(result.Run.RunId, chat.Id, assistantMessageId, sendStartedAt, runCts.Token);
        }
        catch (Exception ex)
        {
            RemoveMessageById(chat.Id, localMessageId);
            RemoveMessageById(chat.Id, assistantMessageId);
            StatusText.Text = $"automation run error | {ShortError(ex)}";
        }
        finally
        {
            _isRunningAutomationNow = false;
            if (!runStarted)
            {
                runCts.Dispose();
                HideRunProgressForChat(chat.Id);
            }
            EndRunActivity(chat.Id);
            UpdateAutomationButtonState();
            UpdateCommandButtonState();
        }
    }

    private async void DeleteAutomation_Click(object sender, RoutedEventArgs e)
    {
        if (_selectedProject is not ProjectDto project || _selectedChat is not ChatDto chat || AutomationsGrid.SelectedItem is not AutomationDto automation)
        {
            return;
        }
        if (automation.IsDraft)
        {
            _automations.Remove(automation);
            StatusText.Text = "automation draft deleted";
            UpdateAutomationButtonState();
            return;
        }
        if (System.Windows.MessageBox.Show(this, $"「{automation.Name}」を削除しますか？", "オートメーション削除", MessageBoxButton.OKCancel, MessageBoxImage.Warning) != MessageBoxResult.OK)
        {
            return;
        }
        try
        {
            await _client.DeleteAutomationAsync(project.Id, chat.Id, automation.Id);
            _automations.Remove(automation);
            StatusText.Text = "automation deleted";
            UpdateAutomationButtonState();
        }
        catch (Exception ex)
        {
            StatusText.Text = $"automation delete error | {ShortError(ex)}";
            UpdateAutomationButtonState();
        }
    }

    private void ReplaceAutomation(AutomationDto updated)
    {
        for (var index = 0; index < _automations.Count; index++)
        {
            if (_automations[index].Id != updated.Id)
            {
                continue;
            }
            _automations[index] = updated;
            AutomationsGrid.SelectedItem = updated;
            return;
        }
        _automations.Add(updated);
        AutomationsGrid.SelectedItem = updated;
        UpdateAutomationButtonState();
    }

    private bool TryReadAutomationEditor(out string name, out string prompt, out int interval)
    {
        name = CleanAutomationName(AutomationNameBox.Text);
        prompt = CleanAutomationPrompt(AutomationPromptBox.Text);
        interval = 0;
        if (string.IsNullOrWhiteSpace(name) || string.IsNullOrWhiteSpace(prompt))
        {
            StatusText.Text = "automation error | 名前と指示を入力してください";
            return false;
        }
        if (!int.TryParse(AutomationIntervalBox.Text.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out interval) || interval < 1)
        {
            StatusText.Text = "automation error | 間隔は1以上の分数で入力してください";
            return false;
        }
        return true;
    }

    private bool AutomationEditorHasChanges(AutomationDto automation, int parsedInterval)
    {
        if (automation.IsDraft)
        {
            return true;
        }
        return CleanAutomationName(AutomationNameBox.Text) != automation.Name
            || CleanAutomationPrompt(AutomationPromptBox.Text) != automation.Prompt
            || parsedInterval != automation.IntervalMinutes
            || (AutomationEnabledBox.IsChecked == true) != automation.Enabled;
    }

    private static string CleanAutomationName(string value)
    {
        return Regex.Replace(value.Trim(), "\\s+", " ");
    }

    private static string CleanAutomationPrompt(string value)
    {
        return value.Replace("\r\n", "\n").Replace("\r", "\n").Trim();
    }

    private void UpdateDiagnosticsSummary(JsonElement diagnostics)
    {
        DiagnosticsAppServerText.Text = TryGetBoolean(diagnostics, "appServerRunning") switch
        {
            true => "running",
            false => "stopped",
            null => "-"
        };
        DiagnosticsPermissionText.Text = TryGetString(diagnostics, "permissionProfile") ?? "-";
        DiagnosticsApprovalText.Text = TryGetString(diagnostics, "approvalPolicy") ?? "-";
        DiagnosticsModelText.Text = TryGetString(diagnostics, "model") is { Length: > 0 } model ? model : "既定";
        DiagnosticsCodexText.Text = TryGetString(diagnostics, "codexVersion")
            ?? TryGetString(diagnostics, "codexPath")
            ?? "-";
        if (TryGetBoolean(diagnostics, "codexHomeExists") == false)
        {
            var codexHome = TryGetString(diagnostics, "codexHome") ?? "$CODEX_HOME";
            StatusText.Text = $"CODEX_HOMEが見つかりません | {codexHome}";
        }
        if (diagnostics.TryGetProperty("activeRunIds", out var activeRuns) && activeRuns.ValueKind == JsonValueKind.Array)
        {
            DiagnosticsActiveRunText.Text = activeRuns.GetArrayLength().ToString(CultureInfo.InvariantCulture);
        }
        else
        {
            DiagnosticsActiveRunText.Text = "-";
        }
    }

    private static string? TryGetString(JsonElement element, string propertyName)
    {
        return element.TryGetProperty(propertyName, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : null;
    }

    private static bool? TryGetBoolean(JsonElement element, string propertyName)
    {
        return element.TryGetProperty(propertyName, out var value) && value.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? value.GetBoolean()
            : null;
    }

    private async void ApplyRuntimeSettings_Click(object sender, RoutedEventArgs e)
    {
        var profile = SelectedPermissionProfile();
        var approvalPolicy = SelectedApprovalPolicy();
        if (profile is null || approvalPolicy is null)
        {
            return;
        }
        var model = SelectedModel();
        await RunActivityAsync("実行設定を更新中...", async () =>
        {
            var settings = await _client.UpdateSettingsAsync(profile, approvalPolicy, model);
            if (settings is not null)
            {
                ApplyRuntimeSettingsSelection(settings);
                StatusText.Text = $"実行設定 | {DisplayRuntimeSettings(settings)}";
            }
            await RefreshDiagnosticsAsync();
        });
    }

    private string? SelectedPermissionProfile()
    {
        return (PermissionProfileBox.SelectedItem as ComboBoxItem)?.Tag as string;
    }

    private string? SelectedApprovalPolicy()
    {
        return (ApprovalPolicyBox.SelectedItem as ComboBoxItem)?.Tag as string;
    }

    private string SelectedModel()
    {
        var text = (ModelBox.Text ?? "").Trim();
        if (text.Length == 0 || text == "既定")
        {
            return "";
        }
        foreach (var item in ModelBox.Items.OfType<ComboBoxItem>())
        {
            if ((item.Tag as string) == text || string.Equals(item.Content as string, text, StringComparison.Ordinal))
            {
                return (item.Tag as string) ?? "";
            }
        }
        return text;
    }

    private void ApplyRuntimeSettingsSelection(AppSettingsDto settings)
    {
        SelectPermissionProfile(settings.PermissionProfile);
        SelectApprovalPolicy(settings.ApprovalPolicy);
        SelectModel(settings.Model);
    }

    private void SelectPermissionProfile(string profile)
    {
        foreach (var item in PermissionProfileBox.Items.OfType<ComboBoxItem>())
        {
            if ((item.Tag as string) == profile)
            {
                PermissionProfileBox.SelectedItem = item;
                return;
            }
        }
    }

    private void SelectApprovalPolicy(string approvalPolicy)
    {
        foreach (var item in ApprovalPolicyBox.Items.OfType<ComboBoxItem>())
        {
            if ((item.Tag as string) == approvalPolicy)
            {
                ApprovalPolicyBox.SelectedItem = item;
                return;
            }
        }
    }

    private void SelectModel(string model)
    {
        foreach (var item in ModelBox.Items.OfType<ComboBoxItem>())
        {
            if ((item.Tag as string) == model)
            {
                ModelBox.SelectedItem = item;
                return;
            }
        }
        ModelBox.SelectedItem = null;
        ModelBox.Text = model;
    }

    private static string DisplayRuntimeSettings(AppSettingsDto settings)
    {
        var model = string.IsNullOrWhiteSpace(settings.Model) ? "既定" : settings.Model;
        return $"{model}, {settings.ApprovalPolicy}, {settings.PermissionProfile}";
    }

    private async void ChatFilterBox_TextChanged(object sender, TextChangedEventArgs e) => await RefreshProjectsAsync();

    private async void Refresh_Click(object sender, RoutedEventArgs e)
    {
        await RunBusyAsync("更新中...", async () =>
        {
            await RefreshProjectsAsync();
            await RefreshDiagnosticsAsync();
            StatusText.Text = "待機中 | 更新済み";
        });
    }

    private async void AddProject_Click(object sender, RoutedEventArgs e)
    {
        await RegisterFolderProjectAsync("新規作成");
    }

    private async void RegisterDirectoryProject_Click(object sender, RoutedEventArgs e)
    {
        await RegisterFolderProjectAsync("ディレクトリ登録");
    }

    private async Task RegisterFolderProjectAsync(string title)
    {
        var path = SelectProjectDirectory(title);
        if (!string.IsNullOrWhiteSpace(path))
        {
            var normalizedPath = path.Trim();
            var pendingProject = new ProjectDto(
                $"pending_{Guid.NewGuid():N}",
                $"登録中: {ProjectNameFromPath(normalizedPath)}",
                normalizedPath,
                "",
                "");
            var pendingItem = AddPendingProjectToTree(pendingProject);
            _selectedProject = pendingProject;
            _selectedChat = null;
            _messages.Clear();
            UpdateCommandButtonState();
            UpdateRightPaneVisibility();
            await FocusProjectItemInTreeAsync(pendingItem);
            await RunActivityAsync("ディレクトリを登録中...", async () =>
            {
                try
                {
                    var project = await _client.CreateProjectAsync(normalizedPath, null);
                    if (project is not null)
                    {
                        var projectItem = ReplacePendingProject(pendingItem, project);
                        await LoadProjectChatsAsync(projectItem);
                        await SelectProjectInTreeAsync(project.Id);
                        StatusText.Text = $"project registered | {project.Name}";
                    }
                    else
                    {
                        await RemoveProjectFromTreeAsync(pendingProject.Id);
                    }
                }
                catch
                {
                    await RemoveProjectFromTreeAsync(pendingProject.Id);
                    throw;
                }
            });
        }
    }

    private string? SelectProjectDirectory(string title)
    {
        using var dialog = new System.Windows.Forms.FolderBrowserDialog
        {
            Description = title,
            UseDescriptionForTitle = true,
            ShowNewFolderButton = true,
            InitialDirectory = WslPathToUncPath("$HOME")
        };
        return dialog.ShowDialog() == System.Windows.Forms.DialogResult.OK
            ? WindowsPathToWslPath(dialog.SelectedPath)
            : null;
    }

    private static string WindowsPathToWslPath(string path)
    {
        var normalized = path.Replace('\\', '/');
        const string wslLocalhostPrefix = "//wsl.localhost/";
        const string wslLegacyPrefix = "//wsl$/";
        if (normalized.StartsWith(wslLocalhostPrefix, StringComparison.OrdinalIgnoreCase))
        {
            return WslUncPathToWslPath(normalized[wslLocalhostPrefix.Length..]);
        }
        if (normalized.StartsWith(wslLegacyPrefix, StringComparison.OrdinalIgnoreCase))
        {
            return WslUncPathToWslPath(normalized[wslLegacyPrefix.Length..]);
        }
        if (normalized.Length >= 3 && normalized[1] == ':' && normalized[2] == '/')
        {
            var drive = char.ToLowerInvariant(normalized[0]);
            return $"/mnt/{drive}/{normalized[3..]}";
        }
        return normalized;
    }

    private static string WslUncPathToWslPath(string distroAndPath)
    {
        var slashIndex = distroAndPath.IndexOf('/');
        if (slashIndex < 0 || slashIndex == distroAndPath.Length - 1)
        {
            return "/";
        }
        return "/" + distroAndPath[(slashIndex + 1)..];
    }

    private string WslPathToUncPath(string path)
    {
        if (path == "$HOME")
        {
            path = _wslHomePath;
        }
        return $"\\\\wsl.localhost\\{_wslDistroName}{path.Replace('/', '\\')}";
    }

    private async void RegisterExistingProject_Click(object sender, RoutedEventArgs e)
    {
        List<ProjectCandidateDto> candidates = [];
        try
        {
            await RunBusyAsync("Codex履歴を読み込み中...", async () =>
            {
                candidates = await _client.ListProjectCandidatesAsync() ?? [];
                StatusText.Text = candidates.Count == 0
                    ? "scan complete | no candidates"
                    : $"scan complete | {candidates.Count} candidate(s)";
            });
        }
        catch (Exception ex)
        {
            StatusText.Text = $"candidate scan error | {ex.Message}";
            return;
        }

        if (candidates.Count > 0)
        {
            var preview = string.Join(Environment.NewLine, candidates.Take(20).Select(candidate => $"{candidate.Name} | {candidate.Path} | {candidate.ThreadCount} threads"));
            if (candidates.Count > 20)
            {
                preview += $"{Environment.NewLine}...";
            }
            if (System.Windows.MessageBox.Show(this, preview, "Import Codex Projects", MessageBoxButton.OKCancel, MessageBoxImage.Question) == MessageBoxResult.OK)
            {
                try
                {
                    await RunBusyAsync($"Importing {candidates.Count} project candidate(s)...", async () =>
                    {
                        var imported = await _client.ImportProjectCandidatesAsync(candidates.Select(candidate => candidate.Path)) ?? [];
                        StatusText.Text = $"import complete | {imported.Count} project(s)";
                        await RefreshProjectsAsync();
                    });
                }
                catch (Exception ex)
                {
                    StatusText.Text = $"import error | {ex.Message}";
                }
            }
            return;
        }
    }

    private void NewChat_Click(object sender, RoutedEventArgs e)
    {
        if (_selectedProject is not ProjectDto project)
        {
            return;
        }
        _selectedProject = project;
        _selectedChat = null;
        _messages.Clear();
        UpdateCommandButtonState();
        UpdateRightPaneVisibility();
        StatusText.Text = $"project | {project.Name} | メッセージ送信で新規チャットを作成";
        NewChatMessageBox.Focus();
    }

    private async void RenameChat_Click(object sender, RoutedEventArgs e)
    {
        if (_selectedChat is not ChatDto chat)
        {
            return;
        }
        await BeginEditChatAsync(chat.Id);
    }

    private async Task BeginEditChatAsync(string chatId)
    {
        var item = FindChatItem(chatId);
        if (item is null)
        {
            return;
        }

        foreach (var chatItem in _projectTree.SelectMany(project => project.Chats))
        {
            chatItem.IsEditing = false;
        }

        item.ResetEditTitle();
        item.IsEditing = true;
        await Dispatcher.InvokeAsync(() =>
        {
            ProjectTree.UpdateLayout();
            if (FindVisualChild<TextBox>(ProjectTree, textBox => ReferenceEquals(textBox.DataContext, item)) is TextBox editor)
            {
                editor.Focus();
                editor.SelectAll();
            }
        }, DispatcherPriority.Loaded);
    }

    private ChatTreeItem? FindChatItem(string chatId)
    {
        return _projectTree.SelectMany(project => project.Chats).FirstOrDefault(item => item.Chat.Id == chatId);
    }

    private void SetChatUnloadedHistoryIndicator(string chatId, bool hasUnloadedHistory)
    {
        if (hasUnloadedHistory)
        {
            _chatsWithUnloadedHistory.Add(chatId);
        }
        else
        {
            _chatsWithUnloadedHistory.Remove(chatId);
        }

        if (FindChatItem(chatId) is { } item)
        {
            item.HasUnloadedHistory = hasUnloadedHistory;
        }
    }

    private bool IsChatRunning(string chatId)
    {
        return _runActivityDepthByChat.ContainsKey(chatId) || ActiveRunForChat(chatId) is not null;
    }

    private void UpdateChatRunningIndicator(string chatId)
    {
        if (FindChatItem(chatId) is { } item)
        {
            item.IsRunning = IsChatRunning(chatId);
        }
    }

    private void MarkChatUnreadIfConversationNotVisible(string chatId)
    {
        if (_selectedChat?.Id == chatId && ReferenceEquals(MainTabs.SelectedItem, ConversationTab))
        {
            return;
        }
        SetChatUnloadedHistoryIndicator(chatId, true);
    }

    private void MarkSelectedConversationSeen()
    {
        if (_selectedChat is ChatDto chat && ReferenceEquals(MainTabs.SelectedItem, ConversationTab))
        {
            SetChatUnloadedHistoryIndicator(chat.Id, false);
        }
    }

    private async Task CommitChatTitleEditAsync(ChatTreeItem item)
    {
        if (!item.IsEditing)
        {
            return;
        }

        var title = item.EditTitle.Trim();
        if (string.IsNullOrWhiteSpace(title))
        {
            item.ResetEditTitle();
            item.IsEditing = false;
            return;
        }

        var previousTitle = item.Chat.Title;
        item.IsEditing = false;
        if (title == previousTitle)
        {
            return;
        }

        item.SetTitle(title);
        try
        {
            var updated = await _client.RenameChatAsync(item.Project.Id, item.Chat.Id, title);
            item.SetTitle(updated?.Title ?? title);
            _selectedProject = item.Project;
            _selectedChat = item.Chat;
            StatusText.Text = $"chat | {item.Project.Name} | {item.Title}";
        }
        catch (Exception ex)
        {
            item.SetTitle(previousTitle);
            StatusText.Text = $"rename chat error | {ShortError(ex)}";
        }
    }

    private void CancelChatTitleEdit(ChatTreeItem item)
    {
        item.ResetEditTitle();
        item.IsEditing = false;
        ProjectTree.Focus();
    }

    private async Task ArchiveChatAsync(ChatTreeItem item)
    {
        if (item.IsArchiving)
        {
            return;
        }

        item.IsArchiving = true;
        BeginActivity($"Archiving chat... | {item.Title}");
        try
        {
            await _client.ArchiveChatAsync(item.Project.Id, item.Chat.Id);
        }
        catch (Exception ex)
        {
            StatusText.Text = $"archive chat warning | {ShortError(ex)}";
        }
        finally
        {
            await RemoveArchivedChatFromTreeAsync(item.Project.Id, item.Chat.Id);
            EndActivity();
        }
    }

    private static string ShortError(Exception ex)
    {
        var message = ex.Message;
        try
        {
            using var document = JsonDocument.Parse(message);
            if (document.RootElement.TryGetProperty("error", out var error) &&
                error.TryGetProperty("message", out var detail) &&
                detail.ValueKind == JsonValueKind.String)
            {
                message = detail.GetString() ?? message;
            }
        }
        catch
        {
        }
        return message.Length > 220 ? message[..217] + "..." : message;
    }

    private async Task RemoveArchivedChatFromTreeAsync(string projectId, string chatId)
    {
        var projectItem = _projectTree.FirstOrDefault(item => item.Project.Id == projectId);
        if (projectItem is null)
        {
            return;
        }

        var removedIndex = projectItem.Chats.ToList().FindIndex(item => item.Chat.Id == chatId);
        if (removedIndex < 0)
        {
            return;
        }

        var nextItem = removedIndex + 1 < projectItem.Chats.Count
            ? projectItem.Chats[removedIndex + 1]
            : removedIndex - 1 >= 0
                ? projectItem.Chats[removedIndex - 1]
                : null;
        var removedWasSelected = _selectedChat?.Id == chatId;
        projectItem.Chats.RemoveAt(removedIndex);

        if (!removedWasSelected)
        {
            StatusText.Text = $"chat archived | {projectItem.Name}";
            return;
        }

        _selectedChat = null;
        _messages.Clear();
        if (nextItem is not null)
        {
            await SelectChatAsync(nextItem);
            await SelectTreeItemAsync(nextItem);
            return;
        }

        await SelectProjectAsync(projectItem.Project);
        await SelectTreeItemAsync(projectItem);
    }

    private async Task SelectTreeItemAsync(object item)
    {
        await Dispatcher.InvokeAsync(() =>
        {
            _suppressTreeSelectionChanged = true;
            try
            {
                ProjectTree.UpdateLayout();
                if (item is ProjectTreeItem projectItem)
                {
                    if (ProjectTree.ItemContainerGenerator.ContainerFromItem(projectItem) is TreeViewItem projectContainer)
                    {
                        projectContainer.IsSelected = true;
                        projectContainer.Focus();
                    }
                    return;
                }

                if (item is ChatTreeItem chatItem)
                {
                    var parentItem = _projectTree.FirstOrDefault(project => project.Project.Id == chatItem.Project.Id);
                    if (parentItem is null ||
                        ProjectTree.ItemContainerGenerator.ContainerFromItem(parentItem) is not TreeViewItem projectContainer)
                    {
                        return;
                    }

                    projectContainer.UpdateLayout();
                    if (projectContainer.ItemContainerGenerator.ContainerFromItem(chatItem) is TreeViewItem chatContainer)
                    {
                        chatContainer.IsSelected = true;
                        chatContainer.Focus();
                    }
                }
            }
            finally
            {
                _suppressTreeSelectionChanged = false;
            }
        }, DispatcherPriority.Loaded);
    }

    private async Task RenameProjectAsync(ProjectDto project)
    {
        await BeginEditProjectAsync(project.Id);
    }

    private async Task BeginEditProjectAsync(string projectId)
    {
        var item = FindProjectItem(projectId);
        if (item is null)
        {
            return;
        }

        foreach (var projectItem in _projectTree)
        {
            projectItem.IsEditing = false;
            foreach (var chatItem in projectItem.Chats)
            {
                chatItem.IsEditing = false;
            }
        }

        item.ResetEditName();
        item.IsEditing = true;
        await Dispatcher.InvokeAsync(() =>
        {
            ProjectTree.UpdateLayout();
            if (FindVisualChild<TextBox>(ProjectTree, textBox => ReferenceEquals(textBox.DataContext, item)) is TextBox editor)
            {
                editor.Focus();
                editor.SelectAll();
            }
        }, DispatcherPriority.Loaded);
    }

    private ProjectTreeItem? FindProjectItem(string projectId)
    {
        return _projectTree.FirstOrDefault(item => item.Project.Id == projectId);
    }

    private async Task SelectProjectInTreeAsync(string projectId)
    {
        var item = FindProjectItem(projectId);
        if (item is null)
        {
            return;
        }
        await SelectProjectAsync(item.Project);
        await Dispatcher.InvokeAsync(() =>
        {
            ProjectTree.UpdateLayout();
            if (ProjectTree.ItemContainerGenerator.ContainerFromItem(item) is TreeViewItem container)
            {
                container.IsSelected = true;
                container.Focus();
            }
        }, DispatcherPriority.Loaded);
    }

    private async Task FocusProjectItemInTreeAsync(ProjectTreeItem item)
    {
        await Dispatcher.InvokeAsync(() =>
        {
            ProjectTree.UpdateLayout();
            if (ProjectTree.ItemContainerGenerator.ContainerFromItem(item) is TreeViewItem container)
            {
                _suppressTreeSelectionChanged = true;
                try
                {
                    container.IsSelected = true;
                    container.Focus();
                }
                finally
                {
                    _suppressTreeSelectionChanged = false;
                }
            }
        }, DispatcherPriority.Loaded);
    }

    private async Task CommitProjectNameEditAsync(ProjectTreeItem item)
    {
        if (!item.IsEditing)
        {
            return;
        }

        var name = item.EditName.Trim();
        if (string.IsNullOrWhiteSpace(name))
        {
            item.ResetEditName();
            item.IsEditing = false;
            return;
        }

        var previousName = item.Project.Name;
        item.IsEditing = false;
        if (name == previousName)
        {
            return;
        }

        item.SetName(name);
        try
        {
            var updated = await _client.RenameProjectAsync(item.Project.Id, name);
            item.SetName(updated?.Name ?? name);
            _selectedProject = item.Project;
            StatusText.Text = $"project | {item.Name} | {item.Project.Path}";
        }
        catch (Exception ex)
        {
            item.SetName(previousName);
            StatusText.Text = $"rename project error | {ShortError(ex)}";
        }
    }

    private void CancelProjectNameEdit(ProjectTreeItem item)
    {
        item.ResetEditName();
        item.IsEditing = false;
        ProjectTree.Focus();
    }

    private async Task DeleteProjectAsync(ProjectDto project)
    {
        var message = $"このプロジェクトの登録を解除します。ファイルやディレクトリは削除されません。\n\n{project.Name}\n{project.Path}";
        if (System.Windows.MessageBox.Show(this, message, "プロジェクト登録解除", MessageBoxButton.OKCancel, MessageBoxImage.Warning) == MessageBoxResult.OK)
        {
            await _client.DeleteProjectAsync(project.Id);
            await RemoveProjectFromTreeAsync(project.Id);
            StatusText.Text = $"project unregistered | {project.Name}";
        }
    }

    private void ProjectMenu_Click(object sender, RoutedEventArgs e)
    {
        OpenContextMenu(sender, e);
    }

    private void ChatMenu_Click(object sender, RoutedEventArgs e)
    {
        OpenContextMenu(sender, e);
    }

    private static void OpenContextMenu(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { ContextMenu: not null } element)
        {
            element.ContextMenu.PlacementTarget = element;
            element.ContextMenu.IsOpen = true;
            e.Handled = true;
        }
    }

    private async void RenameProjectMenu_Click(object sender, RoutedEventArgs e)
    {
        if (GetProjectItemFromMenu(sender) is ProjectTreeItem item)
        {
            await BeginEditProjectAsync(item.Project.Id);
        }
    }

    private async void DeleteProjectMenu_Click(object sender, RoutedEventArgs e)
    {
        if (GetProjectFromMenu(sender) is ProjectDto project)
        {
            await DeleteProjectAsync(project);
        }
    }

    private static ProjectDto? GetProjectFromMenu(object sender)
    {
        return GetProjectItemFromMenu(sender)?.Project;
    }

    private static ProjectTreeItem? GetProjectItemFromMenu(object sender)
    {
        if (sender is MenuItem { Tag: ProjectTreeItem taggedItem })
        {
            return taggedItem;
        }
        if (sender is MenuItem { Parent: ContextMenu { PlacementTarget: FrameworkElement { Tag: ProjectTreeItem item } } })
        {
            return item;
        }
        return null;
    }

    private async void RenameChatMenu_Click(object sender, RoutedEventArgs e)
    {
        if (GetChatFromMenu(sender) is ChatTreeItem item)
        {
            await BeginEditChatAsync(item.Chat.Id);
        }
    }

    private async void ArchiveChatMenu_Click(object sender, RoutedEventArgs e)
    {
        if (GetChatFromMenu(sender) is ChatTreeItem item)
        {
            await ArchiveChatAsync(item);
        }
    }

    private static ChatTreeItem? GetChatFromMenu(object sender)
    {
        if (sender is MenuItem { Tag: ChatTreeItem taggedItem })
        {
            return taggedItem;
        }
        if (sender is MenuItem { Parent: ContextMenu { PlacementTarget: FrameworkElement { Tag: ChatTreeItem item } } })
        {
            return item;
        }
        return null;
    }

    private async void ProjectTree_KeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        if (e.Key == Key.F2)
        {
            e.Handled = true;
            if (ProjectTree.SelectedItem is ChatTreeItem chatItem)
            {
                await SelectChatAsync(chatItem);
                await BeginEditChatAsync(chatItem.Chat.Id);
            }
            else if (ProjectTree.SelectedItem is ProjectTreeItem projectItem)
            {
                await SelectProjectAsync(projectItem.Project);
                await BeginEditProjectAsync(projectItem.Project.Id);
            }
        }
        else if (e.Key == Key.Delete)
        {
            if (ProjectTree.SelectedItem is ChatTreeItem chatItem)
            {
                e.Handled = true;
                StatusText.Text = $"chat | {chatItem.Project.Name} | {chatItem.Title} | Delete is not available. Use archive.";
            }
            else if (ProjectTree.SelectedItem is ProjectTreeItem projectItem)
            {
                e.Handled = true;
                await SelectProjectAsync(projectItem.Project);
                await DeleteProjectAsync(projectItem.Project);
            }
        }
    }

    private async void ProjectNameEdit_KeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        if (sender is not TextBox { DataContext: ProjectTreeItem item })
        {
            return;
        }

        if (e.Key == Key.Enter)
        {
            e.Handled = true;
            await CommitProjectNameEditAsync(item);
            ProjectTree.Focus();
        }
        else if (e.Key == Key.Escape)
        {
            e.Handled = true;
            CancelProjectNameEdit(item);
        }
    }

    private async void ProjectNameEdit_LostKeyboardFocus(object sender, KeyboardFocusChangedEventArgs e)
    {
        if (sender is TextBox { DataContext: ProjectTreeItem item } && item.IsEditing)
        {
            await CommitProjectNameEditAsync(item);
        }
    }

    private async void ChatTitleEdit_KeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        if (sender is not TextBox { DataContext: ChatTreeItem item })
        {
            return;
        }

        if (e.Key == Key.Enter)
        {
            e.Handled = true;
            await CommitChatTitleEditAsync(item);
            ProjectTree.Focus();
        }
        else if (e.Key == Key.Escape)
        {
            e.Handled = true;
            CancelChatTitleEdit(item);
        }
    }

    private async void ChatTitleEdit_LostKeyboardFocus(object sender, KeyboardFocusChangedEventArgs e)
    {
        if (sender is TextBox { DataContext: ChatTreeItem item } && item.IsEditing)
        {
            await CommitChatTitleEditAsync(item);
        }
    }

    private async void Send_Click(object sender, RoutedEventArgs e) => await SubmitMessageBoxAsync();

    private async void NewChatSend_Click(object sender, RoutedEventArgs e) => await SubmitMessageBoxAsync();

    private async void MessageBox_PreviewKeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        await ComposerPreviewKeyDownAsync(e);
    }

    private async void NewChatMessageBox_PreviewKeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        await ComposerPreviewKeyDownAsync(e);
    }

    private async Task ComposerPreviewKeyDownAsync(System.Windows.Input.KeyEventArgs e)
    {
        if (IsComposerImeInputActive(e))
        {
            return;
        }
        if (e.OriginalSource is TextBox textBox && HandleComposerHistoryKey(textBox, e))
        {
            return;
        }
        if (e.Key == Key.V && e.KeyboardDevice.Modifiers.HasFlag(System.Windows.Input.ModifierKeys.Control) && System.Windows.Clipboard.ContainsImage())
        {
            e.Handled = true;
            AddClipboardImageAttachment();
            return;
        }
        if (e.Key == Key.Enter && !e.KeyboardDevice.Modifiers.HasFlag(System.Windows.Input.ModifierKeys.Shift))
        {
            e.Handled = true;
            await SubmitMessageBoxAsync();
        }
    }

    private void RegisterComposerImeHandlers(TextBox textBox)
    {
        textBox.AddHandler(TextCompositionManager.PreviewTextInputStartEvent, new TextCompositionEventHandler(Composer_TextInputStart), true);
        textBox.AddHandler(TextCompositionManager.PreviewTextInputUpdateEvent, new TextCompositionEventHandler(Composer_TextInputUpdate), true);
        textBox.AddHandler(TextCompositionManager.PreviewTextInputEvent, new TextCompositionEventHandler(Composer_TextInput), true);
        textBox.LostKeyboardFocus += (_, _) => _isComposerTextCompositionActive = false;
    }

    private void Composer_TextInputStart(object sender, TextCompositionEventArgs e)
    {
        _isComposerTextCompositionActive = !string.IsNullOrEmpty(e.TextComposition.CompositionText);
    }

    private void Composer_TextInputUpdate(object sender, TextCompositionEventArgs e)
    {
        _isComposerTextCompositionActive = !string.IsNullOrEmpty(e.TextComposition.CompositionText);
    }

    private void Composer_TextInput(object sender, TextCompositionEventArgs e)
    {
        _isComposerTextCompositionActive = false;
    }

    private bool IsComposerImeInputActive(System.Windows.Input.KeyEventArgs e)
    {
        return _isComposerTextCompositionActive || e.Key is Key.ImeProcessed or Key.DeadCharProcessed;
    }

    private void MessageBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (!_isApplyingComposerHistory)
        {
            _composerHistoryIndex = null;
            _composerHistoryDraft = "";
        }
        UpdateCommandButtonState();
    }

    private bool HandleComposerHistoryKey(TextBox textBox, System.Windows.Input.KeyEventArgs e)
    {
        if (e.KeyboardDevice.Modifiers != System.Windows.Input.ModifierKeys.None)
        {
            return false;
        }
        if (e.Key == Key.Up)
        {
            e.Handled = true;
            if (!IsCaretAtTextStart(textBox))
            {
                MoveCaretToTextStart(textBox);
                return true;
            }
            ShowPreviousComposerHistory(textBox);
            return true;
        }
        if (e.Key == Key.Down)
        {
            e.Handled = true;
            if (!IsCaretAtTextEnd(textBox))
            {
                MoveCaretToTextEnd(textBox);
                return true;
            }
            ShowNextComposerHistory(textBox);
            return true;
        }
        return false;
    }

    private static bool IsCaretAtTextStart(TextBox textBox)
    {
        return textBox.CaretIndex <= 0;
    }

    private static bool IsCaretAtTextEnd(TextBox textBox)
    {
        return textBox.CaretIndex >= textBox.Text.Length;
    }

    private static void MoveCaretToTextStart(TextBox textBox)
    {
        textBox.CaretIndex = 0;
        textBox.SelectionLength = 0;
    }

    private static void MoveCaretToTextEnd(TextBox textBox)
    {
        textBox.CaretIndex = textBox.Text.Length;
        textBox.SelectionLength = 0;
    }

    private void ShowPreviousComposerHistory(TextBox textBox)
    {
        if (_composerHistory.Count == 0)
        {
            return;
        }
        if (_composerHistoryIndex is null)
        {
            _composerHistoryDraft = textBox.Text;
            _composerHistoryIndex = _composerHistory.Count;
        }
        if (_composerHistoryIndex <= 0)
        {
            return;
        }
        _composerHistoryIndex--;
        ApplyComposerHistoryText(textBox, _composerHistory[_composerHistoryIndex.Value]);
    }

    private void ShowNextComposerHistory(TextBox textBox)
    {
        if (_composerHistoryIndex is null)
        {
            return;
        }
        if (_composerHistoryIndex >= _composerHistory.Count)
        {
            return;
        }
        _composerHistoryIndex++;
        ApplyComposerHistoryText(
            textBox,
            _composerHistoryIndex == _composerHistory.Count ? _composerHistoryDraft : _composerHistory[_composerHistoryIndex.Value]);
    }

    private void ApplyComposerHistoryText(TextBox textBox, string value)
    {
        _isApplyingComposerHistory = true;
        try
        {
            textBox.Text = value;
            textBox.CaretIndex = textBox.Text.Length;
        }
        finally
        {
            _isApplyingComposerHistory = false;
        }
        UpdateCommandButtonState();
    }

    private void AddComposerHistory(string content)
    {
        var clean = content.Trim();
        if (clean.Length == 0)
        {
            return;
        }
        if (_composerHistory.Count > 0 && _composerHistory[^1] == clean)
        {
            _composerHistoryIndex = null;
            _composerHistoryDraft = "";
            return;
        }
        _composerHistory.Add(clean);
        while (_composerHistory.Count > ComposerHistoryLimit)
        {
            _composerHistory.RemoveAt(0);
        }
        _composerHistoryIndex = null;
        _composerHistoryDraft = "";
    }

    private async Task SubmitMessageBoxAsync()
    {
        UpdateCommandButtonState();
        if (_selectedChat is null)
        {
            if (NewChatSendButton.IsEnabled)
            {
                await SendCurrentMessageAsync();
            }
            return;
        }
        if (!SendButton.IsEnabled)
        {
            return;
        }
        if (SelectedActiveRun() is not null)
        {
            await SteerCurrentRunAsync();
            return;
        }
        await SendCurrentMessageAsync();
    }

    private void AttachFiles_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new Microsoft.Win32.OpenFileDialog
        {
            Multiselect = true,
            Title = "添付ファイルを選択",
            InitialDirectory = WslPathToUncPath("$HOME")
        };
        if (dialog.ShowDialog(this) != true)
        {
            return;
        }
        foreach (var fileName in dialog.FileNames)
        {
            AddAttachmentFromWindowsPath(fileName);
        }
    }

    private void Composer_PreviewDragOver(object sender, System.Windows.DragEventArgs e)
    {
        e.Effects = CanAcceptComposerFileDrop(e) ? DragDropEffects.Copy : DragDropEffects.None;
        e.Handled = true;
    }

    private void Composer_PreviewDrop(object sender, System.Windows.DragEventArgs e)
    {
        e.Handled = true;
        if (!CanAcceptComposerFileDrop(e))
        {
            StatusText.Text = "ここには添付できません";
            return;
        }

        if (e.Data.GetData(System.Windows.DataFormats.FileDrop) is not string[] paths || paths.Length == 0)
        {
            StatusText.Text = "添付できるファイルがありません";
            return;
        }

        var attachedCount = 0;
        var skippedDirectoryCount = 0;
        foreach (var path in paths)
        {
            if (Directory.Exists(path))
            {
                skippedDirectoryCount++;
                continue;
            }
            if (!File.Exists(path))
            {
                continue;
            }
            AddAttachmentFromWindowsPath(path);
            attachedCount++;
        }

        StatusText.Text = attachedCount switch
        {
            > 0 when skippedDirectoryCount > 0 => $"attached | {attachedCount} file(s), skipped {skippedDirectoryCount} folder(s)",
            > 0 => $"attached | {attachedCount} file(s)",
            _ when skippedDirectoryCount > 0 => "フォルダは添付できません",
            _ => "添付できるファイルがありません"
        };
        UpdateCommandButtonState();
    }

    private bool CanAcceptComposerFileDrop(System.Windows.DragEventArgs e)
    {
        if (!e.Data.GetDataPresent(System.Windows.DataFormats.FileDrop))
        {
            return false;
        }
        if (_selectedProject is null || SelectedActiveRun() is not null || IsLikelyExternalProcessing())
        {
            return false;
        }
        return _selectedChat is null || _selectedChat.CanContinue;
    }

    private void PasteImage_Click(object sender, RoutedEventArgs e)
    {
        AddClipboardImageAttachment();
    }

    private void RemoveAttachment_Click(object sender, RoutedEventArgs e)
    {
        if (sender is Button { DataContext: MessageAttachmentDto attachment })
        {
            _pendingAttachments.Remove(attachment);
            StatusText.Text = $"attachment removed | {attachment.Name}";
            UpdateCommandButtonState();
        }
    }

    private void AddClipboardImageAttachment()
    {
        if (!System.Windows.Clipboard.ContainsImage())
        {
            StatusText.Text = "clipboard image not found";
            return;
        }
        var image = System.Windows.Clipboard.GetImage();
        if (image is null)
        {
            StatusText.Text = "clipboard image not found";
            return;
        }
        var directory = AttachmentDirectory();
        Directory.CreateDirectory(directory);
        var fileName = $"clipboard-{DateTimeOffset.Now:yyyyMMdd-HHmmss}-{Guid.NewGuid():N}.png";
        var windowsPath = Path.Combine(directory, fileName);
        using (var stream = File.Create(windowsPath))
        {
            var encoder = new PngBitmapEncoder();
            encoder.Frames.Add(BitmapFrame.Create(image));
            encoder.Save(stream);
        }
        AddAttachmentFromWindowsPath(windowsPath, "image");
    }

    private void AddAttachmentFromWindowsPath(string windowsPath, string? forcedKind = null)
    {
        var kind = forcedKind ?? (IsImagePath(windowsPath) ? "image" : "file");
        _pendingAttachments.Add(new MessageAttachmentDto(
            WindowsPathToWslPath(windowsPath),
            Path.GetFileName(windowsPath),
            kind,
            new Uri(windowsPath).AbsoluteUri));
        WritePerformanceLog("attachment-added", $"windowsPath={LogText(windowsPath)} wslPath={LogText(WindowsPathToWslPath(windowsPath))} kind={LogText(kind)}");
        StatusText.Text = $"attached | {Path.GetFileName(windowsPath)}";
        UpdateCommandButtonState();
    }

    private async Task SendCurrentMessageAsync()
    {
        using var phase = EnterUiPhase("SendCurrentMessage");
        if (_selectedProject is not ProjectDto project)
        {
            return;
        }
        if (_selectedChat is { CanContinue: false } readOnlyChat)
        {
            StatusText.Text = $"read-only chat | {readOnlyChat.ContinueDisabledReason}";
            return;
        }
        var startsNewChat = _selectedChat is null;
        var content = startsNewChat ? NewChatMessageBox.Text : MessageBox.Text;
        var attachments = _pendingAttachments.ToArray();
        if (string.IsNullOrWhiteSpace(content) && attachments.Length == 0)
        {
            return;
        }
        if (string.IsNullOrWhiteSpace(content))
        {
            content = "添付ファイルを確認してください。";
        }
        WritePerformanceLog(
            "send-request",
            $"projectId={LogText(project.Id)} selectedChatId={LogText(_selectedChat?.Id)} startsNewChat={startsNewChat} content={LogText(content)} attachments={LogText(AttachmentLogText(attachments), 12000)}");
        AddComposerHistory(content);
        ChatDto? chat;
        using (EnterUiPhase("SendCurrentMessage/EnsureChat"))
        {
            chat = await EnsureChatForSendAsync(project, content);
        }
        if (chat is null)
        {
            WritePerformanceLog("send-aborted", "reason=chat-null");
            return;
        }
        WritePerformanceLog("send-chat-ready", $"chatId={LogText(chat.Id)} title={LogText(chat.Title)}");
        var runCts = new CancellationTokenSource();
        var runToken = runCts.Token;
        _runProgress.Clear();
        SendButton.IsEnabled = false;
        NewChatSendButton.IsEnabled = false;
        CancelButton.IsEnabled = true;
        RemoveComposerHint();
        RemoveNewChatComposerHint();
        if (startsNewChat)
        {
            NewChatMessageBox.Text = "";
        }
        else
        {
            MessageBox.Text = "";
        }
        _pendingAttachments.Clear();
        string assistantMessageId;
        using (EnterUiPhase("SendCurrentMessage/AppendLocalMessages"))
        {
            AppendMessage(new MessageDto(
                $"local-user-{Guid.NewGuid():N}",
                chat.Id,
                "user",
                content,
                null,
                DateTimeOffset.UtcNow.ToString("O"),
                "instruction",
                attachments),
                scrollToEnd: true);
            assistantMessageId = $"local-assistant-pending-{Guid.NewGuid():N}";
            AppendMessage(new MessageDto(
                assistantMessageId,
                chat.Id,
                "assistant",
                StartingResponseText,
                null,
                DateTimeOffset.UtcNow.ToString("O"),
                "waiting"),
                scrollToEnd: true);
        }
        var sendStartedAt = DateTimeOffset.Now;
        var startHeartbeat = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        startHeartbeat.Tick += (_, _) =>
        {
            var elapsed = DateTimeOffset.Now - sendStartedAt;
            var message = $"開始待ち | {elapsed:mm\\:ss}";
            ShowPendingRunProgressForChat(chat.Id, message);
        };
        startHeartbeat.Start();
        BeginRunActivity(chat.Id, "応答を開始中...");
        ShowPendingRunProgressForChat(chat.Id, "開始待ち");
        var runStarted = false;
        try
        {
            MessagePostResult? result;
            using (EnterUiPhase("SendCurrentMessage/PostMessage"))
            {
                result = await _client.SendMessageAsync(project.Id, chat.Id, content, attachments, runToken);
            }
            if (result is not null)
            {
                WritePerformanceLog("send-run-started", $"runId={LogText(result.RunId)} messageId={LogText(result.MessageId)}");
                var startElapsed = DateTimeOffset.Now - sendStartedAt;
                startHeartbeat.Stop();
                StatusText.Text = "応答中";
                _activeRunsByChat[chat.Id] = new ActiveUiRun(result.RunId, project.Id, chat.Id, runCts);
                ShowRunProgressForChat(chat.Id, $"実行中 | started after {startElapsed.TotalSeconds:F1}s");
                runStarted = true;
                UpdateCommandButtonState();
                using (EnterUiPhase("SendCurrentMessage/StreamRun"))
                {
                    await StreamRunAsync(result.RunId, chat.Id, assistantMessageId, sendStartedAt, runToken);
                }
            }
        }
        catch (OperationCanceledException)
        {
            WritePerformanceLog("send-cancelled", $"runStarted={runStarted}");
            if (!runStarted)
            {
                foreach (var attachment in attachments)
                {
                    _pendingAttachments.Add(attachment);
                }
            }
            ReplaceMessageContentIfWaiting(chat.Id, assistantMessageId, "キャンセルしました");
            StatusText.Text = "キャンセルしました";
            ShowPendingRunProgressForChat(chat.Id, "キャンセルしました");
        }
        catch (Exception ex)
        {
            WritePerformanceLog("send-error", $"type={LogText(ex.GetType().Name)} message={LogText(ex.Message)}");
            foreach (var attachment in attachments)
            {
                _pendingAttachments.Add(attachment);
            }
            ReplaceMessageContentIfWaiting(chat.Id, assistantMessageId, $"send error | {ShortError(ex)}");
            StatusText.Text = $"send error | {ShortError(ex)}";
            ShowPendingRunProgressForChat(chat.Id, $"送信エラー | {ShortError(ex)}");
        }
        finally
        {
            startHeartbeat.Stop();
            EndRunActivity(chat.Id);
            if (!runStarted)
            {
                runCts.Dispose();
            }
            UpdateCommandButtonState();
            CancelButton.IsEnabled = false;
        }
    }

    private async Task<ChatDto?> EnsureChatForSendAsync(ProjectDto project, string firstMessage)
    {
        if (_selectedChat is ChatDto selectedChat)
        {
            return selectedChat;
        }

        var title = TitleFromFirstInstruction(firstMessage);
        try
        {
            var created = await RunActivityAsync("チャットを作成中...", () => _client.CreateChatAsync(project.Id, title));
            if (created is null)
            {
                return null;
            }
            AddCreatedChatToTree(project, created);
            _selectedProject = project;
            _selectedChat = created;
            _messages.Clear();
            UpdateCommandButtonState();
            UpdateRightPaneVisibility();
            StatusText.Text = $"chat | {project.Name} | {created.Title}";
            return created;
        }
        catch (Exception ex)
        {
            StatusText.Text = $"create chat error | {ShortError(ex)}";
            return null;
        }
    }

    private void AddCreatedChatToTree(ProjectDto project, ChatDto chat)
    {
        var projectItem = FindProjectItem(project.Id);
        if (projectItem is null)
        {
            return;
        }
        var existing = projectItem.Chats.FirstOrDefault(item => item.Chat.Id == chat.Id);
        if (existing is not null)
        {
            existing.SetTitle(chat.Title);
            _ = SelectTreeItemAsync(existing);
            return;
        }
        projectItem.IsExpanded = true;
        var chatItem = new ChatTreeItem(project, chat);
        projectItem.Chats.Insert(0, chatItem);
        _ = SelectTreeItemAsync(chatItem);
    }

    private static string TitleFromFirstInstruction(string content)
    {
        var text = string.Join(" ", content.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
        if (string.IsNullOrWhiteSpace(text))
        {
            return "New Chat";
        }
        return text.Length > 80 ? text[..77].TrimEnd() + "..." : text;
    }

    private async Task SteerCurrentRunAsync()
    {
        if (SelectedActiveRun() is not { } activeRun || _selectedChat is not ChatDto chat)
        {
            StatusText.Text = "追加指示は、Codex Liteで応答中のチャットを選択している時だけ送れます。";
            return;
        }
        var runId = activeRun.RunId;
        var content = MessageBox.Text.Trim();
        var attachments = _pendingAttachments.ToArray();
        if (string.IsNullOrWhiteSpace(content) && attachments.Length == 0)
        {
            return;
        }
        if (string.IsNullOrWhiteSpace(content))
        {
            content = "添付ファイルを確認してください。";
        }

        WritePerformanceLog(
            "steer-request",
            $"chatId={LogText(chat.Id)} runId={LogText(runId)} content={LogText(content)} attachments={LogText(AttachmentLogText(attachments), 12000)}");
        MessageBox.Text = "";
        _pendingAttachments.Clear();
        AddComposerHistory(content);
        var localMessageId = $"local-steer-{Guid.NewGuid():N}";
        AppendMessage(new MessageDto(
            localMessageId,
            chat.Id,
            "user",
            content,
            runId,
            DateTimeOffset.UtcNow.ToString("O"),
            "instruction",
            attachments),
            scrollToEnd: true);
        try
        {
            SendButton.IsEnabled = false;
            ShowRunProgressForChat(chat.Id, "追加指示を送信中");
            await _client.SteerRunAsync(runId, content, attachments);
            WritePerformanceLog("steer-sent", $"chatId={LogText(chat.Id)} runId={LogText(runId)}");
            ShowRunProgressForChat(chat.Id, "追加指示を送信済み");
        }
        catch (Exception ex)
        {
            WritePerformanceLog("steer-error", $"chatId={LogText(chat.Id)} runId={LogText(runId)} type={LogText(ex.GetType().Name)} message={LogText(ex.Message)}");
            var localMessage = _messages.FirstOrDefault(message => message.Id == localMessageId);
            if (localMessage is not null)
            {
                _messages.Remove(localMessage);
            }
            MessageBox.Text = content;
            foreach (var attachment in attachments)
            {
                _pendingAttachments.Add(attachment);
            }
            StatusText.Text = $"steer error | {ShortError(ex)}";
            ShowRunProgressForChat(chat.Id, $"追加指示エラー | {ShortError(ex)}");
        }
        finally
        {
            UpdateCommandButtonState();
        }
    }

    private async Task StreamRunAsync(string runId, string chatId, string assistantMessageId, DateTimeOffset sendStartedAt, CancellationToken cancellationToken)
    {
        using var phase = EnterUiPhase("StreamRun");
        BeginRunActivity(chatId, "応答待ち...");
        var currentAssistantMessageId = assistantMessageId;
        MarkAssistantMessageWaiting(chatId, currentAssistantMessageId, runId);
        var startedAt = DateTimeOffset.Now;
        var lastEventAt = startedAt;
        var lastEventName = "connected";
        var reconnectingMessage = "";
        var heartbeat = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        var receivedOutput = false;
        var completed = false;
        heartbeat.Tick += (_, _) =>
        {
            var elapsed = DateTimeOffset.Now - startedAt;
            var idle = DateTimeOffset.Now - lastEventAt;
            var total = DateTimeOffset.Now - sendStartedAt;
            if (!string.IsNullOrWhiteSpace(reconnectingMessage))
            {
                ShowRunProgressForChat(chatId, $"{reconnectingMessage} | stream {elapsed:mm\\:ss} | last {idle:ss}s ago");
            }
            else
            {
                ShowRunProgressForChat(chatId, $"実行中 | stream {elapsed:mm\\:ss} | total {total:mm\\:ss} | last {lastEventName} {idle:ss}s ago");
            }
        };
        heartbeat.Start();
        try
        {
            await foreach (var item in _client.StreamRunEventsAsync(runId, cancellationToken))
            {
                using var eventPhase = EnterUiPhase($"StreamRun/Event/{item.Event}");
                lastEventAt = DateTimeOffset.Now;
                lastEventName = item.Event;
                if (item.Event == "output")
                {
                    reconnectingMessage = "";
                    var text = ExtractSseText(item.Data);
                    WritePerformanceLog("stream-output", $"runId={LogText(runId)} chars={text.Length} text={LogText(text)}");
                    receivedOutput |= !string.IsNullOrEmpty(text);
                    AppendOrUpdateAssistantMessage(chatId, currentAssistantMessageId, runId, text);
                }
                else if (item.Event == "error")
                {
                    reconnectingMessage = "";
                    var text = ExtractSseText(item.Data);
                    WritePerformanceLog("stream-error-event", $"runId={LogText(runId)} chars={text.Length} text={LogText(text)} raw={LogText(item.Data)}");
                    AppendOrUpdateAssistantMessage(chatId, currentAssistantMessageId, runId, text);
                }
                else if (item.Event == "progress")
                {
                    var progress = ExtractSseText(item.Data);
                    var progressMethod = ExtractSseString(item.Data, "method");
                    WritePerformanceLog("stream-progress", $"runId={LogText(runId)} method={LogText(progressMethod)} summary={LogText(progress)} raw={LogText(item.Data)}");
                    if (progressMethod.Equals("app_server/reconnecting", StringComparison.Ordinal))
                    {
                        reconnectingMessage = progress;
                    }
                    else if (!IsLowLevelDeltaProgress(progressMethod, progress))
                    {
                        reconnectingMessage = "";
                    }
                    if (IsAssistantMessageBoundaryProgress(progressMethod))
                    {
                        FlushAssistantMessageText(currentAssistantMessageId);
                        if (HasRealAssistantContent(currentAssistantMessageId))
                        {
                            MarkAssistantMessageCompleted(chatId, currentAssistantMessageId);
                            currentAssistantMessageId = $"local-assistant-boundary-{Guid.NewGuid():N}";
                        }
                        continue;
                    }
                    var isDisplayableProgress = IsDisplayableProgress(progress);
                    if (isDisplayableProgress)
                    {
                        if (!IsLowLevelDeltaProgress(progressMethod, progress))
                        {
                            ShowRunProgressForChat(chatId, $"実行中 | {progress}");
                            AddRunProgress(progress, ProgressCategory(progressMethod, progress));
                        }
                    }
                    if (isDisplayableProgress && ShouldShowInlineProgress(progressMethod, progress))
                    {
                        FlushAssistantMessageText(currentAssistantMessageId);
                        if (HasRealAssistantContent(currentAssistantMessageId))
                        {
                            MarkAssistantMessageCompleted(chatId, currentAssistantMessageId);
                        }
                        else
                        {
                            RemoveAssistantPlaceholder(currentAssistantMessageId);
                        }
                        AddInlineProgressMessage(chatId, runId, progress);
                        currentAssistantMessageId = $"local-assistant-progress-{Guid.NewGuid():N}";
                    }
                }
                if (item.Event is "done" or "error")
                {
                    WritePerformanceLog("stream-terminal", $"runId={LogText(runId)} event={LogText(item.Event)} raw={LogText(item.Data)}");
                    MarkChatUnreadIfConversationNotVisible(chatId);
                    FlushAssistantMessageText(currentAssistantMessageId);
                    completed = item.Event == "done";
                    if (completed)
                    {
                        MarkAssistantMessageAsConclusion(chatId, currentAssistantMessageId);
                        MarkAssistantMessageCompleted(chatId, currentAssistantMessageId);
                        StatusText.Text = "応答完了";
                        ShowRunProgressForChat(chatId, "完了");
                    }
                    else
                    {
                        StatusText.Text = "応答エラー";
                        ShowRunProgressForChat(chatId, "エラー");
                    }
                    break;
                }
            }
            if (completed && !receivedOutput)
            {
                ReplaceMessageContentIfWaiting(chatId, currentAssistantMessageId, "応答のストリーム出力なしで完了しました。");
            }
        }
        catch (Exception ex) when (!cancellationToken.IsCancellationRequested)
        {
            ReplaceMessageContentIfWaiting(chatId, currentAssistantMessageId, $"stream error | {ShortError(ex)}");
            ShowRunProgressForChat(chatId, $"ストリームエラー | {ShortError(ex)}");
            throw;
        }
        finally
        {
            heartbeat.Stop();
            if (_activeRunsByChat.TryGetValue(chatId, out var activeRun) && activeRun.RunId == runId)
            {
                _activeRunsByChat.Remove(chatId);
                activeRun.Cancellation.Dispose();
            }
            UpdateCommandButtonState();
            EndRunActivity(chatId);
            HideRunProgressForChat(chatId);
        }
    }

    private void AppendMessage(MessageDto message, bool scrollToEnd)
    {
        InsertMessageInChronologicalOrder(message);
        if (scrollToEnd)
        {
            ScrollMessagesToEnd();
        }
    }

    private void ReplaceMessageById(string chatId, string messageId, MessageDto replacement)
    {
        if (_selectedChat?.Id != chatId)
        {
            return;
        }
        for (var index = 0; index < _messages.Count; index++)
        {
            if (_messages[index].Id == messageId)
            {
                _messages[index] = replacement;
                return;
            }
        }
    }

    private void RemoveMessageById(string chatId, string messageId)
    {
        if (_selectedChat?.Id != chatId)
        {
            return;
        }
        var message = _messages.FirstOrDefault(item => item.Id == messageId);
        if (message is not null)
        {
            _messages.Remove(message);
        }
    }

    private void InsertMessageInChronologicalOrder(MessageDto message)
    {
        if (!IsChronologicalMessage(message))
        {
            _messages.Add(message);
            return;
        }

        var messageCreatedAt = MessageCreatedAt(message);
        var insertIndex = _messages.Count;
        for (var index = 0; index < _messages.Count; index++)
        {
            var current = _messages[index];
            if (!IsChronologicalMessage(current))
            {
                continue;
            }
            var currentCreatedAt = MessageCreatedAt(current);
            if (currentCreatedAt > messageCreatedAt ||
                (currentCreatedAt == messageCreatedAt && string.CompareOrdinal(current.Id, message.Id) > 0))
            {
                insertIndex = index;
                break;
            }
        }
        _messages.Insert(insertIndex, message);
    }

    private static bool IsChronologicalMessage(MessageDto message)
    {
        return !message.Role.Equals("spacer", StringComparison.OrdinalIgnoreCase)
            && !message.Role.Equals("activity", StringComparison.OrdinalIgnoreCase);
    }

    private void AddRunProgress(string content, string? category = null)
    {
        if (!IsDisplayableProgress(content))
        {
            return;
        }
        var entry = new RunProgressEntry(DateTimeOffset.Now, content, category);
        _runProgress.Clear();
        _runProgress.Add(entry);
    }

    private void AddInlineProgressMessage(string chatId, string runId, string content)
    {
        if (string.IsNullOrWhiteSpace(content) || _selectedChat?.Id != chatId)
        {
            return;
        }
        var shouldFollow = IsMessagesScrolledNearEnd();
        var detailLine = InlineProgressDetailLine(content);
        for (var i = _messages.Count - 1; i >= 0; i--)
        {
            var message = _messages[i];
            if (!message.Role.Equals("status", StringComparison.OrdinalIgnoreCase))
            {
                break;
            }
            if (message.RunId == runId)
            {
                _messages[i] = message with
                {
                    Content = content,
                    ActivityDetails = AppendActivityDetail(message.ActivityDetails, detailLine),
                    CreatedAt = DateTimeOffset.UtcNow.ToString("O")
                };
                if (shouldFollow)
                {
                    ScrollMessagesToEnd();
                }
                return;
            }
        }
        AppendMessage(new MessageDto(
            $"local-progress-{Guid.NewGuid():N}",
            chatId,
            "status",
            content,
            runId,
            DateTimeOffset.UtcNow.ToString("O"),
            "status",
            ActivityDetails: detailLine),
            scrollToEnd: shouldFollow);
    }

    private static string InlineProgressDetailLine(string content)
    {
        var timestamp = DateTimeOffset.Now.ToString("HH:mm:ss", CultureInfo.CurrentCulture);
        return $"{timestamp} {content.Trim()}";
    }

    private static string AppendActivityDetail(string? current, string next)
    {
        if (string.IsNullOrWhiteSpace(current))
        {
            return next;
        }
        return current + Environment.NewLine + next;
    }

    private bool HasRealAssistantContent(string messageId)
    {
        var message = _messages.FirstOrDefault(item => item.Id == messageId);
        if (message is null)
        {
            return false;
        }
        var content = message.Content.Trim();
        return content.Length > 0 && message.Kind != "waiting";
    }

    private void RemoveAssistantPlaceholder(string messageId)
    {
        var message = _messages.FirstOrDefault(item => item.Id == messageId);
        if (message is null)
        {
            return;
        }
        if (message.Kind == "waiting")
        {
            _messages.Remove(message);
        }
    }

    private static bool ShouldShowInlineProgress(string method, string content)
    {
        if (method.Equals("app_server/reconnecting", StringComparison.Ordinal))
        {
            return false;
        }
        if (IsInternalStateProgress(content))
        {
            return false;
        }
        if (IsLowLevelDeltaProgress(method, content))
        {
            return false;
        }
        if (IsAssistantMessageBoundaryProgress(method))
        {
            return false;
        }
        return method.StartsWith("item/", StringComparison.Ordinal)
            || method.StartsWith("exec_command_", StringComparison.Ordinal)
            || method.StartsWith("mcp_tool_call_", StringComparison.Ordinal)
            || method.StartsWith("apply_patch_", StringComparison.Ordinal)
            || method == "thread/settings/applied";
    }

    private static bool IsAssistantMessageBoundaryProgress(string method)
    {
        return method.StartsWith("item/agentMessage", StringComparison.Ordinal)
            && !method.Equals("item/agentMessage/delta", StringComparison.Ordinal);
    }

    private static string? ProgressCategory(string method, string content)
    {
        if (method.Equals("app_server/reconnecting", StringComparison.Ordinal))
        {
            return "app-server-reconnecting";
        }
        if (IsInternalStateProgress(content))
        {
            return "internal-state";
        }
        var normalized = string.IsNullOrWhiteSpace(method) ? content.Trim() : method.Trim();
        if (normalized.StartsWith("item/", StringComparison.Ordinal))
        {
            var parts = normalized.Split('/');
            return parts.Length >= 3 ? $"item/{parts[1]}" : "item";
        }
        if (normalized.StartsWith("exec_command_", StringComparison.Ordinal))
        {
            return "exec-command";
        }
        if (normalized.StartsWith("mcp_tool_call_", StringComparison.Ordinal))
        {
            return "mcp-tool-call";
        }
        if (normalized.StartsWith("apply_patch_", StringComparison.Ordinal))
        {
            return "apply-patch";
        }
        return normalized.Length > 0 ? normalized : null;
    }

    private static bool IsLowLevelDeltaProgress(string method, string content)
    {
        var normalizedMethod = method.Trim();
        var normalizedContent = content.Trim();
        return normalizedMethod.Contains("outputDelta", StringComparison.OrdinalIgnoreCase)
            || normalizedMethod.Contains("delta", StringComparison.OrdinalIgnoreCase)
            || normalizedContent.Contains("outputDelta", StringComparison.OrdinalIgnoreCase)
            || normalizedContent.Contains("/delta", StringComparison.OrdinalIgnoreCase);
    }

    private static bool IsDisplayableProgress(string content)
    {
        if (string.IsNullOrWhiteSpace(content))
        {
            return false;
        }

        var normalized = content.Trim();
        return !normalized.Equals("inProgress", StringComparison.OrdinalIgnoreCase)
            && !normalized.Equals("completed", StringComparison.OrdinalIgnoreCase)
            && !normalized.Equals("agentMessage", StringComparison.OrdinalIgnoreCase);
    }

    private static bool IsInternalStateProgress(string content)
    {
        var normalized = content.Trim();
        return normalized.Equals("userMessage", StringComparison.OrdinalIgnoreCase)
            || normalized.Equals("assistantMessage", StringComparison.OrdinalIgnoreCase)
            || normalized.Equals("systemMessage", StringComparison.OrdinalIgnoreCase)
            || normalized.Equals("toolMessage", StringComparison.OrdinalIgnoreCase);
    }

    private static string MessageKindForAssistantMessageId(string messageId)
    {
        return messageId.StartsWith("local-assistant-progress-", StringComparison.Ordinal)
            ? "work"
            : "conclusion";
    }

    private void AppendOrUpdateAssistantMessage(string chatId, string messageId, string runId, string delta)
    {
        if (string.IsNullOrEmpty(delta))
        {
            return;
        }
        QueueAssistantMessageDelta(chatId, messageId, runId, delta);
    }

    private void QueueAssistantMessageDelta(string chatId, string messageId, string runId, string delta)
    {
        if (!_streamingText.TryGetValue(messageId, out var state))
        {
            state = new StreamingTextState(runId, chatId);
            _streamingText[messageId] = state;
        }
        state.Pending.Append(delta);
        if (!_streamingTextTimer.IsEnabled)
        {
            _streamingTextTimer.Start();
        }
    }

    private void StreamingTextTimer_Tick(object? sender, EventArgs e)
    {
        using var phase = EnterUiPhase("StreamingTextTimer");
        foreach (var entry in _streamingText.ToList())
        {
            var messageId = entry.Key;
            var state = entry.Value;
            if (state.Pending.Length == 0)
            {
                _streamingText.Remove(messageId);
                continue;
            }

            var count = Math.Min(StreamingCharactersPerTick, state.Pending.Length);
            var next = state.Pending.ToString(0, count);
            state.Pending.Remove(0, count);
            AppendAssistantMessageTextNow(state.ChatId, messageId, state.RunId, next);
            if (state.Pending.Length == 0)
            {
                _streamingText.Remove(messageId);
            }
        }

        if (_streamingText.Count == 0)
        {
            _streamingTextTimer.Stop();
        }
    }

    private void FlushAssistantMessageText(string messageId)
    {
        if (!_streamingText.TryGetValue(messageId, out var state))
        {
            return;
        }
        if (state.Pending.Length > 0)
        {
            var text = state.Pending.ToString();
            state.Pending.Clear();
            AppendAssistantMessageTextNow(state.ChatId, messageId, state.RunId, text);
        }
        _streamingText.Remove(messageId);
        if (_streamingText.Count == 0)
        {
            _streamingTextTimer.Stop();
        }
    }

    private void AppendAssistantMessageTextNow(string chatId, string messageId, string runId, string text)
    {
        using var phase = EnterUiPhase("AppendAssistantMessageText");
        MarkChatUnreadIfConversationNotVisible(chatId);
        if (_selectedChat?.Id != chatId)
        {
            return;
        }
        var shouldFollow = IsMessagesScrolledNearEnd();
        var index = -1;
        for (var i = 0; i < _messages.Count; i++)
        {
            if (_messages[i].Id == messageId)
            {
                index = i;
                break;
            }
        }
        if (index < 0)
        {
            AppendMessage(new MessageDto(
                messageId,
                chatId,
                "assistant",
                text,
                runId,
                DateTimeOffset.UtcNow.ToString("O"),
                MessageKindForAssistantMessageId(messageId)),
                scrollToEnd: shouldFollow);
            return;
        }
        var current = _messages[index];
        var isPlaceholder = current.Kind == "waiting";
        var content = isPlaceholder ? text : current.Content + text;
        _messages[index] = current with
        {
            Content = content,
            Kind = isPlaceholder ? MessageKindForAssistantMessageId(messageId) : current.Kind,
            CreatedAt = isPlaceholder ? DateTimeOffset.UtcNow.ToString("O") : current.CreatedAt
        };
        if (shouldFollow)
        {
            ScrollMessagesToEndThrottled();
        }
    }

    private void ReplaceMessageContentIfWaiting(string chatId, string messageId, string content)
    {
        if (_selectedChat?.Id != chatId)
        {
            return;
        }
        var shouldFollow = IsMessagesScrolledNearEnd();
        for (var i = 0; i < _messages.Count; i++)
        {
            if (_messages[i].Id == messageId && _messages[i].Kind == "waiting")
            {
                _messages[i] = _messages[i] with { Content = content, Kind = MessageKindForAssistantMessageId(messageId) };
                if (shouldFollow)
                {
                    ScrollMessagesToEnd();
                }
                return;
            }
        }
    }

    private void MarkAssistantMessageCompleted(string chatId, string messageId)
    {
        if (_selectedChat?.Id != chatId)
        {
            return;
        }
        for (var i = 0; i < _messages.Count; i++)
        {
            if (_messages[i].Id == messageId)
            {
                _messages[i] = _messages[i] with { CreatedAt = DateTimeOffset.UtcNow.ToString("O") };
                return;
            }
        }
    }

    private void MarkAssistantMessageAsConclusion(string chatId, string messageId)
    {
        if (_selectedChat?.Id != chatId)
        {
            return;
        }
        for (var i = 0; i < _messages.Count; i++)
        {
            var message = _messages[i];
            if (message.Id != messageId || !message.Id.StartsWith("local-assistant-progress-", StringComparison.Ordinal))
            {
                continue;
            }
            if (message.Kind == "waiting")
            {
                return;
            }
            _messages[i] = message with { Id = $"local-assistant-final-{Guid.NewGuid():N}", Kind = "conclusion" };
            return;
        }
    }

    private void MarkAssistantMessageWaiting(string chatId, string messageId, string runId)
    {
        if (_selectedChat?.Id != chatId)
        {
            return;
        }
        for (var i = 0; i < _messages.Count; i++)
        {
            if (_messages[i].Id == messageId)
            {
                _messages[i] = _messages[i] with { Content = WaitingForResponseText, RunId = runId, Kind = "waiting" };
                if (IsMessagesScrolledNearEnd())
                {
                    ScrollMessagesToEnd();
                }
                return;
            }
        }

        AppendMessage(new MessageDto(
            messageId,
            chatId,
            "assistant",
            WaitingForResponseText,
            runId,
            DateTimeOffset.UtcNow.ToString("O"),
            "waiting"),
            scrollToEnd: IsMessagesScrolledNearEnd());
    }

    private static string ExtractSseText(string data)
    {
        try
        {
            using var document = JsonDocument.Parse(data);
            var root = document.RootElement;
            if (root.TryGetProperty("text", out var text) && text.ValueKind == JsonValueKind.String)
            {
                return text.GetString() ?? "";
            }
            if (root.TryGetProperty("message", out var message) && message.ValueKind == JsonValueKind.String)
            {
                return message.GetString() ?? "";
            }
            if (root.TryGetProperty("summary", out var summary) && summary.ValueKind == JsonValueKind.String)
            {
                return summary.GetString() ?? "";
            }
            if (root.TryGetProperty("method", out var method) && method.ValueKind == JsonValueKind.String)
            {
                return method.GetString() ?? "";
            }
        }
        catch
        {
        }
        return "";
    }

    private static string ExtractSseString(string data, string propertyName)
    {
        try
        {
            using var document = JsonDocument.Parse(data);
            var root = document.RootElement;
            if (root.TryGetProperty(propertyName, out var value) && value.ValueKind == JsonValueKind.String)
            {
                return value.GetString() ?? "";
            }
        }
        catch
        {
        }
        return "";
    }

    private async void Cancel_Click(object sender, RoutedEventArgs e)
    {
        var activeRun = SelectedActiveRun();
        var runId = activeRun?.RunId;
        CancelButton.IsEnabled = false;
        StatusText.Text = runId is null ? "キャンセル要求中 | 開始待ち" : "キャンセル要求中";
        activeRun?.Cancellation.Cancel();
        try
        {
            if (runId is not null)
            {
                using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                await _client.CancelRunAsync(runId, timeout.Token);
            }
        }
        catch (Exception ex)
        {
            StatusText.Text = $"cancel warning | {ShortError(ex)}";
        }
    }

    private async void FilesRoot_Click(object sender, RoutedEventArgs e) => await RefreshFilesAsync("");

    private async void FilesUp_Click(object sender, RoutedEventArgs e)
    {
        if (_currentDirectoryPath.Length == 0)
        {
            return;
        }
        var parent = Path.GetDirectoryName(_currentDirectoryPath.Replace('/', Path.DirectorySeparatorChar))?.Replace('\\', '/') ?? "";
        await RefreshFilesAsync(parent == "." ? "" : parent);
    }

    private async void FilesTreeItem_Expanded(object sender, RoutedEventArgs e)
    {
        if (e.OriginalSource is not TreeViewItem { DataContext: FileTreeItem item } ||
            item.IsPlaceholder ||
            !item.IsDirectory ||
            item.IsLoaded ||
            item.IsLoading ||
            _selectedProject is not ProjectDto project)
        {
            return;
        }

        item.BeginLoading();
        BeginActivity($"ファイルを読み込み中... | {item.Path}");
        try
        {
            var entries = (await _client.ListFilesAsync(project.Id, item.Path))?.Entries ?? [];
            item.SetChildren(entries);
        }
        catch (Exception ex)
        {
            item.SetChildren([]);
            StatusText.Text = $"file list error | {ShortError(ex)}";
        }
        finally
        {
            EndActivity();
        }
    }

    private async void FilesTree_SelectedItemChanged(object sender, RoutedPropertyChangedEventArgs<object> e)
    {
        if (_selectedProject is not ProjectDto project || e.NewValue is not FileTreeItem item || item.IsPlaceholder)
        {
            return;
        }

        if (item.IsDirectory)
        {
            _currentDirectoryPath = item.Path;
            FilePathText.Text = item.Path.Length == 0 ? "/" : item.Path;
            _currentFilePath = "";
            ClearFilePreview();
            UpdateCommandButtonState();
            return;
        }

        _currentFilePath = item.Path;
        FilePathText.Text = item.Path;
        UpdateCommandButtonState();
        ClearFilePreview();
        if (item.ViewerKind == "markdown")
        {
            SetFileWrapControl(enabled: false, isChecked: true);
            var content = await _client.ReadFileAsync(project.Id, item.Path);
            FileMarkdownPreview.Markdown = content?.Content ?? "";
            FileMarkdownPreview.Visibility = Visibility.Visible;
        }
        else if (item.ViewerKind == "text")
        {
            SetFileWrapControl(enabled: true, isChecked: _wrapFileText);
            var content = await _client.ReadFileAsync(project.Id, item.Path);
            FileContentBox.Text = content?.Content ?? "";
            FileContentBox.Visibility = Visibility.Visible;
        }
        else if (IsImageViewerKind(item.ViewerKind))
        {
            SetFileWrapControl(enabled: false, isChecked: false);
            FileImagePreview.Source = LoadImageSource(ToUncPath(project.Path, item.Path));
            FileImagePanel.Visibility = Visibility.Visible;
        }
        else
        {
            SetFileWrapControl(enabled: false, isChecked: false);
            FileContentBox.Text = PreviewUnavailableText(item.ViewerKind);
            FileContentBox.Visibility = Visibility.Visible;
        }
    }

    private async void MarkdownViewer_LinkClicked(object sender, MarkdownLinkClickedEventArgs e)
    {
        await OpenMarkdownLinkAsync(e.Target);
    }

    private async Task OpenMarkdownLinkAsync(string target)
    {
        var cleanTarget = target.Trim();
        if (cleanTarget.Length == 0)
        {
            return;
        }

        if (HasUriScheme(cleanTarget))
        {
            StartShell(cleanTarget, "");
            return;
        }

        if (_selectedProject is not ProjectDto project)
        {
            StatusText.Text = "file link ignored | project not selected";
            return;
        }

        if (!TryNormalizeProjectRelativePath(project.Path, cleanTarget, out var relativePath))
        {
            StatusText.Text = $"file link ignored | outside project | {cleanTarget}";
            return;
        }

        await SelectFileTabPathAsync(project, relativePath);
    }

    private async Task SelectFileTabPathAsync(ProjectDto project, string relativePath)
    {
        if (relativePath.Length == 0)
        {
            FilesTab.IsSelected = true;
            await RefreshFilesAsync("");
            return;
        }

        FilesTab.IsSelected = true;
        await RefreshFilesAsync("");
        FilesTree.UpdateLayout();

        var parts = relativePath.Split('/', StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length == 0)
        {
            return;
        }

        ItemsControl container = FilesTree;
        ObservableCollection<FileTreeItem> items = _files;
        FileTreeItem? currentItem = null;
        for (var index = 0; index < parts.Length; index++)
        {
            var currentPath = string.Join('/', parts.Take(index + 1));
            currentItem = items.FirstOrDefault(item => item.Path.Equals(currentPath, StringComparison.Ordinal));
            if (currentItem is null)
            {
                StatusText.Text = $"file link not found | {relativePath}";
                return;
            }

            container.UpdateLayout();
            if (container.ItemContainerGenerator.ContainerFromItem(currentItem) is not TreeViewItem treeItem)
            {
                StatusText.Text = $"file link not visible | {relativePath}";
                return;
            }

            if (index == parts.Length - 1)
            {
                treeItem.IsSelected = true;
                treeItem.Focus();
                treeItem.BringIntoView();
                return;
            }

            if (!currentItem.IsDirectory)
            {
                StatusText.Text = $"file link parent is not directory | {relativePath}";
                return;
            }

            await EnsureFileTreeItemLoadedAsync(project, currentItem);
            treeItem.IsExpanded = true;
            treeItem.UpdateLayout();
            container = treeItem;
            items = currentItem.Children;
        }
    }

    private async Task EnsureFileTreeItemLoadedAsync(ProjectDto project, FileTreeItem item)
    {
        if (item.IsLoaded || item.IsLoading || !item.IsDirectory)
        {
            return;
        }

        item.BeginLoading();
        BeginActivity($"ファイルを読み込み中... | {item.Path}");
        try
        {
            var entries = (await _client.ListFilesAsync(project.Id, item.Path))?.Entries ?? [];
            item.SetChildren(entries);
        }
        catch (Exception ex)
        {
            item.SetChildren([]);
            StatusText.Text = $"file list error | {ShortError(ex)}";
        }
        finally
        {
            EndActivity();
        }
    }

    private static bool IsImageViewerKind(string viewerKind) =>
        viewerKind is "png" or "jpeg" or "image";

    private static string PreviewUnavailableText(string viewerKind) =>
        viewerKind switch
        {
            "pdf" => "PDFはプレビュー対象外です。",
            "word" => "Word文書はプレビュー対象外です。",
            "excel" => "Excelファイルはプレビュー対象外です。",
            "binary" => "バイナリファイルはプレビュー対象外です。",
            "too_large" => "大きなテキストファイルはプレビュー対象外です。",
            _ => "このファイル種別はプレビュー対象外です。"
        };

    private void ClearFilePreview()
    {
        FileContentBox.Text = "";
        FileContentBox.Visibility = Visibility.Collapsed;
        FileMarkdownPreview.Markdown = "";
        FileMarkdownPreview.Visibility = Visibility.Collapsed;
        FileImagePanel.Visibility = Visibility.Collapsed;
        FileImagePreview.Source = null;
        SetFileWrapControl(enabled: false, isChecked: false);
    }

    private void SetFileWrapControl(bool enabled, bool isChecked)
    {
        FileWrapCheckBox.IsEnabled = enabled;
        FileWrapCheckBox.IsChecked = isChecked;
    }

    private void FileContentBox_PreviewMouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (FindVisualChild<ScrollViewer>(FileContentBox, _ => true) is not ScrollViewer scrollViewer)
        {
            return;
        }

        e.Handled = true;
        scrollViewer.ScrollToVerticalOffset(scrollViewer.VerticalOffset - e.Delta / 3.0);
    }

    private void OpenFileInCode_Click(object sender, RoutedEventArgs e)
    {
        if (_selectedProject is ProjectDto project && _currentFilePath.Length > 0)
        {
            StartShell("code", CodeWslArguments(project.Path, _currentFilePath, gotoFile: true));
        }
    }

    private void OpenFileExternal_Click(object sender, RoutedEventArgs e)
    {
        if (_selectedProject is ProjectDto project && _currentFilePath.Length > 0)
        {
            StartShell(ToUncPath(project.Path, _currentFilePath), "");
        }
    }

    private void OpenProjectExplorer_Click(object sender, RoutedEventArgs e)
    {
        if (_selectedProject is ProjectDto project)
        {
            StartShell("explorer.exe", $"\"{ToUncPath(project.Path, "")}\"");
        }
    }

    private void OpenProjectCode_Click(object sender, RoutedEventArgs e)
    {
        if (_selectedProject is ProjectDto project)
        {
            StartShell("code", CodeArguments(project.Path, "", gotoFile: false));
        }
    }

    private static void StartShell(string fileName, string arguments)
    {
        Process.Start(new ProcessStartInfo { FileName = fileName, Arguments = arguments, UseShellExecute = true });
    }

    private string ToUncPath(string projectPath, string relativePath)
    {
        var full = JoinWslPath(projectPath, relativePath).Replace('/', '\\');
        return $"\\\\wsl.localhost\\{_wslDistroName}{full}";
    }

    private string CodeArguments(string projectPath, string relativePath, bool gotoFile)
    {
        var target = JoinWslPath(projectPath, relativePath);
        if (IsNativeWslPath(projectPath))
        {
            var command = gotoFile ? "--goto " : "";
            return $"--remote wsl+{_wslDistroName} {command}\"{EscapeArgument(target)}\"";
        }

        var windowsPath = ToWindowsPath(target);
        var windowsCommand = gotoFile ? "--goto " : "";
        return $"{windowsCommand}\"{EscapeArgument(windowsPath)}\"";
    }

    private string CodeWslArguments(string projectPath, string relativePath, bool gotoFile)
    {
        var command = gotoFile ? "--goto " : "";
        return $"--remote wsl+{_wslDistroName} {command}\"{EscapeArgument(JoinWslPath(projectPath, relativePath))}\"";
    }

    private static string JoinWslPath(string projectPath, string relativePath)
    {
        return string.IsNullOrEmpty(relativePath)
            ? projectPath.TrimEnd('/')
            : $"{projectPath.TrimEnd('/')}/{relativePath}";
    }

    private static bool IsNativeWslPath(string path)
    {
        return path.StartsWith('/') && !IsMntDrivePath(path);
    }

    private static bool IsMntDrivePath(string path)
    {
        return path.Length >= 7 &&
               path.StartsWith("/mnt/", StringComparison.Ordinal) &&
               char.IsAsciiLetter(path[5]) &&
               path[6] == '/';
    }

    private string ToWindowsPath(string wslPath)
    {
        if (IsMntDrivePath(wslPath))
        {
            var drive = char.ToUpperInvariant(wslPath[5]);
            return $"{drive}:\\{wslPath[7..].Replace('/', '\\')}";
        }
        return ToUncPath(wslPath, "");
    }

    private static bool HasUriScheme(string target)
    {
        if (Regex.IsMatch(target, @"^[A-Za-z]:[\\/]", RegexOptions.CultureInvariant))
        {
            return false;
        }
        return Regex.IsMatch(target, @"^[A-Za-z][A-Za-z0-9+.-]*:", RegexOptions.CultureInvariant);
    }

    private static bool TryNormalizeProjectRelativePath(string projectPath, string target, out string relativePath)
    {
        relativePath = "";
        var localTarget = target.Split('#', 2)[0].Trim();
        if (localTarget.Length == 0)
        {
            return false;
        }

        try
        {
            localTarget = Uri.UnescapeDataString(localTarget);
        }
        catch
        {
        }
        localTarget = localTarget.Replace('\\', '/');

        var projectFullPath = Path.GetFullPath(projectPath).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        string candidateFullPath;
        if (localTarget.StartsWith("/", StringComparison.Ordinal))
        {
            candidateFullPath = Path.GetFullPath(localTarget);
        }
        else
        {
            candidateFullPath = Path.GetFullPath(Path.Combine(projectFullPath, localTarget.Replace('/', Path.DirectorySeparatorChar)));
        }

        if (!IsPathWithinDirectory(candidateFullPath, projectFullPath))
        {
            return false;
        }

        var relative = Path.GetRelativePath(projectFullPath, candidateFullPath).Replace('\\', '/');
        relativePath = relative == "." ? "" : relative;
        return !relativePath.StartsWith("../", StringComparison.Ordinal) && relativePath != "..";
    }

    private static bool IsPathWithinDirectory(string candidateFullPath, string directoryFullPath)
    {
        return candidateFullPath.Equals(directoryFullPath, StringComparison.Ordinal) ||
               candidateFullPath.StartsWith(directoryFullPath + Path.DirectorySeparatorChar, StringComparison.Ordinal);
    }

    private static bool IsImagePath(string path)
    {
        return Path.GetExtension(path).ToLowerInvariant() is ".png" or ".jpg" or ".jpeg" or ".gif" or ".webp" or ".bmp";
    }

    private static BitmapImage? LoadImageSource(string path)
    {
        try
        {
            var image = new BitmapImage();
            image.BeginInit();
            image.CacheOption = BitmapCacheOption.OnLoad;
            image.UriSource = new Uri(path, UriKind.Absolute);
            image.EndInit();
            image.Freeze();
            return image;
        }
        catch
        {
            return null;
        }
    }

    private static string EscapeArgument(string value)
    {
        return value.Replace("\"", "\\\"");
    }

    private void InitializeCommandButtonIcons()
    {
        var explorerPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "explorer.exe");
        SetButtonIcon(OpenProjectExplorerButton, explorerPath, "Explorer");
        SetButtonIcon(OpenFileInCodeButton, FindCodeExecutablePath(), "");
        SetButtonIcon(OpenProjectCodeButton, FindCodeExecutablePath(), "VS Code");
    }

    private static void SetButtonIcon(Button button, string? executablePath, string label)
    {
        if (string.IsNullOrWhiteSpace(executablePath) || !File.Exists(executablePath))
        {
            button.Content = string.IsNullOrWhiteSpace(label) ? "VS" : label;
            return;
        }

        var icon = TryLoadIconSource(executablePath);
        if (icon is null)
        {
            button.Content = label;
            return;
        }

        button.Content = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Children =
            {
                new System.Windows.Controls.Image
                {
                    Source = icon,
                    Width = 16,
                    Height = 16,
                    Margin = string.IsNullOrWhiteSpace(label) ? new Thickness(0) : new Thickness(0, 0, 5, 0),
                    VerticalAlignment = VerticalAlignment.Center
                },
            }
        };
        if (!string.IsNullOrWhiteSpace(label) && button.Content is StackPanel stack)
        {
            stack.Children.Add(new TextBlock
            {
                Text = label,
                VerticalAlignment = VerticalAlignment.Center
            });
        }
    }

    private static ImageSource? TryLoadIconSource(string executablePath)
    {
        try
        {
            using var icon = DrawingIcon.ExtractAssociatedIcon(executablePath);
            if (icon is null)
            {
                return null;
            }

            return Imaging.CreateBitmapSourceFromHIcon(
                icon.Handle,
                Int32Rect.Empty,
                BitmapSizeOptions.FromWidthAndHeight(16, 16));
        }
        catch
        {
            return null;
        }
    }

    private static string? FindCodeExecutablePath()
    {
        if (Registry.GetValue(@"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\App Paths\Code.exe", "", null) is string userPath &&
            File.Exists(userPath))
        {
            return userPath;
        }

        if (Registry.GetValue(@"HKEY_LOCAL_MACHINE\Software\Microsoft\Windows\CurrentVersion\App Paths\Code.exe", "", null) is string machinePath &&
            File.Exists(machinePath))
        {
            return machinePath;
        }

        var candidates = new[]
        {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Microsoft VS Code", "Code.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Microsoft VS Code", "Code.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), "Microsoft VS Code", "Code.exe")
        };

        return candidates.FirstOrDefault(File.Exists);
    }

    private static T? FindVisualChild<T>(DependencyObject parent, Func<T, bool> predicate) where T : DependencyObject
    {
        for (var i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++)
        {
            var child = VisualTreeHelper.GetChild(parent, i);
            if (child is T typed && predicate(typed))
            {
                return typed;
            }

            var match = FindVisualChild(child, predicate);
            if (match is not null)
            {
                return match;
            }
        }
        return null;
    }

    private static ProjectTreeItem? ProjectItemFromOriginalSource(object originalSource)
    {
        if (originalSource is not DependencyObject source)
        {
            return null;
        }

        var container = FindVisualAncestor<TreeViewItem>(source);
        return container?.DataContext as ProjectTreeItem;
    }

    private ProjectTreeItem? ProjectDropTargetFromOriginalSource(object originalSource)
    {
        if (originalSource is not DependencyObject source)
        {
            return null;
        }

        var current = source;
        while (current is not null)
        {
            if (current is TreeViewItem { DataContext: ProjectTreeItem projectItem })
            {
                return projectItem;
            }
            if (current is TreeViewItem { DataContext: ChatTreeItem chatItem })
            {
                return FindProjectItem(chatItem.Project.Id);
            }
            current = VisualTreeHelper.GetParent(current);
        }
        return null;
    }

    private static T? FindVisualAncestor<T>(DependencyObject source) where T : DependencyObject
    {
        var current = source;
        while (current is not null)
        {
            if (current is T typed)
            {
                return typed;
            }
            current = VisualTreeHelper.GetParent(current);
        }
        return null;
    }

    private string? Prompt(string title, string initialValue)
    {
        var dialog = new Window
        {
            Owner = this,
            Title = title,
            Width = 520,
            Height = 140,
            WindowStartupLocation = WindowStartupLocation.CenterOwner,
            ResizeMode = ResizeMode.NoResize
        };
        var panel = new DockPanel { Margin = new Thickness(12) };
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = System.Windows.HorizontalAlignment.Right };
        var input = new TextBox { Text = initialValue, Margin = new Thickness(0, 0, 0, 12) };
        var ok = new Button { Content = "OK", IsDefault = true, MinWidth = 80, Margin = new Thickness(0, 0, 8, 0) };
        var cancel = new Button { Content = "キャンセル", IsCancel = true, MinWidth = 80 };
        ok.Click += (_, _) => { dialog.DialogResult = true; dialog.Close(); };
        buttons.Children.Add(ok);
        buttons.Children.Add(cancel);
        DockPanel.SetDock(buttons, Dock.Bottom);
        panel.Children.Add(buttons);
        panel.Children.Add(input);
        dialog.Content = panel;
        input.SelectAll();
        return dialog.ShowDialog() == true ? input.Text : null;
    }

    private sealed class UiPhaseScope : IDisposable
    {
        private readonly MainWindow _owner;
        private readonly string _phase;
        private readonly string _previous;
        private readonly Stopwatch _stopwatch = Stopwatch.StartNew();
        private bool _disposed;

        public UiPhaseScope(MainWindow owner, string phase, string previous)
        {
            _owner = owner;
            _phase = phase;
            _previous = previous;
        }

        public void Dispose()
        {
            if (_disposed)
            {
                return;
            }

            _disposed = true;
            _stopwatch.Stop();
            _owner.LeaveUiPhase(_phase, _previous, _stopwatch.Elapsed);
        }
    }
}

public sealed record UiState(
    IReadOnlyList<string> ExpandedProjectIds,
    IReadOnlyList<string>? ProjectOrderIds = null,
    WindowPlacementState? Window = null,
    string? TextSize = null,
    bool WrapFileText = false,
    double? TreePaneWidth = null,
    string? CodexHomeMode = null,
    string? SelectedProjectId = null,
    string? SelectedChatId = null,
    string? SelectedTab = null);

public sealed record WindowPlacementState(
    double Left,
    double Top,
    double Width,
    double Height,
    bool IsMaximized = false);

public enum ProjectDropPlacementKind
{
    Before,
    After
}

public sealed class StreamingTextState(string runId, string chatId)
{
    public string RunId { get; } = runId;

    public string ChatId { get; } = chatId;

    public StringBuilder Pending { get; } = new();
}

public sealed class RunProgressEntry(DateTimeOffset timestamp, string content, string? category = null)
{
    public string? Category { get; } = category;

    public string DisplayText { get; } = $"{timestamp:HH:mm:ss}  {content}";
}

public sealed class SubtractConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
    {
        var source = value is double number ? number : 0;
        var subtract = parameter is string text && double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed)
            ? parsed
            : 0;
        return Math.Max(0, source - subtract);
    }

    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture) =>
        Binding.DoNothing;
}

public sealed class ScaleConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
    {
        var source = value is double number ? number : 0;
        var scale = parameter is string text && double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed)
            ? parsed
            : 1;
        return Math.Max(0, source * scale);
    }

    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture) =>
        Binding.DoNothing;
}
