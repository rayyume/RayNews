# Top Category Auto-Scroll Design

## Goal

When a user activates the fixed category bar on the homepage, align the list state and viewport by returning to the top. A category change resets to page 1; reactivating the already-selected category on page 1 only returns to the top, does not request the same data again, and does not cancel an in-flight manual refresh for the unchanged view.

## Interaction

For a different category, or the current category while viewing a later page, prepare the target first page and animate toward the top in parallel. Keep the old list rendered during the movement. Apply the target list only after both target data is available and the viewport is near the top. Reuse `scrollPageToTop()`, so reduced-motion users receive the existing immediate-scroll behavior.

If target preparation fails, retain the original filter/page and restore the original scroll offset. Navigation sequence guards prevent an older click from overwriting a newer one. Successful application consumes pending-new state for the target, pushes the list URL, optionally closes the mobile sidebar, stabilizes the final top position, and schedules adjacent-page prefetch.

The behavior is opt-in from the fixed top category bar. Existing sidebar filter interactions keep their current behavior.

## Testing

Node runtime tests cover:

- the active page-1 category returning to the top without data preparation;
- a different category starting data preparation and scrolling concurrently, but applying only near the top when data is ready;
- failed preparation retaining the original filter/page and restoring the prior scroll offset; and
- a later click invalidating an earlier in-flight category transition.
