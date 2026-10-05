using System.Net;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using Brush = System.Windows.Media.Brush;
using Brushes = System.Windows.Media.Brushes;
using Color = System.Windows.Media.Color;
using FontFamily = System.Windows.Media.FontFamily;

namespace CodexLite;

public sealed class MarkdownViewer : FlowDocumentScrollViewer
{
    private const double ListIndentPerSourceColumn = 5;
    private const double UnorderedListMarkerWidth = 18;
    private const double OrderedListMarkerWidth = 24;
    private static readonly TimeSpan RenderInterval = TimeSpan.FromMilliseconds(150);
    private static readonly Regex MarkdownLinkRegex = new(
        @"\[([^\]\r\n]+)\]\((?:<([^>\r\n]+)>|([^)\s]+))\)",
        RegexOptions.Compiled);
    private static readonly Regex MarkdownImageRegex = new(
        @"!\[([^\]\r\n]*)\]\((?:<([^>\r\n]+)>|([^)\s]+))\)",
        RegexOptions.Compiled);
    private static readonly Regex HtmlLineBreakRegex = new(
        @"<br\s*/?>",
        RegexOptions.Compiled | RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
    private static readonly Regex HtmlUnderlineOpenRegex = new(
        @"<u\s*>",
        RegexOptions.Compiled | RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
    private static readonly Regex HtmlUnderlineCloseRegex = new(
        @"</u\s*>",
        RegexOptions.Compiled | RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
    private readonly DispatcherTimer _renderTimer;
    private string _pendingMarkdown = "";
    private DateTimeOffset _lastRenderAt = DateTimeOffset.MinValue;
    private bool _preserveNextMarkdownChange;
    private bool _preserveScrollPositionForPendingRender;
    private bool _hasPendingMarkdownRender;
    private readonly List<(TextRange Range, object Background)> _searchHighlights = new();

    public static readonly DependencyProperty SearchTextProperty = DependencyProperty.Register(
        nameof(SearchText), typeof(string), typeof(MarkdownViewer),
        new PropertyMetadata("", (owner, _) => ((MarkdownViewer)owner).UpdateSearchHighlights()));

    public string SearchText
    {
        get => (string)GetValue(SearchTextProperty);
        set => SetValue(SearchTextProperty, value);
    }

    public void BringFirstSearchMatchIntoView()
    {
        if (_searchHighlights.Count > 0
            && _searchHighlights[0].Range.Start.Parent is FrameworkContentElement element)
            element.BringIntoView();
    }

    private void UpdateSearchHighlights()
    {
        foreach (var highlight in _searchHighlights)
            highlight.Range.ApplyPropertyValue(TextElement.BackgroundProperty,
                highlight.Background is Brush brush ? brush : Brushes.Transparent);
        _searchHighlights.Clear();
        if (Document is null || string.IsNullOrWhiteSpace(SearchText)) return;

        // Join adjacent text runs so a term can cross bold/link formatting,
        // while retaining document pointers for accurate highlighting.
        var text = new StringBuilder();
        var runs = new List<(int Offset, string Text, TextPointer Start)>();
        TextPointer? position = Document.ContentStart;
        while (position is not null && position.CompareTo(Document.ContentEnd) < 0)
        {
            var context = position.GetPointerContext(LogicalDirection.Forward);
            if (context == TextPointerContext.Text)
            {
                var value = position.GetTextInRun(LogicalDirection.Forward);
                runs.Add((text.Length, value, position));
                text.Append(value);
            }
            else if (context == TextPointerContext.ElementEnd
                && position.Parent is Paragraph or LineBreak)
                text.Append('\n');
            position = position.GetNextContextPosition(LogicalDirection.Forward);
        }
        var content = text.ToString();
        for (var offset = 0; offset < content.Length;)
        {
            var match = content.IndexOf(SearchText, offset, StringComparison.OrdinalIgnoreCase);
            if (match < 0) break;
            var end = match + SearchText.Length;
            foreach (var run in runs.Where(run => run.Offset < end && match < run.Offset + run.Text.Length))
            {
                var startPointer = run.Start.GetPositionAtOffset(Math.Max(0, match - run.Offset));
                var endPointer = run.Start.GetPositionAtOffset(Math.Min(run.Text.Length, end - run.Offset));
                if (startPointer is not null && endPointer is not null)
                {
                    var range = new TextRange(startPointer, endPointer);
                    _searchHighlights.Add((range, range.GetPropertyValue(TextElement.BackgroundProperty)));
                }
            }
            offset = end;
        }
        // Formatting splits runs, so resolve every pointer before applying it.
        foreach (var highlight in _searchHighlights)
            highlight.Range.ApplyPropertyValue(TextElement.BackgroundProperty, Brushes.Yellow);
    }

    public static readonly DependencyProperty MarkdownProperty =
        DependencyProperty.Register(
            nameof(Markdown),
            typeof(string),
            typeof(MarkdownViewer),
            new PropertyMetadata("", OnMarkdownChanged));

    public static readonly DependencyProperty BubbleMouseWheelProperty =
        DependencyProperty.Register(
            nameof(BubbleMouseWheel),
            typeof(bool),
            typeof(MarkdownViewer),
            new PropertyMetadata(true));

    public MarkdownViewer()
    {
        IsToolBarVisible = false;
        Document = CreateDocument("");
        _renderTimer = new DispatcherTimer { Interval = RenderInterval };
        _renderTimer.Tick += (_, _) => RenderPendingMarkdown();
        Loaded += (_, _) =>
        {
            if (_hasPendingMarkdownRender) RenderPendingMarkdown();
        };
        Unloaded += (_, _) => _renderTimer.Stop();
        PreviewMouseWheel += MarkdownViewer_PreviewMouseWheel;
    }

    public event EventHandler<MarkdownLinkClickedEventArgs>? LinkClicked;

    public event EventHandler<MarkdownImageRequestedEventArgs>? ImageRequested;

    public string Markdown
    {
        get => (string)GetValue(MarkdownProperty);
        set => SetValue(MarkdownProperty, value);
    }

    public bool BubbleMouseWheel
    {
        get => (bool)GetValue(BubbleMouseWheelProperty);
        set => SetValue(BubbleMouseWheelProperty, value);
    }

    private static void OnMarkdownChanged(DependencyObject dependencyObject, DependencyPropertyChangedEventArgs e)
    {
        if (dependencyObject is MarkdownViewer viewer)
        {
            viewer.ScheduleMarkdownRender(e.NewValue as string ?? "", viewer._preserveNextMarkdownChange);
            viewer._preserveNextMarkdownChange = false;
        }
    }

    public void UpdateMarkdownPreservingScroll(string markdown)
    {
        _preserveNextMarkdownChange = true;
        SetCurrentValue(MarkdownProperty, markdown);
        _preserveNextMarkdownChange = false;
    }

    private void ScheduleMarkdownRender(string markdown, bool preserveScrollPosition)
    {
        _pendingMarkdown = markdown;
        _preserveScrollPositionForPendingRender = preserveScrollPosition;
        _hasPendingMarkdownRender = true;
        // XAML can set Markdown before wiring ImageRequested. Rendering at
        // that point permanently turns otherwise valid images into links.
        // Loaded runs after the template and its event handlers are attached.
        if (!IsLoaded) return;
        var elapsed = DateTimeOffset.Now - _lastRenderAt;
        if (elapsed >= RenderInterval)
        {
            RenderPendingMarkdown();
            return;
        }

        _renderTimer.Interval = RenderInterval - elapsed;
        if (!_renderTimer.IsEnabled)
        {
            _renderTimer.Start();
        }
    }

    private void RenderPendingMarkdown()
    {
        _renderTimer.Stop();
        if (!IsLoaded || !_hasPendingMarkdownRender) return;
        _hasPendingMarkdownRender = false;
        var verticalOffset = _preserveScrollPositionForPendingRender
            ? FindScrollViewer(this)?.VerticalOffset
            : null;
        _preserveScrollPositionForPendingRender = false;
        Document = CreateDocument(_pendingMarkdown);
        _searchHighlights.Clear();
        UpdateSearchHighlights();
        _lastRenderAt = DateTimeOffset.Now;
        if (verticalOffset is { } offset)
        {
            _ = Dispatcher.BeginInvoke(DispatcherPriority.Loaded, new Action(() =>
            {
                UpdateLayout();
                FindScrollViewer(this)?.ScrollToVerticalOffset(offset);
            }));
        }
    }

    private static ScrollViewer? FindScrollViewer(DependencyObject root)
    {
        if (root is ScrollViewer scrollViewer)
        {
            return scrollViewer;
        }
        for (var index = 0; index < VisualTreeHelper.GetChildrenCount(root); index++)
        {
            var found = FindScrollViewer(VisualTreeHelper.GetChild(root, index));
            if (found is not null)
            {
                return found;
            }
        }
        return null;
    }

    private void MarkdownViewer_PreviewMouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (!BubbleMouseWheel)
        {
            return;
        }

        e.Handled = true;
        var parentEvent = new MouseWheelEventArgs(e.MouseDevice, e.Timestamp, e.Delta)
        {
            RoutedEvent = UIElement.MouseWheelEvent,
            Source = this
        };
        RaiseEvent(parentEvent);
    }

    private FlowDocument CreateDocument(string markdown)
    {
        var document = new FlowDocument
        {
            PagePadding = new Thickness(0),
            FontFamily = FontFamily,
            FontSize = FontSize,
            Foreground = Foreground,
            LineHeight = 20,
            LineStackingStrategy = LineStackingStrategy.BlockLineHeight
        };

        var lines = markdown.Replace("\r\n", "\n").Replace('\r', '\n').Split('\n');
        var paragraph = new StringBuilder();
        var code = new StringBuilder();
        var inCode = false;

        for (var lineIndex = 0; lineIndex < lines.Length; lineIndex++)
        {
            var rawLine = lines[lineIndex];
            var line = rawLine.TrimEnd();
            if (line.StartsWith("```", StringComparison.Ordinal))
            {
                FlushParagraph(document, paragraph);
                if (inCode)
                {
                    AddCodeBlock(document, code.ToString().TrimEnd('\n'));
                    code.Clear();
                    inCode = false;
                }
                else
                {
                    inCode = true;
                }
                continue;
            }

            if (inCode)
            {
                code.AppendLine(rawLine);
                continue;
            }

            if (TryAddBlockQuote(document, lines, ref lineIndex, paragraph))
            {
                continue;
            }

            if (string.IsNullOrWhiteSpace(line))
            {
                FlushParagraph(document, paragraph, addFollowingLineGap: true);
                continue;
            }

            if (TryAddTable(document, lines, ref lineIndex, paragraph))
            {
                continue;
            }

            if (TryAddSpecialBlock(document, line, paragraph))
            {
                continue;
            }

            if (paragraph.Length > 0)
            {
                paragraph.Append('\n');
            }
            paragraph.Append(line);
        }

        if (inCode)
        {
            AddCodeBlock(document, code.ToString().TrimEnd('\n'));
        }
        FlushParagraph(document, paragraph);
        return document;
    }

    private bool TryAddBlockQuote(FlowDocument document, string[] lines, ref int lineIndex, StringBuilder paragraph)
    {
        if (!TryExtractBlockQuoteContent(lines[lineIndex].TrimEnd(), out _))
        {
            return false;
        }

        FlushParagraph(document, paragraph);
        var quotedLines = new List<string>();
        var nextIndex = lineIndex;
        while (nextIndex < lines.Length &&
               TryExtractBlockQuoteContent(lines[nextIndex].TrimEnd(), out var content))
        {
            quotedLines.Add(content);
            nextIndex++;
        }
        AddBlockQuote(document, quotedLines);
        lineIndex = nextIndex - 1;
        return true;
    }

    private static bool TryExtractBlockQuoteContent(string line, out string content)
    {
        content = "";
        var trimmed = line.TrimStart();
        if (trimmed.Length == 0 || trimmed[0] != '>')
        {
            return false;
        }
        var contentStart = trimmed.Length > 1 && (trimmed[1] == ' ' || trimmed[1] == '\t') ? 2 : 1;
        content = trimmed[contentStart..];
        return true;
    }

    private void AddBlockQuote(FlowDocument document, IReadOnlyList<string> lines)
    {
        var section = new Section
        {
            BorderBrush = new SolidColorBrush(Color.FromRgb(152, 162, 179)),
            BorderThickness = new Thickness(3, 0, 0, 0),
            Foreground = new SolidColorBrush(Color.FromRgb(102, 112, 133)),
            Margin = new Thickness(0, 4, 0, 6),
            Padding = new Thickness(10, 0, 0, 0),
        };
        var paragraphText = new StringBuilder();
        foreach (var line in lines)
        {
            if (string.IsNullOrWhiteSpace(line))
            {
                FlushBlockQuoteParagraph(section, paragraphText);
                continue;
            }
            if (paragraphText.Length > 0)
            {
                paragraphText.Append('\n');
            }
            paragraphText.Append(line);
        }
        FlushBlockQuoteParagraph(section, paragraphText);
        if (section.Blocks.Count == 0)
        {
            section.Blocks.Add(new Paragraph { Margin = new Thickness(0), LineHeight = 20 });
        }
        document.Blocks.Add(section);
    }

    private void FlushBlockQuoteParagraph(Section section, StringBuilder paragraphText)
    {
        if (paragraphText.Length == 0)
        {
            return;
        }
        var paragraph = new Paragraph
        {
            Margin = new Thickness(0, section.Blocks.Count == 0 ? 0 : 8, 0, 0),
            LineHeight = 20,
            LineStackingStrategy = LineStackingStrategy.BlockLineHeight,
        };
        AddInlineRuns(paragraph, paragraphText.ToString());
        section.Blocks.Add(paragraph);
        paragraphText.Clear();
    }

    private bool TryAddTable(FlowDocument document, string[] lines, ref int lineIndex, StringBuilder paragraph)
    {
        if (lineIndex + 1 >= lines.Length)
        {
            return false;
        }

        var headerLine = lines[lineIndex].Trim();
        var separatorLine = lines[lineIndex + 1].Trim();
        if (!LooksLikeTableRow(headerLine) || !IsTableSeparatorRow(separatorLine))
        {
            return false;
        }

        var headers = SplitTableRow(headerLine);
        var separators = SplitTableRow(separatorLine);
        if (headers.Count == 0 || separators.Count < headers.Count || !separators.Take(headers.Count).All(IsTableSeparatorCell))
        {
            return false;
        }

        var rows = new List<List<string>>();
        var nextIndex = lineIndex + 2;
        while (nextIndex < lines.Length)
        {
            var rowLine = lines[nextIndex].Trim();
            if (!LooksLikeTableRow(rowLine))
            {
                break;
            }
            rows.Add(SplitTableRow(rowLine));
            nextIndex++;
        }

        FlushParagraph(document, paragraph);
        AddTable(document, headers, rows);
        lineIndex = nextIndex - 1;
        return true;
    }

    private static bool LooksLikeTableRow(string line)
    {
        return !string.IsNullOrWhiteSpace(line) && line.Contains('|', StringComparison.Ordinal);
    }

    private static bool IsTableSeparatorRow(string line)
    {
        if (!LooksLikeTableRow(line))
        {
            return false;
        }
        var cells = SplitTableRow(line);
        return cells.Count > 0 && cells.All(IsTableSeparatorCell);
    }

    private static bool IsTableSeparatorCell(string cell)
    {
        var value = cell.Trim();
        if (value.Length < 3)
        {
            return false;
        }
        if (value.StartsWith(':'))
        {
            value = value[1..];
        }
        if (value.EndsWith(':'))
        {
            value = value[..^1];
        }
        return value.Length >= 3 && value.All(ch => ch == '-');
    }

    private static List<string> SplitTableRow(string line)
    {
        var trimmed = line.Trim();
        if (trimmed.StartsWith('|'))
        {
            trimmed = trimmed[1..];
        }
        if (trimmed.EndsWith('|'))
        {
            trimmed = trimmed[..^1];
        }
        return trimmed
            .Split('|')
            .Select(cell => cell.Trim())
            .ToList();
    }

    private bool TryAddSpecialBlock(FlowDocument document, string line, StringBuilder paragraph)
    {
        var trimmed = line.TrimStart();
        var headingLevel = HeadingLevel(trimmed);
        if (headingLevel > 0)
        {
            FlushParagraph(document, paragraph);
            AddParagraph(document, trimmed[headingLevel..].Trim(), fontSize: headingLevel == 1 ? 18 : 15, fontWeight: FontWeights.SemiBold, margin: new Thickness(0, 6, 0, 4));
            return true;
        }

        if (trimmed.StartsWith("- ", StringComparison.Ordinal) ||
            trimmed.StartsWith("* ", StringComparison.Ordinal) ||
            trimmed.StartsWith("+ ", StringComparison.Ordinal))
        {
            FlushParagraph(document, paragraph);
            var left = 10 + LeadingIndentColumns(line) * ListIndentPerSourceColumn + UnorderedListMarkerWidth;
            AddParagraph(
                document,
                "• " + trimmed[2..].Trim(),
                margin: new Thickness(left, 1, 0, 1),
                textIndent: -UnorderedListMarkerWidth);
            return true;
        }

        var numberPrefixLength = NumberedListPrefixLength(trimmed);
        if (numberPrefixLength > 0)
        {
            FlushParagraph(document, paragraph);
            var markerWidth = Math.Max(OrderedListMarkerWidth, numberPrefixLength * 7);
            var left = 10 + LeadingIndentColumns(line) * ListIndentPerSourceColumn + markerWidth;
            AddParagraph(
                document,
                trimmed,
                margin: new Thickness(left, 1, 0, 1),
                textIndent: -markerWidth);
            return true;
        }

        return false;
    }

    private static int HeadingLevel(string line)
    {
        var count = 0;
        while (count < line.Length && count < 6 && line[count] == '#')
        {
            count++;
        }
        return count > 0 && count < line.Length && line[count] == ' ' ? count : 0;
    }

    private static int NumberedListPrefixLength(string line)
    {
        var index = 0;
        while (index < line.Length && char.IsDigit(line[index]))
        {
            index++;
        }
        return index > 0 && index + 1 < line.Length && line[index] == '.' && line[index + 1] == ' ' ? index + 2 : 0;
    }

    private static int LeadingIndentColumns(string line)
    {
        var columns = 0;
        foreach (var character in line)
        {
            if (character == ' ')
            {
                columns++;
                continue;
            }
            if (character == '\t')
            {
                columns += 4 - columns % 4;
                continue;
            }
            break;
        }
        return columns;
    }

    private void FlushParagraph(FlowDocument document, StringBuilder paragraph, bool addFollowingLineGap = false)
    {
        if (paragraph.Length == 0)
        {
            return;
        }
        AddParagraph(
            document,
            paragraph.ToString(),
            margin: addFollowingLineGap ? new Thickness(0, 1, 0, 21) : null);
        paragraph.Clear();
    }

    private void AddParagraph(
        FlowDocument document,
        string text,
        double? fontSize = null,
        FontWeight? fontWeight = null,
        Brush? foreground = null,
        Thickness? margin = null,
        double? textIndent = null)
    {
        var paragraph = new Paragraph
        {
            Margin = margin ?? new Thickness(0, 1, 0, 1),
            LineHeight = 20,
            LineStackingStrategy = LineStackingStrategy.BlockLineHeight
        };
        if (textIndent is not null)
        {
            paragraph.TextIndent = textIndent.Value;
        }
        if (fontSize is not null)
        {
            paragraph.FontSize = fontSize.Value;
        }
        if (fontWeight is not null)
        {
            paragraph.FontWeight = fontWeight.Value;
        }
        if (foreground is not null)
        {
            paragraph.Foreground = foreground;
        }
        AddInlineRuns(paragraph, text);
        document.Blocks.Add(paragraph);
    }

    private void AddTable(FlowDocument document, IReadOnlyList<string> headers, IReadOnlyList<List<string>> rows)
    {
        var columnCount = headers.Count;
        var table = new Table
        {
            CellSpacing = 0,
            Margin = new Thickness(0, 4, 0, 4)
        };
        for (var index = 0; index < columnCount; index++)
        {
            table.Columns.Add(new TableColumn());
        }

        var group = new TableRowGroup();
        var headerRow = new TableRow();
        foreach (var header in headers)
        {
            headerRow.Cells.Add(CreateTableCell(header, isHeader: true));
        }
        group.Rows.Add(headerRow);

        foreach (var row in rows)
        {
            var tableRow = new TableRow();
            for (var column = 0; column < columnCount; column++)
            {
                var text = column < row.Count ? row[column] : "";
                tableRow.Cells.Add(CreateTableCell(text, isHeader: false));
            }
            group.Rows.Add(tableRow);
        }

        table.RowGroups.Add(group);
        document.Blocks.Add(table);
    }

    private TableCell CreateTableCell(string text, bool isHeader)
    {
        var paragraph = new Paragraph
        {
            Margin = new Thickness(0),
            LineHeight = 18,
            LineStackingStrategy = LineStackingStrategy.BlockLineHeight
        };
        AddInlineRuns(paragraph, text);
        return new TableCell(paragraph)
        {
            Padding = new Thickness(6, 4, 6, 4),
            BorderBrush = new SolidColorBrush(Color.FromRgb(208, 215, 222)),
            BorderThickness = new Thickness(1),
            Background = isHeader ? new SolidColorBrush(Color.FromRgb(246, 248, 250)) : Brushes.Transparent,
            FontWeight = isHeader ? FontWeights.SemiBold : FontWeights.Normal
        };
    }

    private static void AddCodeBlock(FlowDocument document, string text)
    {
        var normalIconBrush = new SolidColorBrush(Color.FromRgb(87, 96, 106));
        var copyIcon = new TextBlock
        {
            Text = "\uE8C8",
            FontFamily = new FontFamily("Segoe MDL2 Assets"),
            FontSize = 14,
            Foreground = normalIconBrush,
            HorizontalAlignment = System.Windows.HorizontalAlignment.Center,
            VerticalAlignment = System.Windows.VerticalAlignment.Center
        };
        var copyButton = new System.Windows.Controls.Button
        {
            Content = copyIcon,
            ToolTip = "コードをクリップボードへコピー",
            Margin = new Thickness(0, 0, 0, 4),
            HorizontalAlignment = System.Windows.HorizontalAlignment.Right,
            IsTabStop = false
        };
        copyButton.SetResourceReference(FrameworkElement.StyleProperty, "MarkdownCopyButton");
        System.Windows.Automation.AutomationProperties.SetName(copyButton, "コードをコピー");
        copyButton.Click += async (_, _) =>
        {
            try
            {
                System.Windows.Clipboard.SetText(text);
                copyIcon.Foreground = new SolidColorBrush(Color.FromRgb(31, 136, 61));
                copyButton.ToolTip = "コピー済み";
                await Task.Delay(1200);
            }
            catch
            {
                copyIcon.Foreground = new SolidColorBrush(Color.FromRgb(207, 34, 46));
                copyButton.ToolTip = "コピー失敗";
                await Task.Delay(1200);
            }
            copyIcon.Foreground = normalIconBrush;
            copyButton.ToolTip = "コードをクリップボードへコピー";
        };

        var content = new StackPanel();
        content.Children.Add(copyButton);
        content.Children.Add(new System.Windows.Controls.TextBox
        {
            Text = text,
            TextWrapping = TextWrapping.Wrap,
            FontFamily = new FontFamily("Consolas"),
            Foreground = new SolidColorBrush(Color.FromRgb(9, 64, 116)),
            Background = Brushes.Transparent,
            BorderThickness = new Thickness(0),
            Padding = new Thickness(0),
            IsReadOnly = true,
            AcceptsReturn = true,
            IsTabStop = false,
            VerticalScrollBarVisibility = ScrollBarVisibility.Disabled,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled
        });

        var border = new Border
        {
            Background = new SolidColorBrush(Color.FromRgb(246, 248, 250)),
            BorderBrush = new SolidColorBrush(Color.FromRgb(208, 215, 222)),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(2),
            Margin = new Thickness(0, 4, 0, 4),
            Padding = new Thickness(6),
            Child = content
        };
        document.Blocks.Add(new BlockUIContainer(border)
        {
            Margin = new Thickness(0, 4, 0, 4)
        });
    }

    private static Run InlineCodeRun(string text)
    {
        return new Run(text)
        {
            FontFamily = new FontFamily("Consolas"),
            FontSize = 12,
            Foreground = new SolidColorBrush(Color.FromRgb(9, 64, 116)),
            Background = new SolidColorBrush(Color.FromRgb(234, 242, 255))
        };
    }

    private static Run TextRun(string text) => new(WebUtility.HtmlDecode(text));

    private static void AddDecodedTextWithInlineCode(InlineCollection inlines, string text)
    {
        var index = 0;
        while (index < text.Length)
        {
            var codeStart = text.IndexOf('`', index);
            var lineBreakMatch = HtmlLineBreakRegex.Match(text, index);
            var lineBreakStart = lineBreakMatch.Success ? lineBreakMatch.Index : -1;
            var underlineMatch = HtmlUnderlineOpenRegex.Match(text, index);
            var underlineStart = underlineMatch.Success ? underlineMatch.Index : -1;
            var boldStart = text.IndexOf("**", index, StringComparison.Ordinal);
            var strikethroughStart = text.IndexOf("~~", index, StringComparison.Ordinal);
            var next = NextIndex(codeStart, lineBreakStart, underlineStart, boldStart, strikethroughStart);
            if (next < 0)
            {
                inlines.Add(TextRun(text[index..]));
                return;
            }
            if (next > index)
            {
                inlines.Add(TextRun(text[index..next]));
            }
            if (next == lineBreakStart)
            {
                inlines.Add(new LineBreak());
                index = lineBreakMatch.Index + lineBreakMatch.Length;
                continue;
            }
            if (next == underlineStart)
            {
                var underlineContentStart = underlineMatch.Index + underlineMatch.Length;
                var underlineEnd = HtmlUnderlineCloseRegex.Match(text, underlineContentStart);
                if (!underlineEnd.Success)
                {
                    inlines.Add(TextRun(text[underlineMatch.Index..]));
                    return;
                }
                var underline = new Span { TextDecorations = TextDecorations.Underline };
                AddDecodedTextWithInlineCode(underline.Inlines, text[underlineContentStart..underlineEnd.Index]);
                inlines.Add(underline);
                index = underlineEnd.Index + underlineEnd.Length;
                continue;
            }
            if (next == boldStart || next == strikethroughStart)
            {
                var marker = next == boldStart ? "**" : "~~";
                var decorationEnd = text.IndexOf(marker, next + marker.Length, StringComparison.Ordinal);
                if (decorationEnd < 0)
                {
                    inlines.Add(TextRun(text[next..]));
                    return;
                }
                Span decoration = next == boldStart
                    ? new Bold()
                    : new Span { TextDecorations = TextDecorations.Strikethrough };
                AddDecodedTextWithInlineCode(decoration.Inlines, text[(next + marker.Length)..decorationEnd]);
                inlines.Add(decoration);
                index = decorationEnd + marker.Length;
                continue;
            }
            var codeEnd = text.IndexOf('`', codeStart + 1);
            if (codeEnd < 0)
            {
                inlines.Add(TextRun(text[codeStart..]));
                return;
            }
            inlines.Add(InlineCodeRun(text[(codeStart + 1)..codeEnd]));
            index = codeEnd + 1;
        }
    }

    private void AddInlineRuns(Paragraph paragraph, string text)
    {
        var lines = text.Replace("\r\n", "\n").Replace('\r', '\n').Split('\n');
        for (var index = 0; index < lines.Length; index++)
        {
            if (index > 0)
            {
                paragraph.Inlines.Add(new LineBreak());
            }
            AddInlineRunsWithoutLineBreaks(paragraph, lines[index]);
        }
    }

    private void AddInlineRunsWithoutLineBreaks(Paragraph paragraph, string text)
    {
        var index = 0;
        while (index < text.Length)
        {
            var codeStart = text.IndexOf('`', index);
            var boldStart = text.IndexOf("**", index, StringComparison.Ordinal);
            var strikethroughStart = text.IndexOf("~~", index, StringComparison.Ordinal);
            var imageMatch = MarkdownImageRegex.Match(text, index);
            var imageStart = imageMatch.Success ? imageMatch.Index : -1;
            var linkMatch = MarkdownLinkRegex.Match(text, index);
            var linkStart = linkMatch.Success ? linkMatch.Index : -1;
            var lineBreakMatch = HtmlLineBreakRegex.Match(text, index);
            var lineBreakStart = lineBreakMatch.Success ? lineBreakMatch.Index : -1;
            var underlineMatch = HtmlUnderlineOpenRegex.Match(text, index);
            var underlineStart = underlineMatch.Success ? underlineMatch.Index : -1;
            if (imageStart >= 0 && linkStart == imageStart + 1)
            {
                linkStart = -1;
            }
            var next = NextIndex(codeStart, boldStart, strikethroughStart, imageStart, linkStart, lineBreakStart, underlineStart);
            if (next < 0)
            {
                paragraph.Inlines.Add(TextRun(text[index..]));
                return;
            }
            if (next > index)
            {
                paragraph.Inlines.Add(TextRun(text[index..next]));
            }
            if (next == codeStart)
            {
                var end = text.IndexOf('`', next + 1);
                if (end < 0)
                {
                    paragraph.Inlines.Add(TextRun(text[next..]));
                    return;
                }
                paragraph.Inlines.Add(InlineCodeRun(text[(next + 1)..end]));
                index = end + 1;
                continue;
            }
            if (next == lineBreakStart)
            {
                paragraph.Inlines.Add(new LineBreak());
                index = lineBreakMatch.Index + lineBreakMatch.Length;
                continue;
            }
            if (next == underlineStart)
            {
                var underlineContentStart = underlineMatch.Index + underlineMatch.Length;
                var underlineEnd = HtmlUnderlineCloseRegex.Match(text, underlineContentStart);
                if (!underlineEnd.Success)
                {
                    paragraph.Inlines.Add(TextRun(text[underlineMatch.Index..]));
                    return;
                }
                var underline = new Span { TextDecorations = TextDecorations.Underline };
                AddDecodedTextWithInlineCode(underline.Inlines, text[underlineContentStart..underlineEnd.Index]);
                paragraph.Inlines.Add(underline);
                index = underlineEnd.Index + underlineEnd.Length;
                continue;
            }
            if (next == imageStart)
            {
                var targetGroup = imageMatch.Groups[2].Success
                    ? imageMatch.Groups[2]
                    : imageMatch.Groups[3];
                AddImage(paragraph, WebUtility.HtmlDecode(imageMatch.Groups[1].Value), targetGroup.Value);
                index = imageMatch.Index + imageMatch.Length;
                continue;
            }
            if (next == linkStart)
            {
                var targetGroup = linkMatch.Groups[2].Success
                    ? linkMatch.Groups[2]
                    : linkMatch.Groups[3];
                AddHyperlink(paragraph, linkMatch.Groups[1].Value, targetGroup.Value);
                index = linkMatch.Index + linkMatch.Length;
                continue;
            }
            if (next == strikethroughStart)
            {
                var strikethroughEnd = text.IndexOf("~~", next + 2, StringComparison.Ordinal);
                if (strikethroughEnd < 0)
                {
                    paragraph.Inlines.Add(TextRun(text[next..]));
                    return;
                }
                var strikethrough = new Span
                {
                    TextDecorations = TextDecorations.Strikethrough
                };
                AddDecodedTextWithInlineCode(strikethrough.Inlines, text[(next + 2)..strikethroughEnd]);
                paragraph.Inlines.Add(strikethrough);
                index = strikethroughEnd + 2;
                continue;
            }
            var boldEnd = text.IndexOf("**", next + 2, StringComparison.Ordinal);
            if (boldEnd < 0)
            {
                paragraph.Inlines.Add(TextRun(text[next..]));
                return;
            }
            var bold = new Bold();
            AddDecodedTextWithInlineCode(bold.Inlines, text[(next + 2)..boldEnd]);
            paragraph.Inlines.Add(bold);
            index = boldEnd + 2;
        }
    }

    private void AddImage(Paragraph paragraph, string alternativeText, string target)
    {
        var request = new MarkdownImageRequestedEventArgs(target, alternativeText);
        ImageRequested?.Invoke(this, request);
        if (request.Source is null && request.SourceTask is null)
        {
            var fallbackText = string.IsNullOrWhiteSpace(alternativeText) ? "画像" : alternativeText;
            AddHyperlink(paragraph, $"🖼 {fallbackText}", request.LinkTarget);
            return;
        }

        paragraph.LineStackingStrategy = LineStackingStrategy.MaxHeight;
        var image = new System.Windows.Controls.Image
        {
            Source = request.Source,
            Stretch = Stretch.Uniform,
            MaxWidth = 720,
            MaxHeight = 600,
            Margin = new Thickness(0, 4, 0, 4),
            ToolTip = string.IsNullOrWhiteSpace(alternativeText) ? target : alternativeText,
            Visibility = request.Source is null ? Visibility.Collapsed : Visibility.Visible
        };
        image.MouseLeftButtonUp += (_, _) => LinkClicked?.Invoke(this, new MarkdownLinkClickedEventArgs(request.LinkTarget));
        image.Cursor = System.Windows.Input.Cursors.Hand;
        var fallback = CreateImageFallback(alternativeText, request.LinkTarget, request.SourceTask is null ? "画像を表示できません" : "画像を読み込み中…");
        fallback.Visibility = request.Source is null ? Visibility.Visible : Visibility.Collapsed;
        var panel = new StackPanel();
        panel.Children.Add(image);
        panel.Children.Add(fallback);
        paragraph.Inlines.Add(new InlineUIContainer(panel)
        {
            BaselineAlignment = BaselineAlignment.Center
        });
        if (request.SourceTask is not null)
        {
            CompleteImageLoadAsync(image, fallback, request.SourceTask);
        }
    }

    private TextBlock CreateImageFallback(string alternativeText, string target, string suffix)
    {
        var fallback = new TextBlock { TextWrapping = TextWrapping.Wrap };
        var label = string.IsNullOrWhiteSpace(alternativeText) ? "画像" : alternativeText;
        var hyperlink = new Hyperlink(new Run($"🖼 {label}"))
        {
            Cursor = System.Windows.Input.Cursors.Hand,
            Foreground = new SolidColorBrush(Color.FromRgb(9, 105, 218)),
            TextDecorations = TextDecorations.Underline,
            ToolTip = target
        };
        hyperlink.Click += (_, _) => LinkClicked?.Invoke(this, new MarkdownLinkClickedEventArgs(target));
        fallback.Inlines.Add(hyperlink);
        fallback.Inlines.Add(new Run($" — {suffix}") { Foreground = Brushes.DimGray });
        return fallback;
    }

    private static async void CompleteImageLoadAsync(System.Windows.Controls.Image image, TextBlock fallback, Task<ImageSource?> sourceTask)
    {
        try
        {
            var source = await sourceTask;
            if (source is not null)
            {
                image.Source = source;
                image.Visibility = Visibility.Visible;
                fallback.Visibility = Visibility.Collapsed;
                return;
            }
        }
        catch
        {
        }
        image.Visibility = Visibility.Collapsed;
        fallback.Visibility = Visibility.Visible;
        if (fallback.Inlines.LastInline is Run suffix)
        {
            suffix.Text = " — 画像を表示できません";
        }
    }

    private void AddHyperlink(Paragraph paragraph, string text, string target)
    {
        var hyperlink = new Hyperlink
        {
            Cursor = System.Windows.Input.Cursors.Hand,
            Foreground = new SolidColorBrush(Color.FromRgb(9, 105, 218)),
            TextDecorations = TextDecorations.Underline,
            ToolTip = target
        };
        AddDecodedTextWithInlineCode(hyperlink.Inlines, text);
        hyperlink.Click += (_, _) => LinkClicked?.Invoke(this, new MarkdownLinkClickedEventArgs(target));
        paragraph.Inlines.Add(hyperlink);
    }

    private static int NextIndex(params int[] indexes)
    {
        return indexes.Where(index => index >= 0).DefaultIfEmpty(-1).Min();
    }
}

public sealed class MarkdownLinkClickedEventArgs(string target) : EventArgs
{
    public string Target { get; } = target;
}

public sealed class MarkdownImageRequestedEventArgs(string target, string alternativeText) : EventArgs
{
    public string Target { get; } = target;

    public string AlternativeText { get; } = alternativeText;

    public ImageSource? Source { get; set; }

    public Task<ImageSource?>? SourceTask { get; set; }

    public string LinkTarget { get; set; } = target;
}
