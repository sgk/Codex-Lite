namespace CodexLite.Services;

public static class HistoryMessageMerge
{
    // A live message may arrive while the initial HTTP history request is in
    // flight. Keep that existing item rather than inserting its saved copy.
    public static int PrependMissing<T>(IList<T> target, IReadOnlyList<T> loaded, Func<T, string> id, int insertIndex)
    {
        var existingIds = target.Select(id).ToHashSet(StringComparer.Ordinal);
        var added = 0;
        for (var index = loaded.Count - 1; index >= 0; index--)
        {
            if (!existingIds.Add(id(loaded[index]))) continue;
            target.Insert(insertIndex, loaded[index]);
            added++;
        }
        return added;
    }
}
