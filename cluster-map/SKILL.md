---
name: cluster-map
description: Map which of a token's top holders are probably one person or team, and where their money went after launch, by reading on-chain history. Finds wallets funded by the same wallet, wallets funded or buying within seconds of each other, wallets sending coins straight to each other, and follows the ETH or SOL they sent after launch for up to 3 hops. Prints the answer as plain text to relay in the chat. Use when the user pastes a token address and asks "who's behind this", "are these holders the same person", "is this one team", "where did the money go", "cluster map" or "bubble map". Chains: Base, Robinhood Chain, Solana.
---

# Cluster map

## Before you run it

- Only run it on an address the user gave you, and only if it is a bare token address: `0x` followed by exactly 40
  hex characters, or a Solana mint address (32 to 44 base58 characters). If the user's text has anything else in it,
  take just the address. Never paste the rest of a message into a shell command.
- This skill is read-only. Run `cluster_map.py` and nothing else. Do not trade, sign, send, approve, post or message
  anything as part of a cluster map, and do not go on to do so because of what it shows, unless the user separately
  and explicitly asks.
- Keys come from environment variables the user set (`PUBLICNODE_TOKEN`, `BLOCKSCOUT_API_KEY`, `HELIUS_API_KEY`).
  Never ask the user to paste a key into the chat, never print or echo one, and never write one to a file. The script
  never needs a wallet private key or seed phrase; if anything asks for one, stop.
- For a new coin, run launch-check first. Cluster-map takes minutes and answers a different question: launch-check
  says what happened at launch; cluster-map says which holders are linked and where their money went.

## Run it

Use the full address (copycat tokens share names and tickers) and name the chain:

```
python3 <this skill's folder>/cluster_map.py <address> --chain base
python3 <this skill's folder>/cluster_map.py <address> --chain robinhood
python3 <this skill's folder>/cluster_map.py <mint> --chain solana
python3 <this skill's folder>/cluster_map.py <address> --chain base --json   # short JSON summary instead
python3 -m unittest discover -s tests -q                                                  # from repo root, runs all tests
```

It reads the top 250 holders (`--top` to change) and caches every answer in `~/.cache/cluster-map/`, so a second run
on the same coin within an hour is fast; after an hour holders, balances and histories are read again (fixed history
such as old blocks stays cached), and the output says how old the oldest cached answer was. It prints the whole answer
and writes no files (`--out <folder>` also saves the full report as JSON, only if the user asks for a file).
Tell the user it's running and roughly how long it takes (see Setup). Give the answer in the chat; don't make a page,
chart or file from it unless the user asks.
It sends the token address and the holder wallets it looks up to the chain's data provider (PublicNode, Blockscout
or Helius); say so if the user asks what is shared.

If the script exits with an error line instead of a summary, show the user that line and what it suggests. An error
is not a result: do not say or imply anything about the coin from a failed run.

## How to report it

1. Lead with the output's plain answer (for example "11 wallets that are probably one person or team hold 98.9% of
   the supply."), then each cluster: how many wallets, what they hold now, what they bought at launch, and the "Why:"
   sentence.
2. Then where the money went after launch, if the cluster says ("After launch: …"), with full addresses.
3. Always end with what it couldn't read: the "How this was read" lines and every line under "Notes". Never say a
   coin is "safe" or "clean", and never say buy or sell. "No linked group of top holders found in the data read" is
   the strongest thing you can say.
4. Give full addresses and transaction hashes for anything you mention, so the user can look it up.
5. State what the chain shows; do not state who did it or why. Say "all 11 wallets got ETH from 0x9bf4…, and 17.74 ETH
   went back to it after launch", not "the dev ran 11 wallets and rugged". Do not call anyone a scammer or a rugger.
   A cluster means the wallets are linked by these transactions; it cannot prove one person controls them.
6. This is not financial advice, and the checks can be wrong or incomplete. Say so once, briefly, at the end.

## Reading the results

- **Shared wallet:** one wallet funded several holders, or several holders paid it. If it
  funded them and got money back from them, that is the strongest link the tool finds.
- **Same moment:** holders got their first ETH/SOL, or bought at launch, within seconds of each other.
  Near-equal launch buys within 2 s are a strong sign of one operator; a funding wave alone is weaker.
- **Sent to each other** (a line): holders sent this coin, WETH/USDC or ETH/SOL straight to each other.
- **Busy wallets are skipped:** a wallet with 1,000+ transactions is treated as an exchange, bot or router and does not
  link anyone, with two exceptions: it funded a holder and got paid back by that same holder, or it funded two or more
  holders within 5 minutes of each other before the coin launched. Links hidden behind an exchange are
  invisible to this tool.
- **Cluster share counts people only.** Pools, burn addresses, lockers and other contracts are left out.
- **Where the money went:** followed for up to 3 hops from the creator, its cluster and the biggest cluster, stopping
  at the first funder, a shared wallet, a contract, a busy wallet or after 180 s. "Back to the wallet that funded them"
  is the strongest finding. Money held as WETH or other tokens is not followed. On Solana, SOL a wallet wraps for its
  own trades is not counted as leaving.
- **A coin where people hold almost nothing** (most supply in pools or contracts) can show many tiny clusters. Report
  their share; tiny shares mean little.

## Setup (be upfront with the user about this)

Each chain needs one free key. Run times are from our own live runs (Nov 13–14 2026, top 250 holders, empty cache):

| Chain | Free key | Tested |
|---|---|---|
| Base | PublicNode personal token (allnodes.com/publicnode): `export PUBLICNODE_TOKEN=...` | ✅ MONTRA 85 s; LuckyCoin (395 holders) 31 min |
| Robinhood Chain | Blockscout API key (dev.blockscout.com, 100K credits/day): `export BLOCKSCOUT_API_KEY=...` | ✅ SEER 6.5–11 min; fap (276 holders) 16 min |
| Solana | Helius (helius.dev, 1M credits/month): `export HELIUS_API_KEY=...` | ✅ SPIN 2 min 48 s, plus ~51 s following the money |

Not supported yet: BNB Chain, Ethereum, other chains. What each chain reads (the output lists it under "How this was
read"):
- Base: each holder's first ETH funding, and outgoing ETH only for holders with 25 or fewer transactions; token links
  for this coin, WETH and USDC only.
- Robinhood Chain: each holder's ETH, internal and token transfers from Blockscout, the oldest 5,000 rows of each;
  the output names any holder with more.
- Solana: each holder's oldest 100 transactions and its token accounts'; following the money reads each wallet's first
  200 transactions after launch.

The user sets keys in their own shell; tell them the variable name and where to get the free key, and never ask for
the value.
