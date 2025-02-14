import asyncio
import contextlib
import logging
import typing

import aiohttp
import aiohttp_retry
import attrs
import interactions as ipy
import orjson
from msgspec import ValidationError
from redis.asyncio.client import Pipeline
from tortoise.exceptions import DoesNotExist

import common.models as models
import common.utils as utils
import common.xbox_api as xbox_api
from common.microsoft_core import MicrosoftAPIException

MINECRAFT_TITLE_IDS = frozenset(
    {
        "1828326430",
        "2047319603",
        "2044456598",
        "896928775",
        "1739947436",
        "1810924247",
        "1944307183",
    }
)


def _convert_fields(value: tuple[str, ...] | None) -> tuple[str, ...]:
    return ("online", "last_seen") + value if value else ("online", "last_seen")


class RealmPlayersContainer:
    __slots__ = ("player_sessions", "fields")

    player_sessions: list[models.PlayerSession]
    fields: tuple[str, ...]

    def __init__(
        self,
        *,
        player_sessions: list[models.PlayerSession],
        fields: tuple[str, ...] | None = None,
    ) -> None:
        self.player_sessions = player_sessions
        self.fields = _convert_fields(fields)


class GamertagOnCooldown(Exception):
    # used by GamertagHandler to know when to switch to the backup
    def __init__(self) -> None:
        # i could make this anything since this should never be exposed
        # to the user, but who knows
        super().__init__("The gamertag handler is on cooldown.")


class GamertagInfo(typing.NamedTuple):
    gamertag: str
    device: str | None = None


@attrs.define()
class GamertagHandler:
    """
    A special class made to handle the complexities of getting gamertags
    from XUIDs.
    """

    bot: utils.RealmBotBase = attrs.field()
    sem: asyncio.Semaphore = attrs.field()
    xuids_to_get: tuple[str, ...] = attrs.field()
    openxbl_session: aiohttp_retry.RetryClient = attrs.field()
    gather_devices_for: set[str] = attrs.field(kw_only=True, factory=set)

    index: int = attrs.field(init=False, default=0)
    responses: list[xbox_api.ProfileResponse | xbox_api.PeopleHubResponse] = (
        attrs.field(init=False, factory=list)
    )
    AMOUNT_TO_GET: int = attrs.field(init=False, default=500)

    def __attrs_post_init__(self) -> None:
        # filter out empty strings, because that's possible somehow?
        self.xuids_to_get = tuple(x for x in self.xuids_to_get if x)

    async def get_gamertags(self, xuid_list: list[str]) -> None:
        # this endpoint is absolutely op and should rarely fail
        # franky, we usually don't need the backup thing, but you can't go wrong
        # having it

        try:
            people_bytes = await self.bot.xbox.fetch_people_batch(
                xuid_list, bypass_ratelimit=True
            )

        except MicrosoftAPIException as e:
            people_json = await e.resp.json(loads=orjson.loads)

            if people_json.get("code"):  # usually means ratelimited or invalid xuid
                description: str = people_json["description"]

                if description.startswith("Throttled"):  # ratelimited
                    raise GamertagOnCooldown() from e

                # otherwise, invalid xuid
                desc_split = description.split(" ")
                xuid_list.remove(desc_split[1])

                # after removing, try getting data again
                return await self.get_gamertags(xuid_list)

            if people_json.get("limitType"):  # ratelimit
                raise GamertagOnCooldown() from e

            else:
                raise

        self.responses.append(xbox_api.PeopleHubResponse.from_bytes(people_bytes))
        self.index += self.AMOUNT_TO_GET

    async def backup_get_gamertags(self) -> None:
        # openxbl is used throughout this, and its basically a way of navigating
        # the xbox live api in a more sane way than its actually laid out
        # while xbox-webapi-python can also do this without using a 3rd party service,
        # using openxbl can be more reliable at times as it has a generous 500 requests
        # per hour limit on the free tier and is not subject to ratelimits
        # however, there's no bulk xuid > gamertag option, and is a bit slow in general

        for xuid in self.xuids_to_get[self.index :]:
            async with self.openxbl_session.get(
                f"https://xbl.io/api/v2/account/{xuid}"
            ) as r:
                try:
                    r.raise_for_status()

                    self.responses.append(
                        await xbox_api.ProfileResponse.from_response(r)
                    )
                except (
                    aiohttp.ContentTypeError,
                    aiohttp.ClientResponseError,
                    ValidationError,
                ):
                    # can happen, if not rare
                    text = await r.text()
                    logging.getLogger("realms_bot").info(
                        f"Failed to get gamertag of user `{xuid}`.\nResponse code:"
                        f" {r.status}\nText: {text}"
                    )

            self.index += 1

    def _handle_new_gamertag(
        self,
        pipe: Pipeline,
        xuid: str,
        gamertag: str,
        dict_gamertags: dict[str, GamertagInfo],
        *,
        device: str | None = None,
    ) -> dict[str, GamertagInfo]:
        if not xuid or not gamertag:
            return dict_gamertags

        dict_gamertags[xuid] = GamertagInfo(gamertag, device)

        pipe.setex(name=xuid, time=utils.EXPIRE_GAMERTAGS_AT, value=gamertag)
        pipe.setex(name=f"rpl-{gamertag}", time=utils.EXPIRE_GAMERTAGS_AT, value=xuid)

        return dict_gamertags

    async def _execute_pipeline(self, pipe: Pipeline) -> None:
        try:
            await pipe.execute()
        finally:
            await pipe.reset()

    async def run(self) -> dict[str, GamertagInfo]:
        while self.index < len(self.xuids_to_get):
            current_xuid_list = list(
                self.xuids_to_get[self.index : self.index + self.AMOUNT_TO_GET]
            )

            async with self.sem:
                try:
                    await self.get_gamertags(current_xuid_list)
                except (GamertagOnCooldown, ValidationError, MicrosoftAPIException):
                    # hopefully fixes itself in 15 seconds
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.backup_get_gamertags(), timeout=15)

        dict_gamertags: dict[str, GamertagInfo] = {}
        pipe = self.bot.redis.pipeline()

        try:
            for response in self.responses:
                if isinstance(response, xbox_api.PeopleHubResponse):
                    for user in response.people:
                        device = None
                        if (
                            user.xuid in self.gather_devices_for
                            and user.presence_details
                        ):
                            if a_match := next(
                                (
                                    p
                                    for p in user.presence_details
                                    if p.title_id in MINECRAFT_TITLE_IDS
                                    and p.is_primary
                                ),
                                None,
                            ):
                                device = a_match.device

                            elif maybe_match := next(
                                (
                                    p
                                    for p in user.presence_details
                                    if "minecraft for" in p.presence_text.lower()
                                    and p.is_primary
                                ),
                                None,
                            ):
                                device = maybe_match.device
                                await utils.msg_to_owner(
                                    self.bot,
                                    (
                                        f"Possible device: {device} with title ID"
                                        f" {maybe_match.title_id} and presence text"
                                        f" {maybe_match.presence_text}"
                                    ),
                                )

                        dict_gamertags = self._handle_new_gamertag(
                            pipe,
                            user.xuid,
                            user.gamertag,
                            dict_gamertags,
                            device=device,
                        )
                else:
                    for user in response.profile_users:
                        xuid = user.id
                        try:
                            # really funny but efficient way of getting gamertag
                            # from this data
                            gamertag = next(
                                s.value for s in user.settings if s.id == "Gamertag"
                            )
                        except (KeyError, StopIteration):
                            continue

                        dict_gamertags = self._handle_new_gamertag(
                            pipe, xuid, gamertag, dict_gamertags
                        )

            # send data to pipeline in background
            self.bot.create_task(self._execute_pipeline(pipe))
        except:
            await pipe.reset()
            raise

        return dict_gamertags


async def can_run_playerlist(ctx: utils.RealmContext) -> bool:
    # simple check to see if a person can run the playerlist command
    try:
        guild_config = await ctx.fetch_config()
    except DoesNotExist:
        return False
    return bool(guild_config.realm_id)


async def invalidate_premium(
    bot: utils.RealmBotBase,
    config: models.GuildConfig,
) -> None:
    if config.valid_premium:
        config.premium_code = None
    config.live_playerlist = False
    config.fetch_devices = False
    config.live_online_channel = None

    await config.save()

    if config.realm_id:
        bot.live_playerlist_store[config.realm_id].discard(config.guild_id)
        if not await models.GuildConfig.filter(
            realm_id=config.realm_id, fetch_devices=True
        ).exists():
            bot.fetch_devices_for.discard(config.realm_id)


async def eventually_invalidate(
    bot: utils.RealmBotBase,
    guild_config: models.GuildConfig,
    limit: int = 3,
) -> None:
    if utils.TEST_MODE:
        return

    # the idea here is to invalidate autorunners that simply can't be run
    # there's a bit of generousity here, as the code gives a total of 3 tries
    # before actually doing it
    num_times = (
        await bot.redis.get(f"invalid-playerlist{limit}-{guild_config.guild_id}") or "0"
    )
    int_num_times = int(num_times) + 1

    if int_num_times >= limit:
        guild_config.playerlist_chan = None
        old_live_playerlist = guild_config.live_playerlist
        guild_config.live_playerlist = False
        await guild_config.save()
        await bot.redis.delete(f"invalid-playerlist{limit}-{guild_config.guild_id}")

        logging.getLogger("realms_bot").info(
            f"Unlinked guild {guild_config.guild_id} with {limit} invalidations."
        )

        if guild_config.realm_id and old_live_playerlist:
            bot.live_playerlist_store[guild_config.realm_id].discard(
                guild_config.guild_id
            )
    else:
        await bot.redis.set(
            f"invalid-playerlist{limit}-{guild_config.guild_id}",
            str(int_num_times),
            ex=limit * 86400,  # limit times day
        )


async def eventually_invalidate_live_online(
    bot: utils.RealmBotBase,
    guild_config: models.GuildConfig,
) -> None:
    if utils.TEST_MODE:
        return

    num_times = await bot.redis.incr(f"invalid-liveonline-{guild_config.guild_id}")

    if num_times >= 3:
        guild_config.live_online_channel = None
        await guild_config.save()
        await bot.redis.delete(f"invalid-liveonline-{guild_config.guild_id}")


async def fetch_playerlist_channel(
    bot: utils.RealmBotBase, guild: ipy.Guild, config: models.GuildConfig
) -> utils.GuildMessageable:
    try:
        chan = await guild.fetch_channel(config.playerlist_chan)  # type: ignore
    except ipy.errors.HTTPException as e:
        if e.status < 500:  # over 500 is a discord fault
            await eventually_invalidate(bot, config)
        raise ValueError() from None
    except TypeError:  # playerlist chan is none, do nothing
        raise ValueError() from None
    else:
        if not chan:
            # invalid channel
            await eventually_invalidate(bot, config)
            raise ValueError()

    return chan


async def fill_in_gamertags_for_sessions(
    bot: utils.RealmBotBase,
    player_sessions: list[models.PlayerSession],
    *,
    bypass_cache: bool = False,
    bypass_cache_for: set[str] | None = None,
) -> list[models.PlayerSession]:
    session_dict = {session.xuid: session for session in player_sessions}
    unresolved: list[str] = []

    if bypass_cache_for is None:
        bypass_cache_for = set()

    if not bypass_cache:
        async with bot.redis.pipeline() as pipeline:
            for session in player_sessions:
                if session.xuid not in bypass_cache_for:
                    pipeline.get(session.xuid)
                else:
                    # yes, this is dumb. yes, it works
                    pipeline.get("PURPOSELY_INVALID_KEY_AAAAAAAAAAAAAAAA")

            gamertag_list: list[str | None] = await pipeline.execute()

        for index, xuid in enumerate(session_dict.keys()):
            gamertag = gamertag_list[index]
            session_dict[xuid].gamertag = gamertag

            if not gamertag:
                unresolved.append(xuid)
    else:
        unresolved = list(session_dict.keys())
        bypass_cache_for = set(unresolved)

    if unresolved:
        gamertag_handler = GamertagHandler(
            bot,
            bot.pl_sem,
            tuple(unresolved),
            bot.openxbl_session,
            gather_devices_for=bypass_cache_for,
        )
        gamertag_dict = await gamertag_handler.run()

        for xuid, gamertag_info in gamertag_dict.items():
            session_dict[xuid].gamertag = gamertag_info.gamertag
            session_dict[xuid].device = gamertag_info.device

    return list(session_dict.values())
