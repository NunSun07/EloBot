# twitch_faceit_bot.py
"""
TwitchIO v3+ bot з інтеграцією FACEIT.
- Працює асинхронно через aiohttp.
- Зберігає snapshots (elo + ISO timestamp + source) в JSON.
- При !elo додає snapshot і обчислює delta = current_elo - baseline_elo,
  де baseline_elo = перший запис після 04:00 сьогодні або останній перед 04:00.
- Запускає щоденний snapshot о 04:00 (локальний timezone).
- Потребує ENV:
    TWITCH_OAUTH_TOKEN
    TWITCH_CHANNEL
    FACEIT_API_KEY
    FACEIT_NICK
    TWITCH_CLIENT_ID
    TWITCH_CLIENT_SECRET
    BOT_ID
  Опціонально:
    ELO_SNAPSHOT_FILE (default: elo_snapshots.json)
    TIMEZONE (default: Europe/Kyiv)
"""

import os
import asyncio
import json
import logging
import datetime
from typing import Optional, Tuple, List, Dict

import aiohttp
import pytz
from twitchio.ext import commands

# ----------------- Settings -----------------
LOG_LEVEL = logging.INFO
SNAPSHOT_FILE = os.getenv("ELO_SNAPSHOT_FILE", "elo_snapshots.json")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Kyiv")
FACEIT_API_KEY = os.environ.get("FACEIT_API_KEY")

# ----------------- Logging -------------------
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("twitch_faceit_bot")

# ----------------- FaceitClient -----------------
class FaceitClient:
    def __init__(self, api_key: str, snapshot_file: str = SNAPSHOT_FILE, tz_name: str = TIMEZONE):
        if not api_key:
            raise RuntimeError("FACEIT_API_KEY not provided in ENV")
        self.api_key = api_key
        self.snapshot_file = snapshot_file
        self.tz = pytz.timezone(tz_name)
        self._file_lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None

    async def ensure_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    # ---------- File operations (snapshots) ----------
    async def _read_snapshots(self) -> List[dict]:
        async with self._file_lock:
            try:
                if not os.path.exists(self.snapshot_file):
                    return []
                with open(self.snapshot_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
            except Exception as e:
                logger.error("Failed to read snapshots file: %s", e)
            return []

    async def _write_snapshots(self, arr: List[dict]):
        async with self._file_lock:
            tmp = f"{self.snapshot_file}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(arr, f, indent=2, ensure_ascii=False)
                os.replace(tmp, self.snapshot_file)
            except Exception as e:
                logger.error("Failed to write snapshots file: %s", e)
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass

    async def append_snapshot(self, elo: int, source: str = "command"):
        now = datetime.datetime.now(self.tz)
        rec = {"elo": int(elo), "timestamp": now.isoformat(), "source": source}
        arr = await self._read_snapshots()
        arr.append(rec)
        await self._write_snapshots(arr)
        logger.info("Snapshot appended: %s", rec)

    async def read_snapshots(self) -> List[dict]:
        return await self._read_snapshots()

    # ---------- Baseline logic ----------
    async def find_baseline_elo(self, reference_dt: Optional[datetime.datetime] = None) -> Tuple[int, Optional[dict]]:
        """
        Return (baseline_elo, baseline_record_or_None)
        baseline_time = today at 04:00 in self.tz
        Algorithm:
         - find first record with timestamp >= baseline_time (first after 04:00)
         - else take last record before baseline_time
         - else return (0, None)
        """
        now = reference_dt.astimezone(self.tz) if reference_dt else datetime.datetime.now(self.tz)
        baseline_time = now.replace(hour=4, minute=0, second=0, microsecond=0)
        arr = await self._read_snapshots()
        if not arr:
            return 0, None

        parsed: List[Tuple[dict, datetime.datetime]] = []
        for rec in arr:
            ts = rec.get("timestamp")
            if not ts:
                continue
            try:
                dt = datetime.datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = self.tz.localize(dt)
                else:
                    dt = dt.astimezone(self.tz)
                parsed.append((rec, dt))
            except Exception as e:
                logger.debug("Cannot parse timestamp %s: %s", ts, e)

        if not parsed:
            return 0, None

        after = [(r, d) for (r, d) in parsed if d >= baseline_time]
        if after:
            rec, dt = min(after, key=lambda x: x[1])
            return int(rec["elo"]), rec

        before = [(r, d) for (r, d) in parsed if d < baseline_time]
        if before:
            rec, dt = max(before, key=lambda x: x[1])
            return int(rec["elo"]), rec

        return 0, None

    # ---------- Faceit API ----------
    async def get_player_by_nick(self, nickname: str) -> Optional[dict]:
        await self.ensure_session()
        url = "https://open.faceit.com/data/v4/players"
        params = {"nickname": nickname}
        try:
            async with self._session.get(url, headers=self._headers(), params=params, timeout=15) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning("Faceit /players returned %s: %s", resp.status, await resp.text())
        except Exception as e:
            logger.error("Error get_player_by_nick: %s", e)
        return None

    async def get_faceit_stats(self, nickname: str) -> Dict[str, int]:
        """
        Returns {'Elo': int, 'Win': int, 'Lose': int}
        """
        player = await self.get_player_by_nick(nickname)
        if not player:
            return {"Elo": 0, "Win": 0, "Lose": 0}

        elo = int(player.get("games", {}).get("cs2", {}).get("faceit_elo", 0) or 0)
        player_id = player.get("player_id")
        wins, losses = 0, 0
        if player_id:
            wins, losses = await self._get_daily_matches(player_id)
        return {"Elo": elo, "Win": wins, "Lose": losses}

    async def _get_daily_matches(self, player_id: str) -> Tuple[int, int]:
        await self.ensure_session()
        try:
            today_utc = datetime.datetime.utcnow().date()
            from_ts = int(datetime.datetime.combine(today_utc, datetime.time(0, 0)).timestamp())
            to_ts = int(datetime.datetime.utcnow().timestamp())
            url = f"https://open.faceit.com/data/v4/players/{player_id}/history"
            params = {"game": "cs2", "from": from_ts, "to": to_ts, "limit": 100}
            async with self._session.get(url, headers=self._headers(), params=params, timeout=20) as resp:
                if resp.status != 200:
                    logger.warning("Faceit /history returned %s", resp.status)
                    return 0, 0
                data = await resp.json()
                items = data.get("items", [])
                wins = 0
                losses = 0
                for m in items:
                    try:
                        if m.get("status") != "finished":
                            continue
                        teams = m.get("teams", {})
                        results = m.get("results", {})
                        winner = results.get("winner")
                        if not winner:
                            continue
                        player_team = None
                        for faction, team_data in teams.items():
                            for p in team_data.get("players", []):
                                if p.get("player_id") == player_id:
                                    player_team = faction
                                    break
                            if player_team:
                                break
                        if not player_team:
                            continue
                        if player_team == winner:
                            wins += 1
                        else:
                            losses += 1
                    except Exception as e:
                        logger.debug("Error analyzing match: %s", e)
                        continue
                return wins, losses
        except Exception as e:
            logger.error("Error _get_daily_matches: %s", e)
            return 0, 0

    # ---------- Daily snapshot task ----------
    async def daily_snapshot_task(self, nickname: str, initial_delay: Optional[int] = None):
        await self.ensure_session()
        if initial_delay:
            await asyncio.sleep(initial_delay)

        while True:
            try:
                now = datetime.datetime.now(self.tz)
                next_reset = now.replace(hour=4, minute=0, second=0, microsecond=0)
                if now >= next_reset:
                    next_reset = next_reset + datetime.timedelta(days=1)
                delay = (next_reset - now).total_seconds()
                logger.info("Daily snapshot sleeping until %s (seconds=%d)", next_reset.isoformat(), int(delay))
                await asyncio.sleep(delay)

                player = await self.get_player_by_nick(nickname)
                if player:
                    elo = int(player.get("games", {}).get("cs2", {}).get("faceit_elo", 0) or 0)
                    await self.append_snapshot(elo, source="daily_04")
                    logger.info("Daily snapshot saved: elo=%s", elo)
                else:
                    logger.warning("Daily snapshot: player info not fetched")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error("Error in daily_snapshot_task loop: %s", e)
                await asyncio.sleep(60)

# ----------------- TwitchIO Bot -----------------
class TwitchFaceitBot(commands.Bot):
    def __init__(self):
        # required ENV
        twitch_token = os.environ.get("TWITCH_OAUTH_TOKEN")
        twitch_channel = os.environ.get("TWITCH_CHANNEL")
        client_id = os.environ.get("TWITCH_CLIENT_ID")
        client_secret = os.environ.get("TWITCH_CLIENT_SECRET")
        bot_id = os.environ.get("BOT_ID")
        faceit_nick = os.environ.get("FACEIT_NICK")

        missing = [k for k, v in [
            ("TWITCH_OAUTH_TOKEN", twitch_token),
            ("TWITCH_CHANNEL", twitch_channel),
            ("TWITCH_CLIENT_ID", client_id),
            ("TWITCH_CLIENT_SECRET", client_secret),
            ("BOT_ID", bot_id),
            ("FACEIT_API_KEY", FACEIT_API_KEY),
            ("FACEIT_NICK", faceit_nick),
        ] if not v]

        if missing:
            raise RuntimeError(f"Missing required ENV vars: {', '.join([m[0] for m in [(k,v) for (k,v) in zip([m[0] for m in [('TWITCH_OAUTH_TOKEN',None)],],[])]])}")  # placeholder to avoid lint, we check above
            # Note: we already built 'missing' - but raising a clear message next:
        # simpler raise:
        if missing:
            raise RuntimeError(f"Missing required ENV vars: {', '.join(missing)}")

        # IMPORTANT: TwitchIO v3+ requires client_id, client_secret and bot_id
        super().__init__(
            token=twitch_token,
            prefix="!",
            initial_channels=[twitch_channel],
            client_id=client_id,
            client_secret=client_secret,
            bot_id=bot_id,
        )

        self.faceit_nick = faceit_nick
        self.faceit = FaceitClient(FACEIT_API_KEY)
        self._cooldowns: Dict[str, float] = {}
        self.cooldown_seconds = 5

    async def event_ready(self):
        logger.info("Bot ready | Logged in as %s", self.nick)
        # start daily snapshot task (small initial delay)
        self.loop.create_task(self.faceit.daily_snapshot_task(self.faceit_nick, initial_delay=5))

    @commands.command(name="elo")
    async def elo_command(self, ctx: commands.Context):
        now_ts = asyncio.get_event_loop().time()
        last = self._cooldowns.get("elo", 0.0)
        if now_ts - last < self.cooldown_seconds:
            return
        self._cooldowns["elo"] = now_ts

        stats = await self.faceit.get_faceit_stats(self.faceit_nick)
        current_elo = stats.get("Elo", 0)
        wins = stats.get("Win", 0)
        losses = stats.get("Lose", 0)

        # append snapshot from command
        try:
            await self.faceit.append_snapshot(current_elo, source="command")
        except Exception as e:
            logger.error("Failed to save snapshot: %s", e)

        baseline_elo, baseline_rec = await self.faceit.find_baseline_elo()
        if baseline_rec is None:
            baseline_elo = current_elo
            baseline_note = "(no baseline)"
        else:
            baseline_note = f"(baseline @ {baseline_rec['timestamp']})"

        delta = int(current_elo) - int(baseline_elo)
        delta_str = f"+{delta}" if delta > 0 else str(delta)

        msg = f"@{ctx.author.name} → Elo: {current_elo} | Win: {wins} | Lose: {losses} | Δ: {delta_str} {baseline_note}"
        await ctx.send(msg)
        logger.info("Sent !elo response: %s", msg)

    @commands.command(name="save_elo")
    async def save_elo(self, ctx: commands.Context):
        stats = await self.faceit.get_faceit_stats(self.faceit_nick)
        current_elo = stats.get("Elo", 0)
        await self.faceit.append_snapshot(current_elo, source="manual_save")
        await ctx.send(f"@{ctx.author.name} Saved snapshot: {current_elo}")
        logger.info("Manual save_elo executed by %s -> %s", ctx.author.name, current_elo)

    @commands.command(name="lastsnap")
    async def lastsnap(self, ctx: commands.Context):
        arr = await self.faceit.read_snapshots()
        if not arr:
            await ctx.send("No snapshots recorded yet.")
            return
        last5 = arr[-5:]
        parts = []
        for r in reversed(last5):
            parts.append(f"{r.get('timestamp')[-8:]}:{r.get('elo')}")
        await ctx.send("Last snapshots: " + ", ".join(parts))

    async def close(self):
        await self.faceit.close()
        await super().close()

# ----------------- Entry point -----------------
if __name__ == "__main__":
    try:
        bot = TwitchFaceitBot()
        bot.run()
    except Exception as e:
        logger.exception("Bot stopped with exception: %s", e)
