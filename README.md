# aa-sigcheck-cog

An [Alliance Auth](https://gitlab.com/allianceauth/allianceauth) Discord cog
(for [allianceauth-discordbot](https://github.com/pvyParts/allianceauth-discordbot))
that checks whether a character meets the requirements of a
[Secure Groups](https://github.com/Solar-Helix-Independent-Transport/allianceauth-secure-groups)
group and reports, filter by filter, what it **meets (✅)** and **doesn't meet (❌)**.

In a typical setup [corptools](https://github.com/Solar-Helix-Independent-Transport/allianceauth-corptools)
feeds the character data and Secure Groups uses per-group "smart filters" to
automatically add or remove members. This cog lets you ask, on demand, *why* a
character does or doesn't qualify — without waiting for the next cron run.

## What it does

Given a character name, the cog:

1. Resolves the character to its owning Alliance Auth account.
2. For each Secure Group (or one named group), runs every smart filter against
   that account.
3. Replies with a per-filter ✅/❌ breakdown and an overall verdict
   (🟩 *Qualifies* / 🟥 *Does not qualify*).

A Secure Group only grants membership when **all** of its filters pass, so the
verdict is the AND of every filter. Because Secure Groups evaluates at the
account level (every one of a user's characters is considered by the filters),
the result is the same whichever of a user's characters you name.

The report also flags a mismatch between the live verdict and current
membership — e.g. *"Currently a member but no longer qualifies — would be
removed"* — so you can see what the next Secure Groups run will do.

## Access control

Access is gated per channel:

- **Open channels** — anyone may run the command.
- **Restricted channels** — only an allowlist of specific Discord users may run
  the command.
- Any channel in neither list is disallowed entirely.

## Requirements

- Alliance Auth ≥ 5.0
- allianceauth-discordbot (recent)
- allianceauth-corptools
- allianceauth-secure-groups
- Python ≥ 3.10

These must already be installed in the host environment; the cog declares no
dependencies of its own so it can't drag in conflicting versions.

## Installation

Add it to your Alliance Auth `requirements.txt` (pin to a released tag):

```
git+https://github.com/TheLordStyle/aa-sigcheck-cog.git@v0.1.0
```

Then in your `local.py`:

```python
# Register the cog with the discord bot
DISCORD_BOT_COGS += ["aa_sigcheck.sigcheck"]

# Channels where anyone may run /sigcheck
SIGCHECK_DISCORD_BOT_CHANNELS = [
    111111111111111111,   # #recruitment
]

# Channels where only specific Discord users may run /sigcheck
# { channel_id: [discord_user_id, ...] }
SIGCHECK_DISCORD_BOT_RESTRICTED_CHANNELS = {
    222222222222222222: [   # #leadership
        333333333333333333,
        444444444444444444,
    ],
}
```

Restart the discord bot after editing settings.

### Settings

| Setting | Default | Description |
|---|---|---|
| `SIGCHECK_DISCORD_BOT_CHANNELS` | `[]` | Channel IDs where anyone may use the command. |
| `SIGCHECK_DISCORD_BOT_RESTRICTED_CHANNELS` | `{}` | `{channel_id: [discord_user_id, ...]}` — channels limited to listed users. |

## Usage

Both prefix and slash forms are available:

```
!sigcheck Some Pilot
!sigcheck group:"Capital Pilots" Some Pilot
/sigcheck character: Some Pilot
/sigcheck character: Some Pilot group: Capital Pilots
```

- With no group, the report covers every Secure Group.
- With a group, only that group's filter breakdown is shown (and a clean pass
  colours the embed green).

## How it works

For each `securegroups.models.SmartGroup` the cog reads the group's
`SmartFilter` set and calls each filter object's `process_filter(user)` — the
same call Secure Groups uses to decide membership — then aggregates the results.
Evaluation runs off the event loop (`sync_to_async`) after the interaction is
deferred, since filters can touch skills/assets data.

## Caveats

- Results reflect the data corptools and Secure Groups already hold. If
  corptools data is stale, or a filter was just changed, the on-demand result
  can differ from what the periodic Secure Groups run will compute.
- A character that is known to Auth but not linked to any account can't be
  evaluated (there's no account to check); the cog says so explicitly.
- A Secure Group with no filters configured is reported as not-qualifying, since
  there's no automatic criterion to satisfy.

## License

MIT — see [LICENSE](LICENSE).
