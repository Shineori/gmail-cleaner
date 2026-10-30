# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (Shineori fork)
- Protected senders list (addresses or domains, subdomains included), enforced server-side on delete, archive, mark-read and unsubscribe, with a second From-header check before any change; fails closed if the list file is unreadable
- Action history with undo for trash, archive and mark-read (`data/history.json`, last 50 actions)
- Preview dialog with exact counts, per-sender breakdown, skipped protected senders and sample emails before every bulk action
- Optional Cloudflare Access JWT verification (`ACCESS_TEAM_DOMAIN`, `ACCESS_AUD`, `ACCESS_ALLOWED_EMAILS`) and cross-site request blocking
- Remote sign-in: paste Google's redirect address to finish OAuth when the app runs on a server
- `/healthz` endpoint, `docker-compose.server.yml`, `SELF_HOSTING.md`

### Changed (Shineori fork)
- Dockerfile runs as non-root, pins `uv`, installs without dev dependencies, adds a healthcheck
- Litepicker served locally (pinned 2.0.12) instead of `@latest` from a CDN
- Header and action row no longer overflow at phone width

### Added
- CodeRabbit AI code review integration with `.coderabbit.yaml` configuration
- Pre-commit hooks for code quality checks (ruff, bandit, trailing whitespace, etc.)
- Comprehensive type annotations throughout the codebase

### Changed
- Updated pre-commit hook versions to latest stable releases
- Improved code formatting consistency (double quotes, trailing commas, whitespace)
- Enhanced function signatures with multiline formatting for better readability
- Normalized code style across Python, JavaScript, CSS, and HTML files

### Fixed
- Timezone handling in CSV filename generation (now uses UTC)
- Missing return type annotations in multiple functions
- Closure variable binding in batch callback functions
- Test coverage improvements with proper mock assertions
- Boolean positional argument pattern in `mark_important_background`

## [1.0.0] - 2024-11-29

### Added
- Initial release
- Bulk unsubscribe functionality with one-click support
- Delete emails by sender with bulk operations
- Mark emails as read in bulk
- Smart filtering options (age, size, category, sender, label)
- Docker support for all platforms
- Gmail API integration with batch requests
- Privacy-first architecture (runs 100% locally)
- Gmail-style user interface
- Label management (create, apply, remove)
- Archive emails functionality
- Mark emails as important/unimportant
- Download emails as CSV export

[Unreleased]: https://github.com/Gururagavendra/gmail-cleaner/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Gururagavendra/gmail-cleaner/releases/tag/v1.0.0
