"""Polymarket market discovery + CLOB market-channel websocket.

Discovery: gamma API by deterministic slug (btc-updown-5m-<wts>) for the next
few windows. Subscription: one ws connection carrying every active/upcoming
token; when the desired set changes we open a replacement connection before
closing the old one (the market channel takes its asset list at subscribe time).

Tracks per token: top-of-book, current tick size (incl. tick_size_change
events), and a rolling tape of prints for the paper fill engine.
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
    outcome_prices: tuple = None      # set post-resolution by the reconciler


@dataclass
class TokenState:
    best_bid: float = None
    best_bid_size: float = 0.0
    best_ask: float = None
    best_ask_size: float = 0.0
    tick: float = 0.01
    prints: deque = field(default_factory=lambda: deque(maxlen=2000))  # (ts, px, sz, side)
    last_book_ts: float = 0.0


class ClobFeed:
    def __init__(self, cfg):
        self.cfg = cfg
        self.markets = {}                 # wts -> Market
        self.tokens = {}                  # token_id -> TokenState
        self.token_owner = {}             # token_id -> (wts, 'up'|'down')
        self._session = None
        self._ws = None
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
            slug = f"{self.cfg.slug_prefix}-{wts}"
            url = f"{self.cfg.gamma_url}/markets?slug={slug}"
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                arr = await r.json()
            if not arr:
                continue
            m = arr[0]
            toks = json.loads(m["clobTokenIds"])
            outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m["outcomes"]
            up_idx = outcomes.index("Up")
            mk = Market(wts=wts, slug=slug, condition_id=m["conditionId"],
                        token_up=toks[up_idx], token_down=toks[1 - up_idx],
                        min_size=float(m.get("orderMinSize") or 5),
                        tick=float(m.get("orderPriceMinTickSize") or 0.01))
            self.markets[wts] = mk
            for tid, side in [(mk.token_up, "up"), (mk.token_down, "down")]:
                self.tokens[tid] = TokenState(tick=mk.tick)
                self.token_owner[tid] = (wts, side)
            log.info("discovered %s (cond %s...)", slug, mk.condition_id[:10])
            self._want_resub.set()
        # drop markets older than 2 windows past settlement
        horizon = now - 3 * self.cfg.window_secs
        for wts in [w for w in self.markets if w < horizon]:
            mk = self.markets.pop(wts)
            self.tokens.pop(mk.token_up, None)
            self.tokens.pop(mk.token_down, None)
            self.token_owner.pop(mk.token_up, None)
            self.token_owner.pop(mk.token_down, None)
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

    async def _run_ws(self, assets):
        async with self._session.ws_connect(self.cfg.clob_ws, heartbeat=10) as ws:
            await ws.send_json({"type": "market", "assets_ids": list(assets)})
            self._subscribed = assets
            self._want_resub.clear()
            log.info("clob ws subscribed to %d tokens", len(assets))
            recv = asyncio.ensure_future(ws.receive())
            resub = asyncio.ensure_future(self._want_resub.wait())
            pinger = asyncio.ensure_future(self._pinger(ws))
            try:
                while True:
                    done, _ = await asyncio.wait({recv, resub},
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if resub in done:
                        if frozenset(self.tokens.keys()) != self._subscribed:
                            return  # exit; outer loop reconnects with new set
                        self._want_resub.clear()
                        resub = asyncio.ensure_future(self._want_resub.wait())
                    if recv in done:
                        msg = recv.result()
                        if msg.type in (aiohttp.WSMsgType.PING, aiohttp.WSMsgType.PONG):
                            pass
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            if msg.data != "PONG":
                                self._handle(msg.data)
                        else:
                            raise RuntimeError(f"ws closed ({msg.type})")
                        recv = asyncio.ensure_future(ws.receive())
            finally:
                recv.cancel()
                resub.cancel()
                pinger.cancel()

    @staticmethod
    async def _pinger(ws):
        # the CLOB server drops connections without an application-level ping
        while True:
            await asyncio.sleep(9)
            await ws.send_str("PING")

    def _handle(self, raw):
        data = json.loads(raw)
        events = data if isinstance(data, list) else [data]
        now = time.time()
        for ev in events:
            et = ev.get("event_type")
            tid = ev.get("asset_id")
            st = self.tokens.get(tid)
            if st is None:
                continue
            if et == "book":
                bids = ev.get("bids") or []
                asks = ev.get("asks") or []
                if bids:
                    top = max(bids, key=lambda x: float(x["price"]))
                    st.best_bid, st.best_bid_size = float(top["price"]), float(top["size"])
                else:
                    st.best_bid, st.best_bid_size = None, 0.0
                if asks:
                    top = min(asks, key=lambda x: float(x["price"]))
                    st.best_ask, st.best_ask_size = float(top["price"]), float(top["size"])
                else:
                    st.best_ask, st.best_ask_size = None, 0.0
                st.last_book_ts = now
            elif et == "price_change":
                for ch in ev.get("changes", [ev]):
                    try:
                        px, sz = float(ch["price"]), float(ch["size"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    side = (ch.get("side") or "").upper()
                    if side == "BUY":
                        if st.best_bid is None or px >= st.best_bid:
                            st.best_bid = px if sz > 0 else st.best_bid
                            if px == st.best_bid:
                                st.best_bid_size = sz
                    elif side == "SELL":
                        if st.best_ask is None or px <= st.best_ask:
                            st.best_ask = px if sz > 0 else st.best_ask
                            if px == st.best_ask:
                                st.best_ask_size = sz
                st.last_book_ts = now
            elif et == "tick_size_change":
                try:
                    st.tick = float(ev.get("new_tick_size"))
                    wts, _ = self.token_owner.get(tid, (None, None))
                    if wts in self.markets:
                        self.markets[wts].tick = st.tick
                    log.info("tick change %s -> %s", tid[:16], st.tick)
                except (TypeError, ValueError):
                    pass
            elif et in ("last_trade_price", "trade"):
                try:
                    px = float(ev.get("price"))
                    sz = float(ev.get("size") or 0)
                except (TypeError, ValueError):
                    continue
                side = (ev.get("side") or "").upper()
                st.prints.append((now, px, sz, side))

    # ---------------- helpers ----------------
    def market_for(self, wts):
        return self.markets.get(wts)

    def state(self, token_id):
        return self.tokens.get(token_id)

    async def fetch_outcome(self, mk: Market):
        """Post-settlement truth from gamma (for the paper ledger reconciler)."""
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
                        winner = outcomes[[float(p) for p in prices].index(1.0)]
                        return winner.lower()          # 'up' | 'down'
            except Exception as e:  # noqa: BLE001
                log.debug("outcome fetch failed %s: %s", mk.slug, e)
        return None
