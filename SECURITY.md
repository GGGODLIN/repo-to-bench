# Security

## Trust boundary

`repo-to-bench` is a local evaluation toolkit, not a sandbox for arbitrary hostile operators.

- The operator controls the source repo, revisions, test command, model providers, credentials, and output roots.
- Harbor and Docker own the agent and verifier process lifecycle.
- The agent receives only the generated workspace.
- Hidden tests and the oracle patch remain outside the agent workspace.
- The judge receives only the anonymous review packet.
- Reveal mappings and isolation receipts remain controller-only.

The bundled synthetic smoke runs Docker process probes that mount only the agent or judge export and confirm that controller paths are absent.

## Paid execution

`plan` emits the resolved matrix, total trial count, and immutable digest. `execute` requires the exact digest through `--approve-digest`.

This prevents a changed task or matrix from reusing an earlier approval. It does not verify provider pricing, account quota, model availability, or budget. The operator must review those factors before approving a paid run.

## Filesystem safety

- Tool-owned run roots use marker files before replacement or recursive cleanup.
- Absolute mutation paths, traversal, symlink escapes, special files, and reserved tool paths fail closed where enforced by the stage.
- Toolkit-owned JSON reports are written through temporary files and atomically replaced.
- Replay never executes trajectory shell commands.
- Incomplete replay or matrix artifacts cannot enter the normal review path.

Known limits:

- Pathname checks cannot eliminate every TOCTOU race on a hostile local filesystem.
- Source hardlinks are not comprehensively isolated.
- Recursive cleanup may be partial after an operating-system failure.
- Replay v0.1.0 does not preserve executable modes or provide hardlink-specific defenses.

## Secret handling

Credential-shaped content fails closed during staging, task construction, replay, and release scanning. Findings report only category, safe path, line, and a masked fingerprint.

Current detection covers private-key blocks, AWS access keys, common GitHub／Shopify／Slack token formats, bearer credentials, URL credentials, and sensitive assignments. Known gaps include JSON-escaped credentials and some Unicode token-boundary variants.

Never commit:

- Provider API keys or OAuth tokens
- Private repo source or hidden tests
- Real model trajectories or transcripts
- Generated controller reveal mappings
- Personal credential-store, proxy, or home-directory configuration

## Public release checks

The release scan enumerates the tracked tree and reachable Git blobs. It rejects selected private identifiers, personal paths, credential patterns, forbidden filenames, symlinks, and non-regular tracked entries.

The scanner does not claim coverage of arbitrary Git metadata outside reachable tree blobs.

## Reporting a vulnerability

Use a private [GitHub security advisory](https://github.com/GGGODLIN/repo-to-bench/security/advisories/new). Do not include live credentials, private repository contents, hidden tests, or real trajectories in a public issue.
