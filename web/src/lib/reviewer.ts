/** The reviewer's email is remembered on this device for convenience; the PIN never is. */

const KEY = "docforge.reviewer.email";

export function savedEmail(): string {
  try {
    return typeof window === "undefined" ? "" : (window.localStorage.getItem(KEY) ?? "");
  } catch {
    return "";
  }
}

export function rememberEmail(email: string): void {
  try {
    window.localStorage.setItem(KEY, email);
  } catch {
    /* storage unavailable: nothing to remember */
  }
}
