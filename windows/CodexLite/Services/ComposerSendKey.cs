namespace CodexLite.Services;

public static class ComposerSendKey
{
    public const string CtrlEnter = "ctrl-enter";
    public const string Enter = "enter";

    public static string Normalize(string? mode) => mode ?? CtrlEnter;

    public static bool ShouldSend(string? mode, bool isEnter, bool ctrl, bool shift, bool alt, bool meta, bool composing) =>
        isEnter && !composing && !shift && !alt && !meta &&
        (Normalize(mode) == Enter ? !ctrl : Normalize(mode) == CtrlEnter && ctrl);

    public static string Hint(string? mode) => Normalize(mode) switch
    {
        Enter => "Enterで送信・Shift+Enterで改行",
        CtrlEnter => "Ctrl+Enterで送信・Enterで改行",
        _ => "送信キー設定が不正です。設定画面で選び直してください。"
    };
}
