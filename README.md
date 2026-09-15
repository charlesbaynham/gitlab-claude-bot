# gitlab-claude-bot

A polling daemon that lets a GitLab bot account run Claude Code: it picks up the account's to-dos and new comments on its own merge requests, runs one job in a throwaway Docker container, and pushes the result as the bot.

## What it does

- **An issue assigned to the bot** is implemented on a new branch, and the bot opens a merge request that says `Closes #<iid>`.
- **A comment on a merge request the bot opened** is answered in the same thread, and the changes it asks for are pushed to the merge request's branch. No mention needed — it is the bot's own merge request.
- **A comment anywhere else** is acted on only when the bot is `@`-mentioned.

There is no per-project configuration: the bot is added once to a group and works in every project underneath it.

## How it works

Every `POLL_INTERVAL` seconds the daemon reads two feeds: the bot's pending to-do list (assignments and mentions) and the comments on every one of its own open merge requests — all of them, every poll, since posting a note doesn't change an MR's `updated_at` so there's no cheaper filter to apply. Both are filtered to `ALLOWED_USERS`, and to-dos the bot cannot act on are marked done so the list stays short.

Before a job starts, the bot awards 👀 to the note that triggered it. That award is the record that the work is claimed: a note already wearing the bot's 👀 is never picked up again, which is what makes a crash, a restart or an overlapping poll safe.

Each job runs in a fresh container from `AGENT_IMAGE` — read-only root filesystem, no capabilities, a tmpfs home, and a bind-mounted shallow clone as the only writable path. Claude Code sees that clone and the text of the issue or merge request; it holds no GitLab credential. Every side effect on GitLab — the branch, the push, the merge request, every comment — is performed by the daemon afterwards, from the state of the working tree and the agent's final message.

The daemon runs **one job at a time**. A second trigger waits for the current job to finish.

## Setup

1. **Create a bot user on GitLab** — an ordinary user account that will be the bot's identity. Everything it does is attributed to it.
2. **Create a personal access token** on that account with the `api` scope, and keep it out of the repository.
3. **Get a Claude credential.** Run `claude setup-token` on a machine signed in to a Claude Pro or Max subscription and use the token it prints as `CLAUDE_CODE_OAUTH_TOKEN`, or set `ANTHROPIC_API_KEY` to pay per token. Set exactly one of the two.
4. **Add the bot as a Developer to a top-level group.** Membership is inherited by every project in the group, present and future, which is why no per-project setup exists. Projects in a personal namespace are not in any group and have to be added one by one.
5. **Configure and start it.**

   ```sh
   cp .env.example .env      # then fill in the tokens and ALLOWED_USERS
   mkdir -p state && chown 1000 state
   docker compose up -d
   ```

   `WORK_DIR` must be an absolute host path outside `/tmp`: the host's Docker daemon bind-mounts it into each job container, so the path has to mean the same thing inside the daemon's container and out. `DOCKER_GID` must match the host's `docker` group (`getent group docker | cut -d: -f3`).

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `GITLAB_URL` | `https://gitlab.com` | GitLab instance to poll. |
| `GITLAB_TOKEN` | *required* | Personal access token of the bot account, `api` scope. |
| `CLAUDE_CODE_OAUTH_TOKEN` | *one of two* | Claude Code subscription token from `claude setup-token`. |
| `ANTHROPIC_API_KEY` | *one of two* | API key, as an alternative to the subscription token. |
| `ALLOWED_USERS` | *required* | Comma-separated GitLab usernames whose to-dos and comments are acted on. |
| `AGENT_IMAGE` | *required* | Image each job runs in. Pin it by digest in production. |
| `STATE_DIR` | *required* | Directory for the state file and the git askpass helper. |
| `WORK_DIR` | *required* | Absolute host path for per-job checkouts. Must not be under `/tmp`. |
| `POLL_INTERVAL` | `30` | Seconds between polls. |
| `JOB_TIMEOUT` | `1800` | Seconds before a running job's container is killed. |
| `MAX_BUDGET_USD` | unset | Per-job spend cap passed to Claude Code. |
| `CLAUDE_MODEL` | unset | Model for the agent; unset uses Claude Code's default. |
| `HEALTH_PORT` | `8000` | Port of the `/health` endpoint. |
| `CLONE_DEPTH` | `50` | `git clone --depth` for each job. |

`LOG_LEVEL` (default `INFO`) is read by the daemon itself; `DOCKER_GID` is read by `docker-compose.yml` only.

## Trust model

Only the people in `ALLOWED_USERS` can start a job, but **the whole thread is copied into the agent's prompt**, so anyone who can comment on the issue or merge request can influence what the agent does. The system prompt marks that text as untrusted, which is a mitigation and not a boundary. Do not point this at a public project.

- The agent has **network access**, because most real work needs a package index.
- The agent holds **only the Claude credential**. The GitLab token stays in the daemon and never enters the job container.
- The bot pushes as a Developer, so it **cannot push to protected branches** and cannot merge anything. Protect the branches that matter.
- The daemon mounts the host's Docker socket, which is **root-equivalent on the host**. Run it on a machine you would already trust it on.

## Operational notes

- **State** lives in `STATE_DIR/state.json`: the last note seen on each of the bot's merge requests, and the time of the last merge-request poll. Deleting it makes the bot re-baseline rather than replay — open merge requests are picked up again from their newest comment.
- **A restart mid-job** loses that job's container and its checkout. Nothing is duplicated: the 👀 award means the trigger is not picked up again, and when an issue already has an open bot merge request the next job continues on its branch instead of opening a second one. A job interrupted before it pushed simply leaves no trace, and re-assigning the issue starts it again.
- **`review_requested` to-dos are ignored** in v1: being asked to review is not yet distinguished from being asked to change something.
- **`/health`** answers JSON on `HEALTH_PORT`. It is 200 while a job is running or the last poll succeeded recently — within three poll intervals plus one job timeout — and 503 otherwise, including before the first poll completes. The body carries the last poll time, the last poll error, the running job and the job counters.

## Development

```sh
uv sync
uv run pytest
```

The tests are offline: no network, no git, no Docker. `tests/conftest.py` holds the GitLab fake every test builds on.

## NixOS and Proxmox

`nix/gitlab-claude-bot.nix` is a NixOS module that runs the daemon as a hardened systemd service beside its own Docker daemon, with state on a separate volume. `flake.nix` packages the application and builds a Proxmox LXC template from that module, so the container is cattle: rebuilt from git, with only the state volume kept.

## Licence

MIT. See [LICENSE](LICENSE).
