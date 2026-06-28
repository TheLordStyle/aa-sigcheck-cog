"""SigCheck: report whether characters meet Secure Groups filter requirements.

Two commands:

* ``sigcheck <character>`` — resolve the owning Alliance Auth account and, for
  each Secure Group (``securegroups.models.SmartGroup``), report which of the
  group's smart filters the account meets (✅) and which it does not (❌), plus an
  overall qualify / doesn't-qualify verdict. A Secure Group only grants
  membership when *every* one of its filters passes, so the verdict is the AND
  of all filters.

* ``sigaudit <group>`` — audit one Secure Group across a population: who passes
  every filter and who doesn't, with the failing filters and last EVE login for
  those who don't. Restricted to Secure Groups that require membership of
  another group (a non-reversed ``UserInGroupFilter``); that prerequisite group
  bounds the population that gets checked.

Secure Groups operates at the account level (all of a user's characters are
considered by the individual filters), so a character check is really an account
check.
"""
import logging
from datetime import timezone as dt_timezone

import discord
from aadiscordbot.app_settings import get_all_servers
from discord import option
from discord.embeds import Embed
from discord.ext import commands

from asgiref.sync import sync_to_async

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.exceptions import ObjectDoesNotExist
from django.db import OperationalError, close_old_connections, connections
from django.db.models import Max
from django.utils import timezone

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter

from corptools.models import CharacterAudit
from securegroups.models import SmartGroup, UserInGroupFilter

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

def _with_fresh_db(fn, *args, **kwargs):
    """Run a sync ORM callable, recovering from stale DB connections.

    The discord bot is a long-lived process, so a Django DB connection can sit
    idle until MySQL drops it server-side (error 2006, "server has gone away").
    There's no request/response cycle here to reset connections, so we close
    any obsolete ones up front and, on a connection error, force every
    connection closed and retry once against a fresh one. The work is read-only,
    so the retry is safe.
    """
    close_old_connections()
    try:
        return fn(*args, **kwargs)
    except OperationalError:
        logger.warning("sigcheck: stale DB connection, reconnecting and retrying")
        for conn in connections.all():
            conn.close()
        return fn(*args, **kwargs)


def _gather(character: str, group_name: str = None):
    """Resolve the character and evaluate it in a single DB-bound unit."""
    ec, user = _resolve_account(character)
    if ec is None or user is None:
        return ec, user, []
    return ec, user, _evaluate(user, group_name)


async def _run(character: str, group_name: str = None):
    """Resolve + evaluate. Returns (embeds, error_message)."""
    ec, user, results = await sync_to_async(_with_fresh_db)(
        _gather, character, group_name
    )
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
    return _report_embeds(ec.character_name, results, group_name), None


# ---- Group audit (sigaudit) -------------------------------------------------
#
# Audit one Secure Group: who currently passes all its filters and who doesn't,
# with the failing filters and last EVE login for those who don't. Restricted to
# Secure Groups that require membership of another group (a non-reversed
# `UserInGroupFilter`); that prerequisite group bounds the population we check.

def _main_name(user):
    """Best display name for an account: main character, else username."""
    try:
        mc = user.profile.main_character
        if mc is not None:
            return mc.character_name
    except (ObjectDoesNotExist, AttributeError):
        pass
    return user.username


def _group_names(group_ids):
    return list(
        Group.objects.filter(id__in=list(group_ids))
        .values_list("name", flat=True)
    )


def _required_population(required_group_ids):
    """Accounts in any of the prerequisite groups — the audit population."""
    return list(
        User.objects
        .filter(groups__id__in=list(required_group_ids))
        .distinct()
        .select_related("profile__main_character")
    )


def _last_logins(user_ids):
    """Map user_id -> most recent last_known_login across all their characters."""
    rows = (
        CharacterAudit.objects
        .filter(character__character_ownership__user_id__in=user_ids)
        .values("character__character_ownership__user_id")
        .annotate(last=Max("last_known_login"))
    )
    return {
        r["character__character_ownership__user_id"]: r["last"] for r in rows
    }


def _fmt_login(dt):
    if dt is None:
        return "no recorded login"
    if timezone.is_naive(dt):
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _bulk_checks(filters, population_qs):
    """Pre-compute each filter's audit over the whole population.

    Returns {smartfilter_id: <audit_filter result>}. Mirrors how Secure Groups
    itself bulk-audits — far cheaper than calling process_filter per user. A
    filter that errors gets an empty map and falls back to process_filter below.
    """
    bulk = {}
    for sf, fo in filters:
        if fo is None:
            continue
        try:
            bulk[sf.id] = fo.audit_filter(population_qs)
        except Exception:
            logger.exception("audit_filter failed for filter %r", fo)
            bulk[sf.id] = {}
    return bulk


def _user_filter_result(sf, fo, user, bulk):
    """(passed, message) for one user/filter, preferring the bulk audit result.

    Falls back to process_filter(user) when the bulk map has no entry for this
    user — the same fallback Secure Groups uses in process_user().
    """
    try:
        entry = bulk[sf.id][user.id]
        return bool(entry["check"]), entry.get("message", "") or ""
    except Exception:
        try:
            return bool(fo.process_filter(user)), ""
        except Exception:
            logger.exception(
                "process_filter failed for filter %r on user %s", fo, user
            )
            return False, ""


def _audit_group(group_name: str):
    """Audit a Secure Group. Returns a status dict consumed by _run_audit."""
    sg = (
        SmartGroup.objects
        .select_related("group")
        .prefetch_related("filters")
        .filter(group__name__iexact=group_name.strip())
        .first()
    )
    if sg is None:
        return {"status": "no_group"}

    filters = []
    required_group_ids = set()
    for sf in sg.filters.all():
        fo = sf.filter_object
        filters.append((sf, fo))
        # The "requirement of being in another group": a positive (non-reversed)
        # UserInGroupFilter. Its groups define who we audit.
        if isinstance(fo, UserInGroupFilter) and not fo.reversed_logic:
            required_group_ids.update(
                fo.groups.all().values_list("id", flat=True)
            )

    if not required_group_ids:
        return {"status": "no_group_requirement", "group": sg.group.name}

    population = _required_population(required_group_ids)
    if not population:
        return {
            "status": "empty",
            "group": sg.group.name,
            "required_groups": _group_names(required_group_ids),
        }

    user_ids = [u.pk for u in population]
    population_qs = User.objects.filter(pk__in=user_ids)
    bulk = _bulk_checks(filters, population_qs)
    last_login = _last_logins(user_ids)

    passers, failers = [], []
    for user in population:
        failed = []
        for sf, fo in filters:
            if fo is None:
                failed.append("(unavailable filter)")
                continue
            ok, message = _user_filter_result(sf, fo, user, bulk)
            if not ok:
                desc = getattr(fo, "description", str(fo))
                failed.append(f"{desc} — {message}" if message else desc)
        name = _main_name(user)
        if failed:
            failers.append({
                "name": name,
                "failed": failed,
                "last_login": last_login.get(user.pk),
            })
        else:
            passers.append(name)

    return {
        "status": "ok",
        "group": sg.group.name,
        "required_groups": _group_names(required_group_ids),
        "population": len(population),
        "passers": sorted(passers, key=str.lower),
        "failers": sorted(failers, key=lambda f: str(f["name"]).lower()),
    }


def _audit_blocks(result):
    req = ", ".join(result["required_groups"]) or "the required group(s)"
    passers = result["passers"]
    failers = result["failers"]

    blocks = [
        f"__**{result['group']}** — group audit__\n"
        f"Population: {result['population']} account(s) in {req}\n"
        f"🟩 Pass: {len(passers)}    🟥 Fail: {len(failers)}"
    ]

    if passers:
        blocks.extend(_chunk_section(
            f"**🟩 Passing ({len(passers)})**",
            [f"• {n}" for n in passers],
        ))
    if failers:
        lines = []
        for f in failers:
            reasons = "; ".join(f["failed"])
            lines.append(
                f"• **{f['name']}** — last login {_fmt_login(f['last_login'])}\n"
                f" ❌ {reasons}"
            )
        blocks.extend(_chunk_section(
            f"**🟥 Failing ({len(failers)})**", lines,
        ))
    return blocks


def _auditable_groups():
    """Names of Secure Groups that `sigaudit` can audit.

    A group is auditable when it has at least one positive (non-reversed)
    `UserInGroupFilter` — the "requires membership of another group" rule that
    bounds the audit population.
    """
    names = []
    for sg in (
        SmartGroup.objects.select_related("group").prefetch_related("filters")
    ):
        for sf in sg.filters.all():
            fo = sf.filter_object
            if isinstance(fo, UserInGroupFilter) and not fo.reversed_logic:
                names.append(sg.group.name)
                break
    return sorted(names, key=str.lower)


def _audit_list_embeds(names):
    """Embeds listing the groups sigaudit can check."""
    intro = (
        "**SigAudit** — audit a Secure Group that requires membership of "
        "another group.\nUsage: `!sigaudit <group>` or `/sigaudit group:<group>`."
        f"\n\n__**Auditable groups ({len(names)})**__"
    )
    blocks = _chunk_section(intro, [f"• {n}" for n in names])
    return _build_embeds(blocks, "SigAudit — available groups")


async def _run_audit(group_name: str = None):
    """Run a group audit, or list auditable groups. Returns (embeds, error)."""
    if not group_name or not group_name.strip():
        names = await sync_to_async(_with_fresh_db)(_auditable_groups)
        if not names:
            return None, (
                "No Secure Groups can be audited — none require membership of "
                "another group (a non-reversed 'User in Group' filter)."
            )
        return _audit_list_embeds(names), None

    result = await sync_to_async(_with_fresh_db)(_audit_group, group_name)
    status = result["status"]
    if status == "no_group":
        return None, f"No Secure Group named `{group_name}` was found."
    if status == "no_group_requirement":
        return None, (
            f"`{result['group']}` can't be audited this way. This command only "
            "works for Secure Groups that require membership of another group "
            "(a non-reversed 'User in Group' filter); this group has no such "
            "requirement."
        )
    if status == "empty":
        req = ", ".join(result["required_groups"]) or "the required group(s)"
        return None, f"No accounts are in {req}, so there's nothing to audit."

    embeds = _build_embeds(_audit_blocks(result), f"SigAudit: {result['group']}")
    return embeds, None


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

    # ---- sigaudit: audit a whole group --------------------------------

    @commands.command(pass_context=True)
    async def sigaudit(self, ctx, *, group: str = None):
        """!sigaudit [Secure Group name]

        Audit a Secure Group that requires membership of another group: list who
        passes all its filters and, for those who don't, which filters failed and
        their last EVE login. With no group name, lists the auditable groups.
        """
        if not _channel_access(ctx.message.channel.id, ctx.author.id):
            return await ctx.message.add_reaction(chr(0x1F44E))  # 👎

        embeds, err = await _run_audit(group)
        if err:
            return await ctx.message.reply(err)
        for e in embeds:
            await ctx.message.reply(embed=e)

    @commands.slash_command(name="sigaudit", guild_ids=get_all_servers())
    @option(
        "group",
        description="Secure Group to audit (leave blank to list auditable groups)",
        required=False,
    )
    async def slash_sigaudit(self, ctx, group: str = None):
        try:
            await self._audit_impl(ctx, group)
        except Exception as e:
            logger.exception("sigaudit slash command failed")
            msg = (
                f"⚠️ SigAudit hit `{type(e).__name__}`. Ask an admin to check "
                "the discordbot log for the traceback."
            )
            try:
                if ctx.response.is_done():
                    await ctx.followup.send(msg, ephemeral=True)
                else:
                    await ctx.respond(msg, ephemeral=True)
            except Exception:
                logger.exception("sigaudit error reply also failed")

    async def _audit_impl(self, ctx, group):
        if not _channel_access(ctx.channel.id, ctx.user.id):
            return await ctx.respond(
                "This command isn't available to you in this channel.",
                ephemeral=True,
            )

        await ctx.defer()
        embeds, err = await _run_audit(group)
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
