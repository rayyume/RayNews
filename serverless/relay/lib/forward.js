import { RAYNEWS_WEBHOOK_URL, WEBHOOK_TOKEN, CHANNEL_USERNAME } from "./config.js";

// Single attempt, no retry: RayNews's own periodic polling is the backstop for any
// update this relay fails to deliver, so a retry queue here would just add state and
// complexity for no real gain.
export async function forwardUpdate(update, msg) {
  const chatUsername = (msg && msg.chat && msg.chat.username || "").toLowerCase();
  if (!chatUsername || chatUsername !== CHANNEL_USERNAME.toLowerCase()) {
    return;
  }

  try {
    await fetch(RAYNEWS_WEBHOOK_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-RayNews-Webhook-Token": WEBHOOK_TOKEN,
      },
      body: JSON.stringify(update),
    });
  } catch (e) {
    console.log("forwardUpdate failed: " + e);
  }
}
