namespace CodexLite;

internal static class RemoteSyncFeature
{
#if CODEX_LITE_REMOTE_SYNC
    public static bool IsEnabled => true;
#else
    public static bool IsEnabled => false;
#endif
}
