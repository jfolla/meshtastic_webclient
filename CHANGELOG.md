# Changelog

All notable changes to this project are documented in this file.

## v0.6.9

- added tabbed UI layout
- added **Channels** tab
- added channel management API endpoints
- added TX channel selection workflow
- added post-action verification after channel operations
- added explicit TCP support using `node.tcp_port`
- fixed startup script handling for proxy host/port parsing
- refreshed documentation for GitHub publishing

## v0.6.8

- stabilized startup behavior
- improved proxy error handling
- reduced cases that resulted in empty proxy responses

## v0.6.7

- introduced verified TX-channel selection workflow
- improved proxy response consistency

## v0.6.6

- merged tabbed UI work with channel-management work
- added initial serial-safe web channel workflow

## v0.6.5

- introduced tabbed UI layout

## v0.6.4

- baseline package used as functional reference
- proxy-first architecture retained
- serial/TCP backend support
- local address book and message cache
- configuration import/export
- service-based startup
