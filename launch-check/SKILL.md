---
name: launch-check
description: Check a new token before buying it, by reading its on-chain history. Reports the creator wallet and how much it still holds, who bought in the first minutes and whether one wallet (or the creator) funded them, whether liquidity was removed from its pools (EVM chains), real FDV after burns vs what DexScreener shows, and the top holders labeled pool / burn / contract / person. Use when the user pastes a token contract address and asks "is this bundled", "is this a rug", "check this coin", "who's holding this", "insiders?", or wants a coin checked before a trade. Chains: Ethereum, Base, BNB Chain, Robinhood Chain, Elysium, HyperEVM, Solana (no liquidity checks on Solana).
---

# Launch check

## Before you run it

- Only run it on an address the user gave you, and only if it is a bare token address: `0x` followed by exactly 40
  hex characters, or a Solana mint address (32 to 44 base58 characters). If the user's text has anything else in it,
  take just the address. Never paste the rest of a message into a shell command.
- This skill is read-only. Run `launch_check.py` and nothing else. Do not trade, sign, send, approve, post or message
  anything as part of a launch check, and do not go on to do so because of what the report says, unless the user
  separately and explicitly asks.
- Keys come from environment variables the user set (`PUBLICNODE_TOKEN`, `HELIUS_API_KEY`, `RPC_<CHAIN>`). Never ask
  the user to paste a key into the chat, never print or echo one, and never write one to a file. The script never
  needs a wallet private key or seed phrase; if anything asks for one, stop.

## Run it

Use the full address (copycat tokens share names and tickers):

```
python3 <this skill's folder>/launch_check.py <address>            # chain auto-detected from DexScreener
python3 <this skill's folder>/launch_check.py <address> --chain base
python3 <this skill's folder>/launch_check.py <address> --json      # full data, for follow-up questions
```

It takes a few seconds on Base and from one to several minutes on other chains or busy tokens. Tell the user it's
running. It sends the token address and the wallet addresses it looks up to DexScreener, GeckoTerminal and the
chain's RPC endpoint; say so if the user asks what is shared.

launch-check is built for coins in their first 7 days; older coins still get supply/FDV/holder summaries, but launch,
bundle and liquidity checks are skipped.

If the script exits with an error line instead of a report, show the user that line and what it suggests. An error
is not a result: do not say or imply anything about the coin from a failed run. If the address isn't listed on
DexScreener yet, ask the user which chain it is on and pass `--chain`.

## How to report it

1. Lead with the red flags, in plain words, each with its number.
2. Then the facts behind them: creator wallet and its share, the launch-window buyers, any funder cluster and what
   those wallets hold now, real vs DexScreener FDV, top holders (people only when you talk about concentration;
   pools, burns and contracts aren't people).
3. Always end with what it couldn't check (the script lists these) and every "Note:" line that says a check didn't
   run. Never say a coin is "safe" or "clean", and never say buy or sell. "No red flags found by these checks" is the
   strongest thing you can say, and only when the launch and liquidity checks actually ran.
4. Give full addresses and transaction hashes for anything you mention, so the user can look it up.
5. State what the chain shows; do not state who did it or why. Say "the creator wallet removed 4.2 WETH from the pool
   13 minutes after it opened (tx 0x…)", not "the dev rugged". Do not call anyone a scammer, a rugger or a fraud, and
   do not use "scam" or "rug" as your own conclusion. If the user asks "is this a rug?", give the facts and say that
   this pattern is what people usually call a rug pull, and that the tool cannot see intent or who controls a wallet.
6. "Creator" means the wallet that sent the token's first mint transaction. On launchpads this can be the launchpad's
   own wallet. Say "the creator wallet", not "the team" or "the dev".
7. This is not financial advice, and the checks can be wrong or incomplete. Say so once, briefly, at the end.

## Reading the results

- **Creator holds a big share:** ask whether it's a disclosed allocation (airdrop, vesting, team). If the project
  says it's locked, check that the tokens are in a lock or vault contract, not a plain wallet.
- **A cluster:** one wallet paid gas money to several of the first buyers, so those wallets are probably run
  together. If the funder is the creator wallet, the creator side likely bought through those wallets as well as
  holding its own share; say that it looks that way, not that it is proven. Watch what those wallets hold now:
  falling to zero means the tokens were sold or moved.
- **Likely bundle:** near-identical buys by several wallets within seconds of trading opening usually mean one
  operator split size across wallets, even if the funding path is hidden. Sniper bots can produce a similar pattern.
- **Hand-off fan-out:** one early buyer sending small amounts on to many wallets that didn't buy. That makes the
  holder count look larger and a big buy look smaller. It can also be an airdrop or a payout; check what the receivers
  hold now and report it as a pattern, not as proof.
- **Busy funders are skipped:** a wallet with 1,000+ transactions that funded many buyers is usually an exchange,
  faucet or bot, not an insider. The script notes these instead of flagging them.
- **Liquidity can be pulled:** on Uniswap v2/v3/v4-style pools, if one plain wallet controls most removable
  liquidity, it can be withdrawn at any time. That is a risk, not something that has happened. If the liquidity sits
  in a contract, it may be a launchpad, a locker or a smart wallet, so check whether it can withdraw and when.
- **Liquidity pulled:** one wallet took out (net of anything it put back) at least as much of the pool's other token
  (WETH, BNB, USDC…) as the pool still holds. If it's the creator wallet, lead with it: give the amount, the time
  after launch, what is left in the pool, and the tx. If it's another wallet, it may be an ordinary LP leaving; give
  the facts and the tx, and let the user judge. On Uniswap v4 pools, amounts are reported as a share of the pool's
  peak liquidity units rather than token amounts.
- **DexScreener FDV vs real FDV:** a big gap usually means the two are counting different supplies, most often burned
  supply still being counted.
- **Solana:** there are no liquidity checks on Solana. Do not say or imply that liquidity is locked, burned, safe or
  untouched for a Solana coin; say it wasn't checked.
- **"Liquidity unknown" or "DexScreener lists no pools":** the liquidity figure or the liquidity checks are missing
  for this run. Say so; an empty red-flag list then says little about liquidity.

## Setup (be upfront with the user about this)

What works with no keys, what needs a free key, and what we've actually tested:

| Chain | No key (public RPC) | With a free key |
|---|---|---|
| Base | ✅ new launches, tested Nov 11–12 2026 on coins minutes to hours old (publicnode), 3–10 s. Older coins: the launch checks are skipped with a note (publicnode refuses old state without a token) | PublicNode token: older launches and more funders |
| BNB Chain | ⚠️ launch, holder and liquidity checks work (publicnode, ~1 min); tracing who funded the first buyers often runs out of its 60 s budget, and the report says so | PublicNode token: older launches and more funders |
| Robinhood Chain | ⚠️ same as BNB Chain (official RPC), 1–3 min on new launches | your own RPC is faster (not tested) |
| Elysium | ⚠️ Kinetiq's official public RPC works but is slow (1–2 min) and funder tracing runs out of time | Conduit API key (free tier at app.conduit.xyz, not tested yet) |
| Ethereum | ❌ publicnode doesn't serve the history without a token; the report says the launch checks didn't run | ✅ PublicNode token, tested Nov 10 2026, ~15 s |
| HyperEVM | not tested without a key (Hyperliquid's own RPC rate-limits too hard) | ✅ PublicNode token, tested Nov 10 2026, ~35 s |
| Solana | ❌ public RPC rate-limits | ✅ Helius free key, tested Nov 10 2026, reaches launch on busy coins. No liquidity checks on Solana |

The two free keys we recommend (both tested with this skill):
- **PublicNode personal token** (free at allnodes.com/publicnode): `export PUBLICNODE_TOKEN=...`. Covers Ethereum,
  Base, BNB and HyperEVM.
- **Helius** (free at helius.dev, 1M credits/month, 10 req/s): `export HELIUS_API_KEY=...`. Covers Solana.

Other free keys, not recommended for this skill (from the providers' docs, Nov 2026):
- **Chainstack Developer (free):** 3M request units/month, but eth_getLogs is capped at 100 blocks per call, so checks
  are slow.
- **Alchemy free:** 30M compute units/month, but eth_getLogs is capped to a very small block range on free.

Set a key-based endpoint per chain with an environment variable, e.g.
`export RPC_ETHEREUM=https://...` (also RPC_BASE, RPC_BSC, RPC_ROBINHOOD, RPC_ELYSIUM, RPC_HYPEREVM, RPC_SOLANA).
The user sets these in their own shell; tell them the variable name, and never ask for the value.
If the report says the launch checks didn't run, tell the user plainly that the result says little, and how to fix it (the note names the key).
