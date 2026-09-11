/** One write lane shared by timer flushes and stream boundaries. */
export class OrderedWriter<T> {
  private pending: Promise<void> = Promise.resolve();

  constructor(private readonly persist: (value: T) => Promise<void>) {}

  write(value: T): Promise<void> {
    this.pending = this.pending.then(() => this.persist(value));
    return this.pending;
  }

  drain(): Promise<void> {
    return this.pending;
  }
}
