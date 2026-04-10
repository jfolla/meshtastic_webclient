# Changelog

## v0.7.2
- refined Channels tab messaging to remove duplicate fallback warnings
- added explicit **URL-only mode** wording for TCP/node API combinations that do not expose a structured channel list
- kept `Join from URL` bound to the active backend connection
- refreshed GitHub-ready README and screenshot packaging

# Changelog

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
