import { forwardUpdate } from "../lib/forward.js";

export default async function handleChannelPost(update) {
  await forwardUpdate(update, update.channel_post);
}
