using System;
using System.Globalization;
using System.Windows;
using System.Windows.Media;
using System.Windows.Threading;
using Color = System.Windows.Media.Color;
using Pen = System.Windows.Media.Pen;
using Point = System.Windows.Point;

namespace CodexLite;

public sealed class UnloadedHistorySpacer : FrameworkElement
{
    private static readonly string[] SpinnerFrames = ["|", "/", "-", "\\"];
    private readonly DispatcherTimer _spinnerTimer;
    private int _spinnerIndex;

    public static readonly DependencyProperty IsLoadingProperty =
        DependencyProperty.Register(
            nameof(IsLoading),
            typeof(bool),
            typeof(UnloadedHistorySpacer),
            new FrameworkPropertyMetadata(false, FrameworkPropertyMetadataOptions.AffectsRender, OnIsLoadingChanged));

    public UnloadedHistorySpacer()
    {
        _spinnerTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(150) };
        _spinnerTimer.Tick += (_, _) =>
        {
            _spinnerIndex = (_spinnerIndex + 1) % SpinnerFrames.Length;
            InvalidateVisual();
        };
    }

    public bool IsLoading
    {
        get => (bool)GetValue(IsLoadingProperty);
        set => SetValue(IsLoadingProperty, value);
    }

    private static void OnIsLoadingChanged(DependencyObject dependencyObject, DependencyPropertyChangedEventArgs e)
    {
        if (dependencyObject is not UnloadedHistorySpacer spacer)
        {
            return;
        }
        if (e.NewValue is true)
        {
            spacer._spinnerTimer.Start();
        }
        else
        {
            spacer._spinnerTimer.Stop();
            spacer._spinnerIndex = 0;
        }
        spacer.InvalidateVisual();
    }

    protected override void OnRender(DrawingContext drawingContext)
    {
        base.OnRender(drawingContext);
        if (ActualHeight <= 0 || ActualWidth <= 0)
        {
            return;
        }

        var dpi = VisualTreeHelper.GetDpi(this).PixelsPerDip;
        var textBrush = new SolidColorBrush(Color.FromRgb(120, 130, 145));
        var lineBrush = new SolidColorBrush(Color.FromRgb(226, 231, 238));
        var typeface = new Typeface("Segoe UI");
        const double step = 360;
        var label = IsLoading
            ? $"{SpinnerFrames[_spinnerIndex]}  履歴をロード中..."
            : "未ロード履歴  /  上へスクロールでロード";

        for (var y = 32.0; y < ActualHeight; y += step)
        {
            var lineY = Math.Min(y + 12, ActualHeight - 1);
            drawingContext.DrawLine(new Pen(lineBrush, 1), new Point(12, lineY), new Point(Math.Max(12, ActualWidth - 12), lineY));

            var text = new FormattedText(
                label,
                CultureInfo.CurrentCulture,
                System.Windows.FlowDirection.LeftToRight,
                typeface,
                12,
                textBrush,
                dpi)
            {
                MaxTextWidth = Math.Max(0, ActualWidth - 32),
                Trimming = TextTrimming.CharacterEllipsis
            };
            drawingContext.DrawText(text, new Point(16, y));
        }
    }
}
