using System.IO;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CodexLite.Services;

public sealed class GoogleOAuthClient
{
    private const string ClientIdSuffix = ".apps.googleusercontent.com";
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(20) };
    private string? _state;
    private string? _verifier;

    public async Task<GoogleOAuthSession> SignInAsync(CancellationToken cancellationToken = default)
    {
        var config = LoadConfig();
        var clientId = config.ClientId!;
        var redirectUri = RedirectUriForClientId(clientId);
        var savedRefreshToken = CredentialStore.Read();
        if (!string.IsNullOrWhiteSpace(savedRefreshToken))
        {
            try
            {
                return await RefreshAsync(clientId, config.ClientSecret, savedRefreshToken, cancellationToken);
            }
            catch
            {
                CredentialStore.Delete();
            }
        }

        _state = Base64Url(RandomNumberGenerator.GetBytes(32));
        _verifier = Base64Url(RandomNumberGenerator.GetBytes(64));
        var challenge = Base64Url(SHA256.HashData(Encoding.ASCII.GetBytes(_verifier)));
        var authorizationUrl = "https://accounts.google.com/o/oauth2/v2/auth"
            + $"?client_id={Uri.EscapeDataString(clientId)}"
            + $"&redirect_uri={Uri.EscapeDataString(redirectUri)}"
            + "&response_type=code&scope=openid%20email%20profile&access_type=offline&prompt=consent"
            + $"&state={Uri.EscapeDataString(_state)}&code_challenge={Uri.EscapeDataString(challenge)}&code_challenge_method=S256";
        BrowserLauncher.Open(authorizationUrl);
        var callback = await App.WaitForOAuthCallbackAsync(cancellationToken);
        var uri = new Uri(callback);
        var query = ParseQuery(uri.Query);
        if (!string.Equals(query.GetValueOrDefault("state"), _state, StringComparison.Ordinal))
        {
            throw new InvalidOperationException("Google認証のstateが一致しません。");
        }
        if (!query.TryGetValue("code", out var code) || string.IsNullOrWhiteSpace(code))
        {
            throw new InvalidOperationException("Google認証コードを受け取れませんでした。");
        }
        var parameters = new Dictionary<string, string>
        {
            ["client_id"] = clientId,
            ["code"] = code,
            ["code_verifier"] = _verifier,
            ["grant_type"] = "authorization_code",
            ["redirect_uri"] = redirectUri,
        };
        if (!string.IsNullOrWhiteSpace(config.ClientSecret)) parameters["client_secret"] = config.ClientSecret;
        using var content = new FormUrlEncodedContent(parameters);
        using var response = await _http.PostAsync("https://oauth2.googleapis.com/token", content, cancellationToken);
        var body = await response.Content.ReadAsStringAsync(cancellationToken);
        if (!response.IsSuccessStatusCode)
        {
            throw new InvalidOperationException(
                $"Google認証コードの交換に失敗しました。{DescribeOAuthError(body, response.StatusCode)}");
        }
        var token = JsonSerializer.Deserialize<GoogleTokenResponse>(body, JsonOptions);
        if (string.IsNullOrWhiteSpace(token?.AccessToken)) throw new InvalidOperationException("Googleアクセストークンを取得できませんでした。");
        if (!string.IsNullOrWhiteSpace(token.RefreshToken)) CredentialStore.Write(token.RefreshToken);
        return ToSession(token, token.RefreshToken);
    }

    public async Task<GoogleOAuthSession?> TryRestoreSessionAsync(CancellationToken cancellationToken = default)
    {
        var savedRefreshToken = CredentialStore.Read();
        if (string.IsNullOrWhiteSpace(savedRefreshToken)) return null;
        try
        {
            return await RefreshAsync(savedRefreshToken, cancellationToken);
        }
        catch (Exception ex)
        {
            throw new InvalidOperationException("保存済みRemote認証の更新に失敗しました。", ex);
        }
    }

    public async Task<GoogleOAuthSession> RefreshAsync(string refreshToken, CancellationToken cancellationToken = default)
    {
        var config = LoadConfig();
        return await RefreshAsync(config.ClientId!, config.ClientSecret, refreshToken, cancellationToken);
    }

    private async Task<GoogleOAuthSession> RefreshAsync(string clientId, string? clientSecret, string refreshToken, CancellationToken cancellationToken)
    {
        var parameters = new Dictionary<string, string>
        {
            ["client_id"] = clientId,
            ["refresh_token"] = refreshToken,
            ["grant_type"] = "refresh_token",
        };
        if (!string.IsNullOrWhiteSpace(clientSecret)) parameters["client_secret"] = clientSecret;
        using var content = new FormUrlEncodedContent(parameters);
        using var response = await _http.PostAsync("https://oauth2.googleapis.com/token", content, cancellationToken);
        var body = await response.Content.ReadAsStringAsync(cancellationToken);
        if (!response.IsSuccessStatusCode) throw new InvalidOperationException("Googleアクセストークンの更新に失敗しました。");
        var token = JsonSerializer.Deserialize<GoogleTokenResponse>(body, JsonOptions);
        if (string.IsNullOrWhiteSpace(token?.AccessToken)) throw new InvalidOperationException("更新後のGoogleアクセストークンを取得できませんでした。");
        var nextRefreshToken = string.IsNullOrWhiteSpace(token.RefreshToken) ? refreshToken : token.RefreshToken;
        CredentialStore.Write(nextRefreshToken);
        return ToSession(token, nextRefreshToken);
    }

    private static GoogleOAuthSession ToSession(GoogleTokenResponse token, string? refreshToken)
    {
        return new GoogleOAuthSession(token.AccessToken!, refreshToken, DateTimeOffset.UtcNow.AddSeconds(Math.Max(60, token.ExpiresIn)));
    }

    private static OAuthConfig LoadConfig()
    {
        var path = Path.Combine(AppContext.BaseDirectory, "remote-oauth.json");
        if (!File.Exists(path)) throw new FileNotFoundException("このビルドにはRemote OAuth設定が含まれていません。", path);
        using var stream = File.OpenRead(path);
        var config = JsonSerializer.Deserialize<OAuthConfig>(stream, JsonOptions);
        if (string.IsNullOrWhiteSpace(config?.ClientId) || string.IsNullOrWhiteSpace(config.ClientSecret))
        {
            throw new InvalidOperationException("Remote OAuth設定が不完全です。");
        }
        return config;
    }

    public static string RedirectUriForClientId(string clientId)
    {
        var id = clientId.EndsWith(ClientIdSuffix, StringComparison.OrdinalIgnoreCase)
            ? clientId[..^ClientIdSuffix.Length]
            : clientId;
        return $"com.googleusercontent.apps.{id}:/oauth2redirect";
    }

    public static string? TryGetRedirectScheme()
    {
        try
        {
            var clientId = LoadConfig().ClientId!;
            return new Uri(RedirectUriForClientId(clientId)).Scheme;
        }
        catch
        {
            return null;
        }
    }

    private static Dictionary<string, string> ParseQuery(string query) => query.TrimStart('?').Split('&', StringSplitOptions.RemoveEmptyEntries)
        .Select(part => part.Split('=', 2))
        .Where(parts => parts.Length == 2)
        .ToDictionary(parts => Uri.UnescapeDataString(parts[0]), parts => Uri.UnescapeDataString(parts[1]), StringComparer.Ordinal);

    private static string Base64Url(byte[] bytes) => Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');

    private static string DescribeOAuthError(string body, System.Net.HttpStatusCode statusCode)
    {
        try
        {
            using var document = JsonDocument.Parse(body);
            var root = document.RootElement;
            var error = ReadJsonString(root, "error");
            var description = ReadJsonString(root, "error_description");
            if (!string.IsNullOrWhiteSpace(description))
            {
                return $"{description} ({error ?? statusCode.ToString()})";
            }

            if (!string.IsNullOrWhiteSpace(error)) return error;
        }
        catch (JsonException)
        {
            // Googleのエラー本文がJSONでない場合は、秘密情報を画面に出さずHTTP状態だけ表示する。
        }

        return statusCode.ToString();
    }

    private static string? ReadJsonString(JsonElement root, string propertyName)
    {
        return root.TryGetProperty(propertyName, out var property) && property.ValueKind == JsonValueKind.String
            ? property.GetString()
            : null;
    }

    private sealed record OAuthConfig(string? ClientId, string? ClientSecret);
    private sealed record GoogleTokenResponse(
        [property: JsonPropertyName("access_token")] string? AccessToken,
        [property: JsonPropertyName("expires_in")] int ExpiresIn,
        [property: JsonPropertyName("refresh_token")] string? RefreshToken);
}

public sealed record GoogleOAuthSession(string AccessToken, string? RefreshToken, DateTimeOffset ExpiresAt);

internal static class CredentialStore
{
    private const string TargetName = "CodexLite/RemoteGoogleRefreshToken";
    private const int GenericCredentialType = 1;

    public static void Write(string value)
    {
        var target = Marshal.StringToCoTaskMemUni(TargetName);
        var blob = Marshal.AllocHGlobal(Encoding.UTF8.GetByteCount(value));
        try
        {
            var bytes = Encoding.UTF8.GetBytes(value);
            Marshal.Copy(bytes, 0, blob, bytes.Length);
            var credential = new NativeCredential
            {
                Type = GenericCredentialType,
                TargetName = target,
                CredentialBlob = blob,
                CredentialBlobSize = (uint)bytes.Length,
                Persist = 2,
                UserName = Marshal.StringToCoTaskMemUni(Environment.UserName),
            };
            if (!CredWrite(ref credential, 0)) throw new InvalidOperationException("Google refresh tokenをWindows Credential Managerへ保存できませんでした。");
            Marshal.FreeCoTaskMem(credential.UserName);
        }
        finally
        {
            Marshal.FreeCoTaskMem(target);
            Marshal.FreeHGlobal(blob);
        }
    }

    public static string? Read()
    {
        if (!CredRead(TargetName, GenericCredentialType, 0, out var pointer)) return null;
        try
        {
            var credential = Marshal.PtrToStructure<NativeCredential>(pointer);
            if (credential.CredentialBlob == IntPtr.Zero || credential.CredentialBlobSize == 0) return null;
            var bytes = new byte[credential.CredentialBlobSize];
            Marshal.Copy(credential.CredentialBlob, bytes, 0, bytes.Length);
            return Encoding.UTF8.GetString(bytes);
        }
        finally
        {
            CredFree(pointer);
        }
    }

    public static void Delete() => CredDelete(TargetName, GenericCredentialType, 0);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredWrite(ref NativeCredential userCredential, uint flags);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredRead(string targetName, int type, int flags, out IntPtr credential);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredDelete(string targetName, int type, int flags);

    [DllImport("advapi32.dll")]
    private static extern void CredFree(IntPtr credential);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NativeCredential
    {
        public uint Flags;
        public uint Type;
        public IntPtr TargetName;
        public IntPtr Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        public IntPtr TargetAlias;
        public IntPtr UserName;
    }
}

internal static class BrowserLauncher
{
    public static void Open(string url) => System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo { FileName = url, UseShellExecute = true });
}
