import type { Readable } from "node:stream";

/** EOF is the desktop's stop signal, including while authentication is pending. */
export function watchParentInput(input: Readable = process.stdin, stop: () => void = () => process.exit(0)): void {
  input.once("end", stop);
  input.resume();
}
