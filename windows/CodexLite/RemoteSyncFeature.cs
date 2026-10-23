using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace CodexLite;

internal static class RemoteSyncFeature
{
    public static CloudSyncConfiguration? Configuration { get; }
    public static string? ConfigurationError { get; }
    public static bool IsEnabled => Configuration?.Enabled == true;

    static RemoteSyncFeature()
    {
        try
        {
            Configuration = CloudSyncConfiguration.Load(Path.Combine(AppContext.BaseDirectory, "cloud-sync.json"));
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException or InvalidOperationException)
        {
            // JSON may contain OAuth credentials. Never include its contents in diagnostics.
            ConfigurationError = ex is InvalidOperationException
                ? ex.Message : "cloud-sync.json を読み込めません。形式とアクセス権を確認してください。";
        }
    }

    public static CloudSyncConfiguration RequireConfiguration() => IsEnabled
        ? Configuration! : throw new InvalidOperationException(ConfigurationError ?? "クラウド同期は構成されていません。");
}

internal sealed record CloudSyncConfiguration(bool Enabled, FirebaseConnection? Firebase, CloudOAuthConfiguration? OAuth)
{
    public string Identity => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(
        JsonSerializer.Serialize(new[] { Firebase!.ProjectId, Firebase.AppId, OAuth!.ClientId }))));

    public bool RestoreEnabled(bool savedEnabled, string? savedIdentity) => Enabled && savedEnabled && savedIdentity == Identity;

    public static CloudSyncConfiguration? Load(string path)
    {
        if (!File.Exists(path)) return null;
        var config = JsonSerializer.Deserialize<CloudSyncConfiguration>(File.ReadAllText(path), new JsonSerializerOptions(JsonSerializerDefaults.Web));
        if (config is null) throw new InvalidOperationException("cloud-sync.json の設定が空です。");
        if (!config.Enabled) return config;
        if (string.IsNullOrWhiteSpace(config.Firebase?.ProjectId) ||
            string.IsNullOrWhiteSpace(config.Firebase.ApiKey) || string.IsNullOrWhiteSpace(config.Firebase.AppId) ||
            string.IsNullOrWhiteSpace(config.OAuth?.ClientId) || string.IsNullOrWhiteSpace(config.OAuth.ClientSecret))
            throw new InvalidOperationException("cloud-sync.json のFirebaseまたはOAuth設定が不足しています。");
        return config;
    }
}

internal sealed record FirebaseConnection(string ProjectId, string ApiKey, string AppId, string? AuthDomain = null);
internal sealed record CloudOAuthConfiguration(string ClientId, string ClientSecret);
