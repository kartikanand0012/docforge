/** The shared demo account, when this deployment is the public demo (synthetic data only).
 * Switched on by DOCFORGE_DEMO_PIN on the web server; nothing is shown otherwise. */
export type DemoSignIn = { tenant: string; email: string; pin: string };

export function demoSignIn(env: Record<string, string | undefined>): DemoSignIn | null {
  const pin = env.DOCFORGE_DEMO_PIN ?? "";
  if (!/^\d{6,}$/.test(pin)) return null;
  return { tenant: "demo", email: "demo@docforge.example", pin };
}
