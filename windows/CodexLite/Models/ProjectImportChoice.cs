using System.ComponentModel;

namespace CodexLite.Models;

public sealed class ProjectImportChoice(string path, string name, int threadCount, string? lastUsedAt) : INotifyPropertyChanged
{
    public string Path { get; } = path;
    public string Name { get; } = name;
    public int ThreadCount { get; } = threadCount;
    public string LastUsed { get; } = DateTimeOffset.TryParse(lastUsedAt, out var date)
        ? date.ToLocalTime().ToString("yyyy/MM/dd HH:mm") : "—";
    private bool _isSelected;
    public bool IsSelected
    {
        get => _isSelected;
        set
        {
            if (_isSelected == value) return;
            _isSelected = value;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(IsSelected)));
        }
    }
    public event PropertyChangedEventHandler? PropertyChanged;
}
