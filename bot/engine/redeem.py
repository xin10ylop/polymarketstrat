"""Collect winnings: the on-chain redemption path (launch blocker #1).

WHY THIS EXISTS. A won position pays out only when its conditional tokens
are redeemed against the ConditionalTokens contract; the CLOB never does
it for you. Without this loop a live bot is "profitable" while its USDC
balance only ever falls — every win parks collateral in redeemable
tokens until the wallet starves. (Measured alternative: selling winners
at 0.95 before resolution recycles collateral through the book, but the
exit ladder priced that at -2.55c/decision — expensive rent. So: redeem.)

THE MECHANICS, verified against the venue 2026-08-20: the 5m/15m up-down
markets are PLAIN BINARY CTF markets (gamma negRisk=false), so
redemption is ConditionalTokens.redeemPositions(collateral, 0x0,
conditionId, [1, 2]) on Polygon — both index sets in one call; the
losing side pays zero and redeeming it is harmless. conditionId comes
from gamma by slug at redemption time (not latency-critical, no schema
change). LIVE-SETUP REQUIREMENT recorded in the runbook: the account
must be an EOA (PM_SIGNATURE_TYPE=0) — Magic/proxy accounts hold their
tokens IN THE PROXY and need Polymarket's relayer to redeem; an EOA can
call the contract directly and only needs a little POL for gas.

SHADOW-FIRST. REDEEM_DRY defaults ON even in live mode: the loop finds
every redeemable window and writes a redeem_dry event with what it WOULD
have done — the shadow phase on the EU box verifies the whole pipeline
(discovery, conditionId resolution, amounts) against the venue's UI
before the first real transaction. Flipping REDEEM_DRY=0 is a deliberate
human step listed in the live env block.

Failure discipline: a failed send writes redeem_error and the window
stays pending — retried every pass, never dropped, never crashes the
loop (it runs supervised). web3 is imported lazily inside the send path
only, so paper boxes never need the dependency.
"""
import asyncio
import json
import logging
import time

from bot.config import slug_for

log = logging.getLogger("redeem")


class Redeemer:
    def __init__(self, cfg, ledger):
        self.cfg, self.ledger = cfg, ledger
        self._cids = {}                # wts -> conditionId

    # ---- what still needs redeeming ----
    def handled(self):
        """Windows already redeemed (or dry-logged, so dry mode reports
        each window once instead of every pass)."""
        out = set()
        for (detail,) in self.ledger.db.execute(
                "SELECT detail FROM events WHERE kind IN "
                "('redeemed', 'redeem_dry')"):
            tok = detail.split()[0] if detail else ""
            if tok.startswith("w") and tok[1:].isdigit():
                out.add(int(tok[1:]))
        return out

    def pending(self):
        done = self.handled()
        rows = self.ledger.winning_settled_windows(self.cfg.redeem_min_age_s)
        return [(w, sz) for w, sz in rows if w not in done]

    # ---- one pass ----
    async def once(self):
        todo = self.pending()[: self.cfg.redeem_max_batch]
        for wts, sz in todo:
            try:
                cid = await self._condition_id(wts)
            except Exception as e:  # noqa: BLE001
                log.warning("w%s conditionId lookup failed: %s", wts, e)
                continue
            if cid is None:
                log.warning("w%s no conditionId on gamma yet", wts)
                continue
            if self.cfg.redeem_dry or self.cfg.live_shadow:
                self.ledger.event(
                    "redeem_dry", f"w{wts} would redeem {sz:.0f} winning "
                    f"shares, conditionId {cid}")
                log.info("DRY redeem w%s: %.0f shares (%s)", wts, sz, cid)
                continue
            try:
                txh = self._send_redeem(cid)
                self.ledger.event("redeemed",
                                  f"w{wts} {sz:.0f} shares tx {txh}")
                log.info("redeemed w%s: %.0f shares tx %s", wts, sz, txh)
            except Exception as e:  # noqa: BLE001
                # stays pending; retried next pass — a redemption can wait,
                # silently losing one cannot
                self.ledger.event("redeem_error", f"w{wts} {e}")
                log.error("redeem w%s failed (will retry): %s", wts, e)

    async def run(self):
        while True:
            await self.once()
            await asyncio.sleep(self.cfg.redeem_every_s)

    # ---- venue plumbing ----
    async def _condition_id(self, wts):
        if wts in self._cids:
            return self._cids[wts]
        import aiohttp
        slug = slug_for(self.cfg, wts)
        url = f"{self.cfg.gamma_url}/markets?slug={slug}&closed=true"
        async with aiohttp.ClientSession() as s:
            async with s.get(url,
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                arr = await r.json()
        cid = arr[0].get("conditionId") if arr else None
        if cid:
            self._cids[wts] = cid
            if len(self._cids) > 500:
                self._cids = dict(list(self._cids.items())[-250:])
        return cid

    def _send_redeem(self, condition_id):
        """redeemPositions(collateral, 0x0, conditionId, [1,2]) from the
        EOA. Imported lazily: only a LIVE box with REDEEM_DRY=0 ever needs
        web3 installed, POLYGON_RPC set, and POL for gas."""
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(self.cfg.polygon_rpc))
        acct = w3.eth.account.from_key(self.cfg.pm_private_key)
        ctf = w3.eth.contract(
            address=Web3.to_checksum_address(self.cfg.ctf_address),
            abi=json.loads(_CTF_REDEEM_ABI))
        tx = ctf.functions.redeemPositions(
            Web3.to_checksum_address(self.cfg.collateral_address),
            b"\x00" * 32, bytes.fromhex(condition_id[2:]), [1, 2],
        ).build_transaction({
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
        })
        signed = acct.sign_transaction(tx)
        txh = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(txh, timeout=120)
        return txh.hex()


_CTF_REDEEM_ABI = json.dumps([{
    "name": "redeemPositions", "type": "function",
    "stateMutability": "nonpayable", "outputs": [],
    "inputs": [
        {"name": "collateralToken", "type": "address"},
        {"name": "parentCollectionId", "type": "bytes32"},
        {"name": "conditionId", "type": "bytes32"},
        {"name": "indexSets", "type": "uint256[]"}],
}])
