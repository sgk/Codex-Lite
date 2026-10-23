namespace CodexLite.Services;

// A lost HTTP response is not a rejection. Once uncertain, this submission can
// only read its receipt, including after the user presses the confirmation button.
public sealed class SubmissionReceiptTracker
{
    public bool NeedsConfirmation { get; private set; }
    public int Checks { get; private set; }
    public DateTimeOffset NextCheckAt { get; private set; }
    public bool Exhausted => Checks >= 12;
    public bool CanCheck(DateTimeOffset now) => !NeedsConfirmation || (!Exhausted && now >= NextCheckAt);

    public void ResetChecks()
    {
        Checks = 0;
        NextCheckAt = DateTimeOffset.MinValue;
    }

    public async Task<T?> ResolveAsync<T>(Func<Task<T?>> send, Func<Task<T?>> readReceipt,
        Func<Exception, bool> isUncertain, DateTimeOffset now) where T : class
    {
        if (!CanCheck(now)) throw new InvalidOperationException("送信受付の確認時刻または回数上限に達しています。");
        if (NeedsConfirmation)
        {
            Checks++;
            NextCheckAt = now.AddSeconds(5);
            return await readReceipt();
        }
        try
        {
            var result = await send();
            if (result is not null) return result;
        }
        catch (Exception ex) when (isUncertain(ex))
        {
            // Keep the immutable request ID in the owner; no second POST.
        }
        NeedsConfirmation = true;
        NextCheckAt = now.AddSeconds(2);
        return null;
    }
}
