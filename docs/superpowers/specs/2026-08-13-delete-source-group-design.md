# Delete Source Group Design

## Goal

Make the administrator delete action in source management remove the selected source label group itself as well as every article in that group. If a genuinely new article for one of those source names arrives later, RayNews must rediscover it as a new source with no retained customized label, category, or merge configuration.

## Scope and deletion unit

The deletion unit is the source label row visible to the administrator. A row may contain one source name or several source variants that have been grouped under the same label. Deleting the row removes the complete group submitted by the frontend, not only its primary source.

The operation removes:

- all articles whose effective feed source belongs to the submitted group, including aliases resolved by the backend;
- shared source category and label records for every source in the group;
- user-specific category overrides for those sources;
- shared and user-specific alias relationships whose alias or target belongs to the group; and
- existing article-dependent data already handled by article deletion, including AI results, favorites, and image-cache pins.

Article deletion tombstones remain. They prevent the same historical Telegram message IDs from returning after a refresh and do not prevent later articles with new IDs from being stored.

## Architecture and data flow

The existing `DELETE /sources/articles` endpoint remains the public interface so existing clients do not need a route migration. The frontend continues to send the row's complete `sources` array. The backend expands those names through known aliases, selects and deletes their articles, and then explicitly purges all source metadata connected to the resolved group.

Metadata deletion must be explicit rather than delegated to `cleanup_stale_source_categories()`. That cleanup intentionally preserves `manual` and `classified` sources, which is the current reason an empty label remains in the source drawer.

The backend response continues to report the article count and also reports the number of source-metadata rows removed. After success, the frontend reloads source metadata and news using the existing dependent-view refresh path. Because the source rows no longer exist, the label disappears from both the management list and the home drawer.

## Rediscovery behavior

No blocklist or source-level tombstone is introduced. When a later article with a new article ID arrives, the normal fetcher upsert stores it. The existing source discovery pass then creates a fresh `pending` source record, exactly as it does for a source seen for the first time. Old customized label text, category choices, user overrides, and merge relationships must not be restored.

`INITIAL_CATEGORY_MAP` remains the first-seen default for its known sources. However, initialization must only seed a known source when the articles table currently contains that effective source. This prevents a deleted known source from reappearing merely because a process restarted. When a later new article arrives, that article makes the source eligible for its original first-seen preset label and category; unknown sources continue to start as `Info` / `pending` through ordinary discovery.

## User interface

The delete button tooltip, confirmation prompt, and success message will say that both the source and its articles are deleted. The existing large-deletion confirmation behavior remains: groups with at least 20 articles require typing the displayed source label.

## Error handling and consistency

Input validation and administrator authorization remain unchanged. The endpoint starts one news-database transaction that deletes the selected articles, their `deleted_articles` tombstones and `ai_results`, then purges the resolved group’s shared/user categories and shared/user aliases. A failure at any news-database step rolls back the entire transaction, so no article, tombstone, AI result, or source metadata change persists; the endpoint returns HTTP 500 rather than a partial success. Favorites in the application database and image-cache unpinning run only after the news transaction commits. They are deliberately post-commit side effects because they are outside the news database transaction.

## Testing

Backend endpoint tests will prove that deleting a grouped source:

- removes articles from the primary source, submitted variants, and resolved aliases;
- preserves deletion tombstones for the removed article IDs;
- removes shared and user-specific category records;
- removes alias relationships connected as either alias or target;
- rolls back article, tombstone, AI-result, and source-metadata writes while preserving favorites and cache pins when a metadata write fails; and
- does not reseed a known source while it has no articles; and
- allows a later new article to recreate the source through normal discovery with `pending` status, using first-seen presets where applicable and never restoring the deleted customized settings.

A frontend contract test will prove that the delete copy describes deletion of both the source and its articles and that the complete source group is still submitted.
