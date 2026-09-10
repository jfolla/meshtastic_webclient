# Changelog

## v0.7.7-beta

- Normalize direct recipients to numeric node IDs before Meshtastic sends.
- Support explicit hexadecimal IDs, decimal numbers and known node database keys.
- Validate channel indices and return actionable send validation errors.
- Clarify that Send to selects a node, not a radio channel.
- Pass 26 regression tests, including real library recipient conversion.

## v0.7.6-beta

- Added mandatory login with local admin/viewer accounts and scrypt password hashes.
- Added expiring server-side sessions, logout, CSRF checks, secure cookies and login throttling.
- Enforced read-only permissions on API mutations and device refreshes.
- Added outgoing delivery status, recipient ACK correlation, NAK handling and confirmation timeouts.
- Kept broadcast delivery explicitly unconfirmed and preserved status changes in SQLite batches.
- Added account setup instructions and 10 auth/delivery tests, for 22 regression tests total.
- UI, messages, documentation and upgrade package remain in English.

## v0.7.5-beta

- Batch SQLite synchronization with deduplication and retention of 10,000 messages.
- Apply/Rollback with complete admin read-back and verification that survives restarts.
- Channel URLs and PSKs removed from API responses; fingerprints in the UI and Apply by room_id.
- Loopback-only proxy.
- Removed raw_json storage and added 2/5/10/20/30-second reconnection backoff.
- Passed 12 regression tests; physical radio validation remains pending.
- Standardized the interface, messages and documentation in English, including the downloadable ZIP.

## v0.7.4-beta

- Reworked the UI into six real selectable tabs: Chat, Channels, Address Book, Nodes, Config and Debug.
- Kept tab switching independent from application JavaScript by using CSS/radio navigation.
- Consolidated browser polling into a single `/api/snapshot` request.
- Consolidated Flask-to-proxy polling into one local snapshot request.
- Reworked channel read/apply/rollback to use the already-open Meshtastic Python connection (`localNode.getURL()` / `localNode.setURL()`).
- Added channel URL/hash Preview before Import or Apply.
- Added automatic channel backup and Rollback with serialized channel operations.
- Preserved proxy message IDs to avoid duplicate messages after service restarts.
- Made JSON writes atomic and sensitive runtime files owner-only.
- Made upgrade archives configuration-safe: runtime config, address book and saved rooms are no longer shipped as repository defaults.
- Updated launcher supervision so proxy and web processes are restarted together if either one exits.
- Added `tools/static_selftest.py`.
- Replaced deprecated UTC timestamp handling with timezone-aware timestamps.
- Added the v0.7.4-beta Channels screenshot and refreshed the English README.

## v0.7.2

- Refined Channels tab messaging to remove duplicate fallback warnings.
- Added explicit URL-only mode wording for TCP/node API combinations that do not expose a structured channel list.
- Kept Join from URL bound to the active backend connection.
- Refreshed GitHub-ready README and screenshot packaging.

## v0.7.1

- Fixed channel handling for TCP-connected nodes where `requestChannels()` succeeds but `node.channels` stays unset.
- Added URL-only fallback mode for the Channels tab.
- Updated Join-from-URL verification to prefer the active backend connection and `getURL()` fallback checks.
- Improved UI messaging for channel operations when the node/API does not expose a structured channel list.
- Kept the repository GitHub-ready with screenshot, README, changelog, and cleaned metadata files.

## v0.7.0

- Added GitHub-ready packaging with screenshot and refreshed documentation.
- Improved proxy robustness for channel operations and error handling.

## v0.6.9

- Added tabbed UI layout and initial Channels tab integration.


