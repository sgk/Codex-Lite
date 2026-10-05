export class ConnectionHealth {
  private catalogState: "idle" | "syncing" | "failed" = "idle";
  private listenerFailed = false;

  catalogStarted(): void { this.catalogState = "syncing"; }
  catalogCompleted(): void { this.catalogState = "idle"; }
  catalogFailed(): void { this.catalogState = "failed"; }
  listenerError(): void { this.listenerFailed = true; }

  confirmed(phase: "listen" | "heartbeat"): { state: "transport_ready"; phase: string } | undefined {
    if (phase === "listen") this.listenerFailed = false;
    if (this.catalogState !== "idle" || this.listenerFailed) return undefined;
    return { state: "transport_ready", phase };
  }
}
