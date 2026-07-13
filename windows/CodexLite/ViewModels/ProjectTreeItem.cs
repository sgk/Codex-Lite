using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using CodexLite.Models;

namespace CodexLite.ViewModels;

public sealed class ProjectTreeItem : INotifyPropertyChanged
{
    public ProjectTreeItem(ProjectDto project, bool isExpanded = false)
    {
        Project = project;
        EditName = project.Name;
        _isExpanded = isExpanded;
    }

    public ProjectDto Project { get; private set; }

    public string Name => Project.Name;

    public string EditName { get; set; }

    private bool _isEditing;
    private bool _isExpanded;
    private bool _showDropBefore;
    private bool _showDropAfter;
    private bool _isPending;

    public bool IsEditing
    {
        get => _isEditing;
        set
        {
            if (_isEditing == value)
            {
                return;
            }
            _isEditing = value;
            OnPropertyChanged();
        }
    }

    public bool IsExpanded
    {
        get => _isExpanded;
        set
        {
            if (_isExpanded == value)
            {
                return;
            }
            _isExpanded = value;
            OnPropertyChanged();
        }
    }

    public bool ShowDropBefore
    {
        get => _showDropBefore;
        set
        {
            if (_showDropBefore == value)
            {
                return;
            }
            _showDropBefore = value;
            OnPropertyChanged();
        }
    }

    public bool ShowDropAfter
    {
        get => _showDropAfter;
        set
        {
            if (_showDropAfter == value)
            {
                return;
            }
            _showDropAfter = value;
            OnPropertyChanged();
        }
    }

    public bool IsPending
    {
        get => _isPending;
        set
        {
            if (_isPending == value)
            {
                return;
            }
            _isPending = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(IsAvailable));
        }
    }

    public bool IsAvailable => !IsPending;

    public ObservableCollection<ChatTreeItem> Chats { get; } = new();

    public event PropertyChangedEventHandler? PropertyChanged;

    public void SetName(string name)
    {
        Project = Project with { Name = name };
        EditName = name;
        OnPropertyChanged(nameof(Name));
        OnPropertyChanged(nameof(EditName));
    }

    public void SetProject(ProjectDto project)
    {
        Project = project;
        EditName = project.Name;
        IsPending = false;
        OnPropertyChanged(nameof(Project));
        OnPropertyChanged(nameof(Name));
        OnPropertyChanged(nameof(EditName));
    }

    public void ResetEditName()
    {
        EditName = Name;
        OnPropertyChanged(nameof(EditName));
    }

    private void OnPropertyChanged([CallerMemberName] string? propertyName = null)
    {
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
    }
}

public sealed class ChatTreeItem : INotifyPropertyChanged
{
    public ChatTreeItem(ProjectDto project, ChatDto chat)
    {
        Project = project;
        Chat = chat;
        EditTitle = chat.Title;
    }

    public ProjectDto Project { get; }

    public ChatDto Chat { get; private set; }

    public string Title => DisplayTitle(Chat.Title);

    public string EditTitle { get; set; }

    private bool _isEditing;
    private bool _isArchiving;
    private bool _showDropAfter;
    private bool _hasUnloadedHistory;
    private bool _isRunning;

    public bool IsEditing
    {
        get => _isEditing;
        set
        {
            if (_isEditing == value)
            {
                return;
            }
            _isEditing = value;
            OnPropertyChanged();
        }
    }

    public bool IsArchiving
    {
        get => _isArchiving;
        set
        {
            if (_isArchiving == value)
            {
                return;
            }
            _isArchiving = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(IsAvailable));
        }
    }

    public bool ShowDropAfter
    {
        get => _showDropAfter;
        set
        {
            if (_showDropAfter == value)
            {
                return;
            }
            _showDropAfter = value;
            OnPropertyChanged();
        }
    }

    public bool IsAvailable => !IsArchiving;

    public bool HasUnloadedHistory
    {
        get => _hasUnloadedHistory;
        set
        {
            if (_hasUnloadedHistory == value)
            {
                return;
            }
            _hasUnloadedHistory = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(ShowUnreadIndicator));
        }
    }

    public bool IsRunning
    {
        get => _isRunning;
        set
        {
            if (_isRunning == value)
            {
                return;
            }
            _isRunning = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(ShowUnreadIndicator));
        }
    }

    public bool ShowUnreadIndicator => HasUnloadedHistory && !IsRunning;

    public event PropertyChangedEventHandler? PropertyChanged;

    public void SetTitle(string title)
    {
        Chat = Chat with { Title = title };
        EditTitle = title;
        OnPropertyChanged(nameof(Title));
        OnPropertyChanged(nameof(EditTitle));
    }

    public void ResetEditTitle()
    {
        EditTitle = Title;
        OnPropertyChanged(nameof(EditTitle));
    }

    private void OnPropertyChanged([CallerMemberName] string? propertyName = null)
    {
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
    }

    private static string DisplayTitle(string? value)
    {
        var text = string.Join(" ", (value ?? "").Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
        if (string.IsNullOrWhiteSpace(text))
        {
            return "New Chat";
        }
        return text.Length > 80 ? text[..77].TrimEnd() + "..." : text;
    }
}
