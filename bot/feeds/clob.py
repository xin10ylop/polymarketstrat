"""Polymarket market discovery + CLOB market-channel websocket.

Discovery: gamma API by deterministic slug (btc-updown-5m-<wts>) for the next
few windows. Subscription: one ws connection carrying every active/upcoming
token; when the desired set changes we open a replacement connection.

Book state is a full price ladder per token (dict price->size), seeded from
`book` snapshots and updated by absolute-size `price_change` deltas, so best
bid/ask can move in BOTH directions and cancelled levels disappear. On every
reconnect the books are resynced via REST — websocket gaps can otherwise leave
stale tops that fake paper liquidity.

Prints carry a monotonically increasing per-token sequence number so fill
cursors survive deque eviction.
"""
import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger("clob")


@dataclass
class Market:
    wts: int
    slug: str
    condition_id: str
    token_up: str
    token_down: str
    tick: float = 0.01
    min_size: float = 5.0


@dataclass
class TokenState:
    bids: dict = field(default_factory=dict)      # price -> size
    asks: dict = field(default_factory=dict)
    tick: float = 0.01
    prints: deque = field(default_factory=lambda: deque(maxlen=4000))  # (seq, ts, px, sz, side)
    print_seq: int = 0
    last_book_ts: float = 0.0

    @property
    def best_bid(self):
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self):
        return min(self.asks) if self.asks else None

    @property
    def best_bid_size(self):
        b = self.best_bid
        return self.bids.get(b, 0.0) if b is not None else 0.0

    @property
    def best_ask_size(self):
        a = self.best_ask
        return self.asks.get(a, 0.0) if a is not None else 0.0

    def book_fresh(self, max_age):
        return time.time() - self.last_book_ts <= max_age


class ClobFeed:
    def __init__(self, cfg):
        self.cfg = cfg
        self.markets = {}                 # wts -> Market
        self.tokens = {}                  # token_id -> TokenState
        self.token_owner = {}             # token_id -> (wts, 'up'|'down')
        self._session = None
        self._subscribed = frozenset()
        self._want_resub = asyncio.Event()

    # ---------------- discovery ----------------
    async def discover_loop(self):
        while True:
            try:
                await self._discover()
            except Exception as e:  # noqa: BLE001
                log.warning("discovery error: %s", e)
            await asyncio.sleep(45)

    async def _discover(self):
        now = int(time.time())
        cur = now - now % self.cfg.window_secs
        wanted = [cur + i * self.cfg.window_secs
                  for i in range(0, self.cfg.discovery_lookahead + 1)]
        for wts in wanted:
            if wts in self.markets:
                continue
            from bot.config import et_slug_ambiguous, slug_for
            if et_slug_ambiguous(self.cfg, wts):
                log.warning("skipping DST-ambiguous hourly window w%s", wts)
                continue
            slug = slug_for(self.cfg, wts)
            url = f"{self.cfg.gamma_url}/markets?slug={slug}"
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                arr = await r.json()
            if not arr:
                continue
            m = arr[0]
            # THE slug-bug firewall: whatever slug scheme produced this market,
            # its endDate must equal this window's close — otherwise we found a
            # DIFFERENT market (DST naming, format drift) and trading it would
            # be a silent wrong-market position. Skip and log instead.
            end_ts = self._parse_end(m.get("endDate"))
            if end_ts != wts + self.cfg.window_secs:
                log.error("slug %s endDate=%s != w%s close — WRONG MARKET, skipping",
                          slug, end_ts, wts)
                continue
            if any(x.condition_id == m["conditionId"] for x in self.markets.values()):
                log.error("slug %s condition already registered under another "
                          "window — skipping duplicate", slug)
                continue
            toks = json.loads(m["clobTokenIds"])
            outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m["outcomes"]
            try:
                up_idx = outcomes.index("Up")
            except ValueError:
                log.error("slug %s unexpected outcomes %s — skipping", slug, outcomes)
                continue
            mk = Market(wts=wts, slug=slug, condition_id=m["conditionId"],
                        token_up=toks[up_idx], token_down=toks[1 - up_idx],
                        min_size=float(m.get("orderMinSize") or 5),
                        tick=float(m.get("orderPriceMinTickSize") or 0.01))
            self.markets[wts] = mk
            for tid in (mk.token_up, mk.token_down):
                # setdefault: NEVER wipe the live book of a token we already
                # track — a reset ladder rebuilt from deltas fakes liquidity
                self.tokens.setdefault(tid, TokenState(tick=mk.tick))
                self.token_owner[tid] = (wts, "up" if tid == mk.token_up else "down")
            log.info("discovered %s (cond %s...)", slug, mk.condition_id[:10])
            self._want_resub.set()
            await self._resync_books([mk.token_up, mk.token_down])
            await self._check_fee_meta(mk)
        horizon = now - 3 * self.cfg.window_secs
        for wts in [w for w in self.markets if w < horizon]:
            mk = self.markets.pop(wts)
            for tid in (mk.token_up, mk.token_down):
                self.tokens.pop(tid, None)
                self.token_owner.pop(tid, None)
            self._want_resub.set()

    # ---------------- websocket ----------------
    async def ws_loop(self):
        self._session = aiohttp.ClientSession(trust_env=True)
        try:
            while True:
                desired = frozenset(self.tokens.keys())
                if not desired:
                    await asyncio.sleep(1)
                    continue
                try:
                    await self._run_ws(desired)
                except Exception as e:  # noqa: BLE001
                    log.warning("clob ws dropped: %s; reconnecting", e)
                    await asyncio.sleep(1)
        finally:
            await self._session.close()

    @staticmethod
    def _parse_end(end):
        """ISO endDate -> epoch seconds; None/parse failure -> -1 (never matches)."""
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp())
        except Exception:  # noqa: BLE001
            return -1

    async def _check_fee_meta(self, mk):
        """Once per process: verify CLOB fee metadata still matches the
        modelled curve's basis (maker/taker base_fee 1000). Drift = loud log."""
        if getattr(self, "_fee_checked", False):
            return
        self._fee_checked = True
        try:
            async with self._session.get(
                    f"{self.cfg.clob_url}/markets/{mk.condition_id}",
                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                meta = await r.json()
            mb, tb = meta.get("maker_base_fee"), meta.get("taker_base_fee")
            if (mb, tb) != (1000, 1000):
                log.error("FEE METADATA DRIFT: maker_base_fee=%s taker_base_fee=%s "
                          "(modelled 0.07*p*(1-p) assumed 1000/1000) — re-verify "
                          "the fee curve before trusting PnL", mb, tb)
            else:
                log.info("fee metadata check OK (base_fee 1000/1000)")
        except Exception as e:  # noqa: BLE001
            log.warning("fee metadata check failed (non-fatal): %s", e)

    async def _resync_books(self, assets):
        """REST snapshot of every subscribed book (heals ws-gap staleness)."""
        for tid in assets:
            st = self.tokens.get(tid)
            if st is None:
                continue
            try:
                async with self._session.get(
                        f"{self.cfg.clob_url}/book?token_id={tid}",
                        timeout=aiohttp.ClientTimeout(total=8)) as r:
                    book = await r.json()
                self._apply_snapshot(st, book.get("bids") or [], book.get("asks") or [])
            except Exception as e:  # noqa: BLE001
                log.debug("book resync failed %s: %s", tid[:12], e)

    @staticmethod
    def _apply_snapshot(st, bids, asks):
        st.bids = {float(x["price"]): float(x["size"]) for x in bids if float(x["size"]) > 0}
        st.asks = {float(x["price"]): float(x["size"]) for x in asks if float(x["size"]) > 0}
        st.last_book_ts = time.time()

    async def _run_ws(self, assets):
        async with self._session.ws_connect(self.cfg.clob_ws, heartbeat=10) as ws:
            await ws.send_json({"type": "market", "assets_ids": list(assets)})
            self._subscribed = assets
            self._want_resub.clear()
            if frozenset(self.tokens.keys()) != assets:
                self._want_resub.set()   # discovery raced the handshake
            log.info("clob ws subscribed to %d tokens", len(assets))
            await self._resync_books(assets)
            recv = asyncio.ensure_future(ws.receive())
            resub = asyncio.ensure_future(self._want_resub.wait())
            pinger = asyncio.ensure_future(self._pinger(ws))
            try:
                while True:
                    done, _ = await asyncio.wait({recv, resub},
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if recv in done:                     # always drain recv first
                        msg = recv.result()
                        if msg.type in (aiohttp.WSMsgType.PING, aiohttp.WSMsgType.PONG):
                            pass
                        elif msg.type == aiohttp.WSMsgType.CLOSE:
                            raise RuntimeError(f"server close (code={msg.data})")
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            if msg.data and msg.data != "PONG":
                                self._handle(msg.data)
                        else:
                            raise RuntimeError(f"ws closed ({msg.type})")
                        recv = asyncio.ensure_future(ws.receive())
                    if resub in done:
                        if frozenset(self.tokens.keys()) != self._subscribed:
                            return                       # reconnect with new set
                        self._want_resub.clear()
                        resub = asyncio.ensure_future(self._want_resub.wait())
            finally:
                recv.cancel()
                resub.cancel()
                pinger.cancel()

    @staticmethod
    async def _pinger(ws):
        while True:
            await asyncio.sleep(5)
            await ws.send_str("PING")

    def _handle(self, raw):
        try:
            data = json.loads(raw)
        except ValueError:
            return
        events = data if isinstance(data, list) else [data]
        now = time.time()
        for ev in events:
            et = ev.get("event_type")
            tid = ev.get("asset_id")
            st = self.tokens.get(tid)
            if st is None:
                continue
            if et == "book":
                self._apply_snapshot(st, ev.get("bids") or [], ev.get("asks") or [])
            elif et == "price_change":
                for ch in ev.get("changes", [ev]):
                    try:
                        px, sz = float(ch["price"]), float(ch["size"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    side = (ch.get("side") or "").upper()
                    ladder = st.bids if side == "BUY" else st.asks if side == "SELL" else None
                    if ladder is None:
                        continue
                    if sz > 0:
                        ladder[px] = sz
                    else:
                        ladder.pop(px, None)
                st.last_book_ts = now
            elif et == "tick_size_change":
                try:
                    new_tick = float(ev.get("new_tick_size"))
                except (TypeError, ValueError):
                    continue
                wts, _side = self.token_owner.get(tid, (None, None))
                mk = self.markets.get(wts)
                if mk:
                    mk.tick = new_tick
                    for t2 in (mk.token_up, mk.token_down):   # both tokens share the regime
                        if t2 in self.tokens:
                            self.tokens[t2].tick = new_tick
                log.info("tick change w%s -> %s", wts, new_tick)
            elif et in ("last_trade_price", "trade"):
                try:
                    px = float(ev.get("price"))
                    sz = float(ev.get("size") or 0)
                except (TypeError, ValueError):
                    continue
                st.print_seq += 1
                st.prints.append((st.print_seq, now, px, sz, (ev.get("side") or "").upper()))

    # ---------------- helpers ----------------
    def market_for(self, wts):
        return self.markets.get(wts)

    def state(self, token_id):
        return self.tokens.get(token_id)

    async def fetch_outcome(self, mk: Market):
        url = f"{self.cfg.gamma_url}/markets?slug={mk.slug}"
        for extra in ("", "&closed=true"):
            try:
                async with self._session.get(url + extra,
                                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                    arr = await r.json()
                if arr:
                    op = arr[0].get("outcomePrices")
                    prices = json.loads(op) if isinstance(op, str) else op
                    if prices and float(max(prices, key=float)) == 1.0:
                        outcomes = arr[0].get("outcomes")
                        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                        return outcomes[[float(p) for p in prices].index(1.0)].lower()
            except Exception as e:  # noqa: BLE001
                log.debug("outcome fetch failed %s: %s", mk.slug, e)
        return None
