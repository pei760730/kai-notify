/**
 * kai-notify (TypeScript) — outbound Telegram push for Kai's AI backend.
 *
 * Mirrors the Python core. Same rules: fail-soft (never throw), fail-closed
 * (single configured recipient), plain text only, zero runtime deps (uses the
 * global `fetch`, Node >= 18).
 */

const API = (token: string) =>
  `https://api.telegram.org/bot${token}/sendMessage`;

const TIMEOUT_MS = 10_000;

function credentials(): { token: string; chatId: string } | null {
  const token = (process.env.KAI_NOTIFY_BOT_TOKEN ?? "").trim();
  const chatId = (process.env.KAI_NOTIFY_CHAT_ID ?? "").trim();
  if (!token || !chatId) {
    // Not an error: a repo without the secret set simply stays silent.
    console.warn(
      "kai-notify: KAI_NOTIFY_BOT_TOKEN / KAI_NOTIFY_CHAT_ID not set; " +
        "skipping push (fail-soft).",
    );
    return null;
  }
  return { token, chatId };
}

async function send(text: string): Promise<boolean> {
  const creds = credentials();
  if (!creds) return false;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(API(creds.token), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: creds.chatId,
        text,
        disable_web_page_preview: true,
      }),
      signal: controller.signal,
    });
    if (!res.ok) {
      console.warn(`kai-notify: send failed (fail-soft): HTTP ${res.status}`);
      return false;
    }
    return true;
  } catch (err) {
    console.warn("kai-notify: send failed (fail-soft):", err);
    return false;
  } finally {
    clearTimeout(timer);
  }
}

/** Push a single line / block of text. Skips empty; never throws. */
export async function notify(text: string): Promise<boolean> {
  const t = (text ?? "").trim();
  if (!t) return false;
  return send(t);
}

/** Push a titled bullet list. Blanks dropped; skips if nothing to say. */
export async function notifyDigest(
  title: string,
  items: ReadonlyArray<unknown>,
): Promise<boolean> {
  const lines: string[] = [];
  const t = (title ?? "").trim();
  if (t) lines.push(t);
  for (const item of items ?? []) {
    const s = String(item ?? "").trim();
    if (s) lines.push(`• ${s}`);
  }
  if (lines.length === 0) return false;
  return send(lines.join("\n"));
}
