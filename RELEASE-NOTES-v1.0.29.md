# pyto-agent v1.0.29 (testing pre-release)

## Browser chat improvements

- Keep the conversation transcript within the available chat panel, so long histories do not push the page and composer outside the viewport.
- Let long code blocks scroll horizontally within their message.
- Add an **All chats** link to the chat page header for a direct return to the saved-session list.
- Let users permanently delete a saved chat from that list after confirmation. The active chat cannot be deleted.

## Testing

The full offline test suite passed: 886 tests under CPython 3.12. The web interface was also checked in Firefox at a narrow 320 × 760 viewport, including transcript scrolling, code-block overflow, navigation back to all chats, and deletion of an inactive session.

Please treat this as a testing pre-release. Pyto/Safari device behavior has not been verified; use the browser interface device checklist and report any issues before this becomes a stable release.
