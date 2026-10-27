using System.ComponentModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using CodexLite.Models;

namespace CodexLite;

public partial class ProjectHistoryImportDialog : Window
{
    private readonly List<ProjectImportChoice> _choices;
    private ICollectionView? _view;

    public ProjectHistoryImportDialog(IEnumerable<ProjectCandidateDto> candidates)
    {
        _choices = candidates.Select(candidate => new ProjectImportChoice(
            candidate.Path, candidate.Name, candidate.ThreadCount, candidate.LastUsedAt)).ToList();
        InitializeComponent();
        _view = CollectionViewSource.GetDefaultView(_choices);
        _view.Filter = item => item is ProjectImportChoice choice &&
            (choice.Name.Contains(FilterBox.Text.Trim(), StringComparison.OrdinalIgnoreCase) ||
             choice.Path.Contains(FilterBox.Text.Trim(), StringComparison.OrdinalIgnoreCase));
        CandidatesGrid.ItemsSource = _view;
        foreach (var choice in _choices) choice.PropertyChanged += Choice_PropertyChanged;
        UpdateSelectionSummary();
    }

    public IReadOnlyList<string> SelectedPaths => _choices.Where(choice => choice.IsSelected).Select(choice => choice.Path).ToArray();

    private void Choice_PropertyChanged(object? sender, PropertyChangedEventArgs e) => UpdateSelectionSummary();

    private void UpdateSelectionSummary()
    {
        var selected = _choices.Where(choice => choice.IsSelected).ToList();
        SelectionSummary.Text = _choices.Count == 0 ? "読み込み可能な未登録の履歴はありません。" :
            $"選択 {selected.Count} / {_choices.Count} プロジェクト・{selected.Sum(choice => choice.ThreadCount)} 会話（絞り込みで非表示の選択も含む）";
        ImportButton.IsEnabled = selected.Count > 0;
    }

    private void FilterBox_TextChanged(object sender, TextChangedEventArgs e) => _view?.Refresh();
    private void SelectVisible_Click(object sender, RoutedEventArgs e)
    {
        if (_view is null) return;
        foreach (ProjectImportChoice choice in _view) choice.IsSelected = true;
    }
    private void ClearSelection_Click(object sender, RoutedEventArgs e)
    {
        foreach (var choice in _choices) choice.IsSelected = false;
    }
    private void Import_Click(object sender, RoutedEventArgs e)
    {
        if (SelectedPaths.Count > 0) DialogResult = true;
    }
}
