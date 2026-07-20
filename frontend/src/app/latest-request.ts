/**
 * Small monotonic gate for async UI work.
 *
 * Every navigation/conversation transition advances the gate. Async callbacks
 * may update the UI only while the token captured at their start is current.
 */
export class LatestRequestGate {
  private generation = 0;

  begin(): number {
    this.generation += 1;
    return this.generation;
  }

  invalidate(): void {
    this.generation += 1;
  }

  isCurrent(token: number): boolean {
    return token === this.generation;
  }
}
