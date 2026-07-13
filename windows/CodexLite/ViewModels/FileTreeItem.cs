using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Windows.Media;
using CodexLite.Models;

namespace CodexLite.ViewModels;

public sealed class FileTreeItem : INotifyPropertyChanged
{
    public FileTreeItem(FileEntryDto entry)
    {
        Entry = entry;
        if (IsDirectory)
        {
            Children.Add(CreatePlaceholder());
        }
    }

    private FileTreeItem()
    {
        Entry = new FileEntryDto("読み込み中...", "", "placeholder", null, "", null, "");
        IsPlaceholder = true;
    }

    public FileEntryDto Entry { get; }

    public string Name => Entry.Name;

    public string Path => Entry.Path;

    public string Kind => Entry.Kind;

    public string ViewerKind => Entry.ViewerKind;

    public bool IsDirectory => Entry.Kind == "directory";

    public string IconText => ViewerKind switch
    {
        "text" => "A",
        "markdown" => "MD",
        "pdf" => "PDF",
        "png" => "PNG",
        "jpeg" => "JPG",
        "image" => "IMG",
        "word" => "W",
        "excel" => "X",
        _ => ""
    };

    public double IconFontSize => ViewerKind is "markdown" or "pdf" or "png" or "jpeg" or "image" ? 5.3 : 8;

    public System.Windows.Media.Brush IconForeground => Brush(ViewerKind switch
    {
        "markdown" => "#8250DF",
        "pdf" => "#C53030",
        "png" or "jpeg" or "image" => "#2F855A",
        "word" => "#2B6CB0",
        "excel" => "#2F855A",
        "text" => "#0969DA",
        _ => "#6A737D"
    });

    public System.Windows.Media.Brush IconPaperFill => Brush(ViewerKind switch
    {
        "markdown" => "#F5F0FF",
        "pdf" => "#FFF5F5",
        "png" or "jpeg" or "image" => "#F0FFF4",
        "word" => "#EBF8FF",
        "excel" => "#F0FFF4",
        "text" => "#F6F8FA",
        _ => "White"
    });

    public System.Windows.Media.Brush IconPaperStroke => Brush(ViewerKind switch
    {
        "markdown" => "#8250DF",
        "pdf" => "#C53030",
        "png" or "jpeg" or "image" => "#2F855A",
        "word" => "#2B6CB0",
        "excel" => "#2F855A",
        _ => "#8C959F"
    });

    public System.Windows.Media.Brush IconDogEarFill => Brush(ViewerKind switch
    {
        "markdown" => "#E9D8FD",
        "pdf" => "#FED7D7",
        "png" or "jpeg" or "image" => "#C6F6D5",
        "word" => "#BEE3F8",
        "excel" => "#C6F6D5",
        _ => "#EDF2F7"
    });

    public bool IsPlaceholder { get; }

    public bool IsLoaded { get; private set; }

    public bool IsLoading { get; private set; }

    public ObservableCollection<FileTreeItem> Children { get; } = new();

    public event PropertyChangedEventHandler? PropertyChanged;

    public void BeginLoading()
    {
        IsLoading = true;
        OnPropertyChanged(nameof(IsLoading));
    }

    public void SetChildren(IEnumerable<FileEntryDto> entries)
    {
        Children.Clear();
        foreach (var entry in entries)
        {
            Children.Add(new FileTreeItem(entry));
        }
        IsLoaded = true;
        IsLoading = false;
        OnPropertyChanged(nameof(IsLoaded));
        OnPropertyChanged(nameof(IsLoading));
    }

    private static FileTreeItem CreatePlaceholder() => new();

    private static System.Windows.Media.Brush Brush(string color) =>
        (System.Windows.Media.Brush)new BrushConverter().ConvertFromString(color)!;

    private void OnPropertyChanged(string propertyName) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
}
