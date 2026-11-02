namespace CodexLite.Services;

// Capture immediately before insertion, not before the asynchronous request.
internal readonly record struct HistoryScrollPosition(double Offset, double Extent, long InteractionVersion, bool WasAtEnd = false)
{
    public double? OffsetAfterInsertion(double newExtent, double viewportHeight, long interactionVersion)
    {
        if (interactionVersion != InteractionVersion)
        {
            return null;
        }

        return WasAtEnd
            ? Math.Max(0, newExtent - viewportHeight)
            : Math.Max(0, Offset + newExtent - Extent);
    }
}
