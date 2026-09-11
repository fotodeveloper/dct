# Dependency refresh — September 2026

The updated baseline retains Python 3.11+. Requirements remain ranges for PyPI
consumers; `uv.lock` records the resolved development set. Both editable runtimes
must be refreshed when requirements change, followed by an MCP reconnect.

| Package | Previous runtime | Updated | Relevant upstream changes |
|---|---|---|---|
| MCP | 2.0.0 | 2.2.0 | Result-schema compatibility and handler-error hardening in 2.1; HTTP/OAuth/session and schema-reference fixes in 2.2. Local stdio does not use most HTTP changes. |
| SQLAlchemy | 2.0.51 | 2.0.52 | SQLite UNIQUE reflection performance fix; PostgreSQL sequence quoting and CHECK reflection corrections. ORM-only fixes do not affect DCT's Core usage. |
| psycopg | 3.3.4 | 3.3.5 | Prepared-statement invalidation after schema changes and I/O/adaptation fixes, relevant to migration-heavy workloads. |
| Markdown | 3.10.2 | 3.10.3 | Correct Setext heading recognition; affects rendered Markdown, not DCT's separate plan parser. |
| packaging | 26.2 | 26.3 | Metadata/tag handling and new version-range APIs. Doctor uses existing Requirement/SpecifierSet APIs. |
| Ruff | 0.16.0 | 0.16.7 | Updated pinned lint tool, kept identical in CI and release jobs; full lint verified. |

Official release notes: [MCP 2.1](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.1.0),
[MCP 2.2](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.2.0),
[SQLAlchemy](https://docs.sqlalchemy.org/en/20/changelog/changelog_20.html#change-2.0.52),
[psycopg](https://www.psycopg.org/psycopg3/docs/news.html),
[Markdown](https://python-markdown.github.io/changelog/),
[packaging](https://packaging.pypa.io/en/stable/changelog.html).

Transitive updates include AnyIO 4.15.1 (cancellation/cleanup fixes), Starlette
1.6.0 (HTTP limits/ranges/streaming improvements) and cryptography 50.0.1
(including a PKCS7 decryption-oracle fix). DCT does not directly use PKCS7;
this is not evidence that DCT itself was vulnerable. Its dashboard uses Flask,
so Starlette changes primarily affect MCP's optional HTTP stack.

Sources: [AnyIO](https://anyio.readthedocs.io/en/stable/versionhistory.html),
[Starlette](https://www.starlette.io/release-notes/),
[cryptography](https://cryptography.io/en/latest/changelog/).

Flask 3.1.3, python-ulid 4.0.1, pytest 9.1.1 and pytest-cov 7.1.0 were already
current in the development environment; their tested versions now define the
baseline. Platformdirs is updated to 4.11.8 for Windows. The MCP tool runtime
also had stale dctracker metadata and python-ulid; reinstalling the editable
package corrects that drift.

Transitive packages must satisfy their parents' constraints. A newer published
pydantic-core is not installed independently of the exact version required by
pydantic. Prereleases are not part of this refresh.

Automated `pip-licenses` inspection found no unknown license metadata or GPL/AGPL
dependencies in the inspected set. Optional PostgreSQL packages `psycopg` and
`psycopg-binary` retain LGPL-3.0-only licensing; the MCP-only wheel environment
uses permissive licenses. This inventory is not a blanket license classification
for future dependency resolutions or a change to DCT's MIT license.
