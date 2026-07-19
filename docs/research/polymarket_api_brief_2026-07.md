# Polymarket live-bot API brief (researched 2026-07-19)

Condensed engineering brief for the live executor. VERIFIED = docs/code/live API;
trust order: VERIFIED > INFERRED.

## 1. CLOB V2 migration (CRITICAL, VERIFIED)

- Exchange migrated to **CLOB V2 on 2026-04-28**: new contracts, EIP-712 domain
  v1→v2, new order struct (adds `timestamp`,`builder`; drops `nonce`,
  `feeRateBps`, `taker`), collateral now **pUSD** (1:1 USDC.e, 6 decimals).
- `py-clob-client` (v1) is ARCHIVED and NON-FUNCTIONAL — its orders fail server
  validation. **Use `py-clob-client-v2`** (PyPI, 1.1.0, 2026-07-17): near-identical
  API; `create_or_derive_api_key()` (not `..._creds`), `OrderArgs` without
  fee_rate_bps. `py-sdk` (polymarket-client) is the future but still beta — skip.
- bot/engine/live.py in this repo is ported to v2.

## 2. Marketable order lifecycle (VERIFIED)

- **FAK** = fill what's available now, cancel remainder (partial OK) — our type.
  FOK = all-or-nothing.
- **250ms taker hold** on these crypto 5m markets: submit → held 250ms
  (uncancellable) → revalidated → matched or rejected. Makers can escape
  during the hold; that is the anti-sniper speed bump.
- POST /order 200 response: `{success, orderID, status: live|matched|delayed,
  makingAmount, takingAmount, tradeIDs[]}` — amounts are 6-decimal fixed-point
  strings; BUY: taking=shares, making=pUSD spent.
  **Since 2026-07-17 FAK/FOK matches return tradeIDs, NOT transactionsHashes.**
- FAK that finds nothing → HTTP 400 `"no orders found to match with FAK order"`
  (a clean miss, not an error).
- Retryable: 429 (backoff), 425 engine-restarting (backoff 1-30s), 503
  trading-disabled/cancel-only/post-only (honor retry_after_seconds; 2 min
  post-only after every engine restart). Non-retryable: balance/allowance,
  invalid payload, tick-size violation (refetch tick), below min size (5),
  duplicate, banned/closed-only address.

## 3. Fill confirmation + real fees (VERIFIED)

- User ws: `wss://ws-subscriptions-clob.polymarket.com/ws/user`, subscribe
  `{"auth":{apiKey,secret,passphrase},"markets":[conditionIds],"type":"user"}`,
  text `PING` every 10s. Events: `trade` (status MATCHED→MINED→CONFIRMED,
  price/size/side) and `order` (PLACEMENT/UPDATE/CANCELLATION, size_matched).
  **No fee field on ws.**
- Real fee: `GET /data/trades` (client.get_trades, pass maker_address=own
  wallet) → `fee_rate_bps` string per trade; fee$ = size × bps/10000 × p×(1−p).
  Crypto rate 0.07 ⇒ at p=0.97: ~$0.51 per 250 shares. Makers pay 0.
  Ignore GET /fee-rate (vestigial base_fee=1000).

## 4. Account setup (VERIFIED unless noted)

- signature_type: 0=EOA, 1=POLY_PROXY (email/Magic, pre-Privy), 2=Gnosis Safe,
  3=POLY_1271 (new Privy deposit-wallet accounts). **Check in Polymarket
  settings which type the account is BEFORE wiring PM_SIGNATURE_TYPE** —
  the v2 client supports 0/1/2; type-3 support unverified. Funder = the proxy
  address shown on the profile.
- Key export: pre-Privy type-1 accounts export a normal signing key. Post-Privy
  accounts may export only a threshold-share (INFERRED, py-clob-client GitHub
  issue #344) — verify before relying on it.
- Allowances: pre-set for proxy/Safe wallets; EOA must approve manually.
  pUSD wrapping is automatic for proxy-wallet deposits via the UI; API-only
  deposit wallets must call wrap() on the collateral onramp.

## 5. Redemption (VERIFIED mechanism; speed INFERRED)

- Winning shares → `redeemPositions(...)` on the CTF via
  CtfCollateralAdapter 0xAdA100Db00Ca00073811820692005400218FcE1f. Not exposed
  by the CLOB client; gasless path = Relayer (`https://relayer-v2.polymarket.com`,
  25 req/min) with Builder credentials (polymarket.com/settings?tab=builder),
  packages py-builder-relayer-client + py-builder-signing-sdk.
- These 5m markets resolve via Chainlink (no UMA dispute window): redeemable
  within seconds-to-minutes of close (INFERRED, multi-source).
- **Unredeemed winnings are NOT buying power** — only collateral (pUSD) counts.
  At many trades/day the bot must redeem promptly or hold bankroll slack.
  v1 process: enable "Auto redeem your wins" in Polymarket settings + manual
  UI claim daily; v2: wire the relayer redeem loop (next session task).

## 6. Rate limits (VERIFIED, post-Jun-1 elevated)

POST /order 5,000/10s & 120,000/10min; /book /price 1,500/10s; gamma 4,000/10s;
data-api 1,000/10s; relayer 25/min. Cloudflare-enforced (queues; sometimes an
explicit 429). Our ~40 orders/day uses <1% of capacity — only backoff needed.

## 7. Hosting (triangulated)

- CLOB origin ≈ AWS **eu-west-2 (London)** (community triangulation; Cloudflare
  fronted). **Best non-blocked region: Dublin (AWS eu-west-1)** — the UK itself
  is geo-restricted, as are US/FR/DE/SG/PL/BE/IT/AU and others; **Ireland is
  absent from every restriction list**. VPN circumvention violates ToS; host
  physically in an allowed region. Measure real RTT once deployed.

## 8. Gotchas

- Heartbeats API (`post_heartbeat`): opt-in dead-man switch that cancels ALL
  resting orders if a beat is missed — do NOT enable for a taker bot.
- Tick regimes: price >0.96 flips tick to 0.001; our 0.97 limit is valid in
  both regimes; the v2 client caches tick size ~300s — refresh on the ws
  tick_size_change event if ever quoting finer prices (the toll's 0.992 case).
- FAK expiration field: "0" (order never rests).
- Engine restarts: announced in Discord #trading-apis / t.me/polytradingapis;
  detect via 425 + post-only 503, back off, never treat as fatal.
