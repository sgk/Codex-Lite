// All callers share the actual completion, including rejection. Returning early
// while another sync is running would make a revision watcher busy-poll forever.
export class SingleFlight {
  private pending: Promise<void> | undefined;

  run(operation: () => Promise<void>): Promise<void> {
    if (this.pending) return this.pending;
    // Install the promise before invoking operation, so even a synchronous
    // exception or a reentrant caller cannot start a second operation.
    const pending = Promise.resolve().then(operation);
    this.pending = pending;
    const clear = () => {
      if (this.pending === pending) this.pending = undefined;
    };
    // Handle both outcomes without creating an unhandled rejecting finally()
    // promise. The original rejection is still delivered to every caller.
    void pending.then(clear, clear);
    return pending;
  }
}
