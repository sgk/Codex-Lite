using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Input;
using System.Windows.Threading;
using CodexLite.Models;
using KeyEventArgs = System.Windows.Input.KeyEventArgs;

namespace CodexLite;

public partial class MainWindow
{
    public static readonly DependencyProperty HistorySearchTermProperty = DependencyProperty.Register(
        nameof(HistorySearchTerm), typeof(string), typeof(MainWindow), new PropertyMetadata(""));

    public string HistorySearchTerm
    {
        get => (string)GetValue(HistorySearchTermProperty);
        set => SetValue(HistorySearchTermProperty, value);
    }

    private readonly List<MessageDto> _historySearchMatches = new();
    private int _historySearchIndex = -1;
    private CancellationTokenSource? _historySearchCancellation;

    private void HistorySearch_PreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (!ConversationTab.IsSelected || _selectedChat is null) return;
        if (e.Key == Key.F && Keyboard.Modifiers == ModifierKeys.Control)
        {
            e.Handled = true;
            HistorySearchPanel.Visibility = Visibility.Visible;
            HistorySearchBox.Focus();
            HistorySearchBox.SelectAll();
        }
        else if (HistorySearchPanel.Visibility == Visibility.Visible && e.Key == Key.Escape)
        {
            e.Handled = true;
            CloseHistorySearch();
            ActiveMessagesList.Focus();
        }
        else if (HistorySearchPanel.Visibility == Visibility.Visible && e.Key == Key.F3)
        {
            e.Handled = true;
            MoveHistorySearch(Keyboard.Modifiers.HasFlag(ModifierKeys.Shift) ? -1 : 1);
        }
    }

    private async void HistorySearchBox_PreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Enter || Keyboard.Modifiers != ModifierKeys.None) return;
        e.Handled = true;
        await SearchHistoryAsync();
    }

    private async void RunHistorySearch_Click(object sender, RoutedEventArgs e) => await SearchHistoryAsync();
    private void NextHistorySearch_Click(object sender, RoutedEventArgs e) => MoveHistorySearch(1);
    private void PreviousHistorySearch_Click(object sender, RoutedEventArgs e) => MoveHistorySearch(-1);
    private void CloseHistorySearch_Click(object sender, RoutedEventArgs e) => CloseHistorySearch();

    private void CloseHistorySearch()
    {
        _historySearchCancellation?.Cancel();
        _historySearchCancellation = null;
        _historySearchMatches.Clear();
        _historySearchIndex = -1;
        HistorySearchTerm = "";
        HistorySearchPanel.Visibility = Visibility.Collapsed;
        HistorySearchCountText.Text = "";
    }

    private async Task SearchHistoryAsync()
    {
        if (_selectedProject is not { } project || _selectedChat is not { } chat) return;
        _historySearchCancellation?.Cancel();
        using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(30));
        _historySearchCancellation = cancellation;
        var source = _messages;
        var query = HistorySearchBox.Text.Trim();
        _historySearchMatches.Clear();
        _historySearchIndex = -1;
        HistorySearchTerm = "";
        HistorySearchCountText.Text = query.Length == 0 ? "" : "検索中…";
        try
        {
            if (query.Length == 0) return;
            while (_isLoadingMessages || _isLoadingOlderMessages)
                await Task.Delay(100, cancellation.Token);
            var saved = await _client.ListMessagesAsync(project.Id, chat.Id, cancellation.Token)
                ?? throw new InvalidOperationException("チャット履歴を取得できませんでした。");
            if (cancellation.IsCancellationRequested || !ReferenceEquals(_messages, source)
                || _selectedChat?.Id != chat.Id || _selectedProject?.Id != project.Id) return;

            // Fetch once per explicit search, including unloaded history. WPF
            // virtualizes the display; compact-history filtering remains active.
            using (CollectionViewSource.GetDefaultView(ActiveMessagesList.ItemsSource).DeferRefresh())
                AppendNewMessages(saved);
            _hasMoreOlderMessages = false;
            _messageTotalCount = Math.Max(saved.Count, LoadedHistoryMessageCount());
            SetChatUnloadedHistoryIndicator(chat.Id, false);
            UpdateHistorySpacer();
            UpdateCurrentChatHistoryState();
            HistorySearchTerm = query;
            _historySearchMatches.AddRange(ActiveMessagesList.Items.OfType<MessageDto>()
                .Where(message => !message.IsInlineStatus && !message.IsSpacer && !message.IsActivityIndicator
                    && !message.IsWaitingMessage
                    && message.Content.Contains(query, StringComparison.OrdinalIgnoreCase)));
            if (_historySearchMatches.Count == 0)
                HistorySearchCountText.Text = "見つかりません";
            else
                MoveHistorySearch(1);
        }
        catch (OperationCanceledException)
        {
            if (ReferenceEquals(_historySearchCancellation, cancellation))
                HistorySearchCountText.Text = "検索がタイムアウトしました";
        }
        catch (Exception ex)
        {
            if (ReferenceEquals(_historySearchCancellation, cancellation))
                HistorySearchCountText.Text = $"検索エラー: {ShortError(ex)}";
        }
        finally
        {
            if (ReferenceEquals(_historySearchCancellation, cancellation))
                _historySearchCancellation = null;
        }
    }

    private void MoveHistorySearch(int direction)
    {
        if (_historySearchMatches.Count == 0) return;
        _historySearchIndex = (_historySearchIndex + direction + _historySearchMatches.Count) % _historySearchMatches.Count;
        var found = _historySearchMatches[_historySearchIndex];
        // Streaming may replace a message object while keeping its identity.
        var current = ActiveMessagesList.Items.OfType<MessageDto>().FirstOrDefault(message => message.Id == found.Id);
        if (current is null) return;
        _messageScrollInteractionVersion++;
        SetCurrentChatFollowNewMessages(false);
        ActiveMessagesList.SelectedItem = current;
        ActiveMessagesList.ScrollIntoView(current);
        var view = ActiveMessagesList;
        _ = Dispatcher.BeginInvoke(DispatcherPriority.Loaded, new Action(() =>
        {
            if (!ReferenceEquals(view, ActiveMessagesList) || view.SelectedItem is not MessageDto selected
                || selected.Id != current.Id || HistorySearchPanel.Visibility != Visibility.Visible) return;
            view.UpdateLayout();
            if (view.ItemContainerGenerator.ContainerFromItem(selected) is DependencyObject container)
                FindVisualChild<MarkdownViewer>(container, _ => true)?.BringFirstSearchMatchIntoView();
        }));
        HistorySearchCountText.Text = $"{_historySearchIndex + 1} / {_historySearchMatches.Count} メッセージ";
    }
}
