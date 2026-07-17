import { forwardUpdate } from "../lib/forward.js";

export default async function handleEditedChannelPost(update) {
  await forwardUpdate(update, update.edited_channel_post);
}
