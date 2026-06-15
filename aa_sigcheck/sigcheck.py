"""SigCheck: report whether a character meets a Secure Groups group's filters.

For a given character name this resolves the owning Alliance Auth account and,
for each Secure Group (`securegroups.models.SmartGroup`), reports which of the
group's smart filters the account meets (✅) and which it does not (❌), plus an
overall qualify / doesn't-qualify verdict. A Secure Group only grants membership
when *every* one of its filters passes, so the verdict is the AND of all filters.

Secure Groups operates at the account level (all of a user's characters are
considered by the individual filters), so the result is the same regardless of
which of a user's characters is named.
"""
import logging

import discord
from aadiscordbot.app_settings import get_all_servers
from discord import option
from discord.embeds import Embed
from discord.ext import commands

from asgiref.sync import sync_to_async

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter

from securegroups.models import SmartGroup

logger = logging.getLogger(__name__)


# ---- Channel / user access --------------------------------------------------
#
# Two settings drive access:
#   SIGCHECK_DISCORD_BOT_CHANNELS            -> [channel_id, ...]
#       channels where anyone may run the command.
#   SIGCHECK_DISCORD_BOT_RESTRICTED_CHANNELS -> {channel_id: [discord_user_id]}
#       channels where only the listed Discord users may run the command.
# A channel in neither setting is disallowed entirely.

def _open_channels():
    return getattr(settings, "SIGCHECK_DISCORD_BOT_CHANNELS", [])


def _restricted_channels():
    return getattr(settings, "SIGCHECK_DISCORD_BOT_RESTRICTED_CHANNELS", {})


def _channel_access(channel_id: int, discord_user_id: int) -> bool:
    """True if `discord_user_id` may run the command in `channel_id`."""
    if channel_id in _open_channels():
        return True
    allowed = _restricted_channels().get(channel_id)
    if allowed is not None:
        return discord_user_id in allowed
    return False


# ---- Resolution & evaluation (sync ORM) -------------------------------------

def _resolve_account(name: str):
    """Resolve a character name to (EveCharacter, owning User).

    Returns (None, None) for an unknown character, and (EveCharacter, None)
    for a known character not linked to any Alliance Auth account.
    """
    ec = EveCharacter.objects.filter(character_name__iexact=name.strip()).first()
    if ec is None:
        return None, None
    try:
        return ec, ec.character_ownership.user
    except (CharacterOwnership.DoesNotExist, ObjectDoesNotExist, AttributeError):
        return ec, None


def _smart_groups(group_name: str = None):
    qs = SmartGroup.objects.select_related("group").prefetch_related("filters")
    if group_name:
        qs = qs.filter(group__name__iexact=group_name.strip())
    return list(qs)


def _evaluate(user, group_name: str = None):
    """Evaluate `user` against Secure Groups; return a list of result dicts.

    Each dict: {group, in_group, filters: [(description, passed)], qualifies}.
    A group with no filters can never be qualified for automatically, so it is
    reported as not-qualifying.
    """
    results = []
    for sg in _smart_groups(group_name):
        checks = []
        for sf in sg.filters.all():
            fo = sf.filter_object
            if fo is None:
                # Orphaned SmartFilter (concrete filter object deleted).
                checks.append(("(unavailable filter)", False))
                continue
            try:
                passed = bool(fo.process_filter(user))
            except Exception:
                logger.exception(
                    "process_filter failed for filter %r on user %s",
                    fo, getattr(user, "username", user),
                )
                passed = False
            checks.append((getattr(fo, "description", str(fo)), passed))
        qualifies = bool(checks) and all(p for _, p in checks)
        results.append({
            "group": sg.group.name,
            "in_group": user.groups.filter(pk=sg.group_id).exists(),
            "filters": checks,
            "qualifies": qualifies,
        })
    return results


# ---- Formatting -------------------------------------------------------------
#
# Mirrors the chunk/paginate approach used by aa-authcheck so we never produce
# an embed description over Discord's 4096-char cap.

_MAX_BLOCK = 3500


def _chunk_section(section_header, lines):
    """Split one group's lines into ≤_MAX_BLOCK-char chunks.

    Each chunk carries its own header — the first uses `section_header`
    verbatim, follow-ups append ` (cont.)`.
    """
    chunks, current, size = [], [section_header], len(section_header)
    for line in lines:
        addition = len(line) + 1  # newline
        if size + addition > _MAX_BLOCK and len(current) > 1:
            chunks.append("\n".join(current))
            cont = f"{section_header} (cont.)"
            current, size = [cont, line], len(cont) + addition
        else:
            current.append(line)
            size += addition
    chunks.append("\n".join(current))
    return chunks


def _group_block(result):
    """Return one or more text blocks describing a single group result."""
    verdict = "🟩 Qualifies" if result["qualifies"] else "🟥 Does not qualify"
    header = f"__**{result['group']}** — {verdict}__"

    # Surface a mismatch between live membership and the current verdict, since
    # the Secure Groups cron will act on it on its next run.
    note = None
    if result["in_group"] and not result["qualifies"]:
        note = "⚠️ Currently a member but no longer qualifies — would be removed."
    elif not result["in_group"] and result["qualifies"]:
        note = "ℹ️ Not currently a member but qualifies — would be added."

    if result["filters"]:
        lines = [
            f"{'✅' if passed else '❌'} {desc}"
            for desc, passed in result["filters"]
        ]
    else:
        lines = ["_This group has no filters configured._"]
    if note:
        lines.insert(0, note)

    return _chunk_section(header, lines)


def _build_embeds(text_blocks, title, empty_message="Nothing to report."):
    """Paginate pre-formatted text blocks into Discord embeds (3900-char cap)."""
    if not text_blocks:
        return [Embed(title=title, description=empty_message, colour=0x95A5A6)]

    pages, buf, size = [], [], 0
    for block in text_blocks:
        if size + len(block) + 2 > 3900 and buf:
            pages.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(block)
        size += len(block) + 2
    if buf:
        pages.append("\n\n".join(buf))

    out = []
    for i, desc in enumerate(pages, 1):
        page_title = title + (f" ({i}/{len(pages)})" if len(pages) > 1 else "")
        out.append(Embed(title=page_title, description=desc, colour=0x3498DB))
    return out


def _report_embeds(character_name, results, group_name=None):
    """Build the embed pages for a finished evaluation."""
    blocks = []
    for r in results:
        blocks.extend(_group_block(r))

    if group_name:
        title = f"SigCheck: {character_name} — {group_name}"
        empty = (
            f"No Secure Group named `{group_name}` was found."
        )
    else:
        title = f"SigCheck: {character_name}"
        empty = "No Secure Groups are configured."

    # Single-group + qualifies → colour the (single) page green for a clear pass.
    embeds = _build_embeds(blocks, title, empty_message=empty)
    if group_name and len(results) == 1 and results[0]["qualifies"]:
        for e in embeds:
            e.colour = discord.Colour(0x2ECC71)
    return embeds


# ---- Shared command body ----------------------------------------------------

async def _run(character: str, group_name: str = None):
    """Resolve + evaluate. Returns (embeds, error_message)."""
    ec, user = await sync_to_async(_resolve_account)(character)
    if ec is None:
        return None, (
            f"`{character}` isn't a character known to Alliance Auth "
            "(unknown name, or never registered)."
        )
    if user is None:
        return None, (
            f"`{ec.character_name}` is known to Alliance Auth but isn't linked "
            "to any account, so its group eligibility can't be evaluated."
        )
    results = await sync_to_async(_evaluate)(user, group_name)
    return _report_embeds(ec.character_name, results, group_name), None


# ---- The cog ----------------------------------------------------------------

class SigCheck(commands.Cog):
    """Report whether a character meets Secure Groups requirements."""

    def __init__(self, bot):
        self.bot = bot

    # ---- prefix --------------------------------------------------------

    @commands.command(pass_context=True)
    async def sigcheck(self, ctx, *, character: str = None):
        """!sigcheck <character name>
           !sigcheck group:"Group Name" <character name>

        Without `group:` the report covers every Secure Group.
        """
        if not _channel_access(ctx.message.channel.id, ctx.author.id):
            return await ctx.message.add_reaction(chr(0x1F44E))  # 👎
        if not character:
            return await ctx.message.reply(
                'Usage: `!sigcheck <name>` or '
                '`!sigcheck group:"Group Name" <name>`'
            )

        group_name, name = _parse_prefix_args(character)
        if not name:
            return await ctx.message.reply(
                'Usage: `!sigcheck group:"Group Name" <name>`'
            )

        embeds, err = await _run(name, group_name)
        if err:
            return await ctx.message.reply(err)
        for e in embeds:
            await ctx.message.reply(embed=e)

    # ---- slash ---------------------------------------------------------

    @commands.slash_command(name="sigcheck", guild_ids=get_all_servers())
    @option("character", description="Exact character name", required=True)
    @option(
        "group",
        description="Limit to one Secure Group (default: report all groups)",
        required=False,
    )
    async def slash_sigcheck(self, ctx, character: str, group: str = None):
        try:
            await self._slash_impl(ctx, character, group)
        except Exception as e:
            # aadiscordbot's generic error handler swallows the traceback and
            # shows the user "Something Went Wrong" — log it here so the
            # discordbot log captures the real failure.
            logger.exception("sigcheck slash command failed")
            msg = (
                f"⚠️ SigCheck hit `{type(e).__name__}`. Ask an admin to check "
                "the discordbot log for the traceback."
            )
            try:
                if ctx.response.is_done():
                    await ctx.followup.send(msg, ephemeral=True)
                else:
                    await ctx.respond(msg, ephemeral=True)
            except Exception:
                logger.exception("sigcheck error reply also failed")

    async def _slash_impl(self, ctx, character, group):
        if not _channel_access(ctx.channel.id, ctx.user.id):
            return await ctx.respond(
                "This command isn't available to you in this channel.",
                ephemeral=True,
            )

        await ctx.defer()
        embeds, err = await _run(character, group)
        if err:
            return await ctx.respond(err, ephemeral=True)
        await ctx.respond(embed=embeds[0])
        for e in embeds[1:]:
            await ctx.followup.send(embed=e)


def _parse_prefix_args(text: str):
    """Parse `[group:"Name"] <character>` from a prefix command's free text.

    Returns (group_name_or_None, character_name). The group token may be
    quoted (`group:"a b"`) or a single bare word (`group:Foo`).
    """
    text = text.strip()
    if not text.lower().startswith("group:"):
        return None, text

    rest = text[len("group:"):].lstrip()
    if rest[:1] in ('"', "'"):
        quote = rest[0]
        end = rest.find(quote, 1)
        if end == -1:
            return rest[1:].strip(), ""  # unterminated quote: no name left
        group = rest[1:end]
        name = rest[end + 1:].strip()
    else:
        parts = rest.split(None, 1)
        group = parts[0] if parts else ""
        name = parts[1].strip() if len(parts) > 1 else ""
    return (group or None), name


def setup(bot):
    bot.add_cog(SigCheck(bot))
