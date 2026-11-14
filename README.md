# crypto-skills

Free AI skills for checking crypto yourself, by [@gkisokay](https://x.com/gkisokay).

Everyone calls out scams, but few people check the chain for themselves. These tools let you check a coin before you buy it. They
read the token's transactions from the blockchain through public RPC endpoints, so you see what the chain says, not what the chart
or the caller says. Pool addresses, prices and liquidity figures come from DexScreener, and holder counts from GeckoTerminal.

New skills go out by email first: **[sign up on Substack](https://substack.com/@gkisokay)** (free).

## Skills

Each skill folder is self-contained (code + docs), so new skills can be added beside `launch-check/`.

| Skill folder | What it does | Run from repo root | Tests |
|---|---|---|---|
| `launch-check/` | Checks a fresh token's launch: creator, early buyers/funders, bundle patterns, liquidity control/pulls (EVM), FDV mismatch, holders | `python3 launch-check/launch_check.py <address>` | `python3 -m unittest discover -s tests -q` |

## launch-check

Paste a token address, get back:

- **The creator wallet** (the wallet that sent the token's first mint transaction) and how much of the supply it still holds
- **Who bought in the first 3 minutes**, and whether one wallet (or the creator) funded several of them. That's
  how a bundled launch looks onchain.
- **Timing bundles:** flags likely bundles when several wallets buy near-identical sizes within seconds of trading
  opening, even if funding links are hidden.
- **Real FDV vs DexScreener's FDV.** A big gap usually means the two are counting different supplies (for example
  burned tokens still being counted).
- **The top holders, labeled** pool, burn, contract or person, so a liquidity pool doesn't get mistaken for a whale
- **Liquidity control and removals (EVM chains, Uniswap v2/v3/v4-style pools):** flags when one plain wallet
  (including the creator) controls most removable liquidity, and reports which wallet removed liquidity and how soon
  after launch; v4 amounts are reported as a share of the pool's liquidity units.
- **A one-line verdict** on line 2: the main risk in plain words, picked from the red flags.
- **Snipers vs bundles:** early buyers with 1,000+ past transactions are called sniper bots; fresh wallets that buy
  together are a likely bundle.
- **Wallet farms:** 20+ plain wallets holding near-equal slices of the supply, with a one-hop funder sample.
- **Creator history and exit:** how many other coins the creator wallet launched, how they're doing now, and when the
  creator sold or moved its tokens after launch.
- **Contract traps (EVM):** a simulated sell (honeypot, sell tax), owner-only mint, blacklist and pause functions, and
  upgradeable proxies. **Token traps (Solana):** mint and freeze authority, and Token-2022 extensions that can block
  transfers or take fees.
- **Fake market cap:** FDV far above what the liquidity supports.
- **Copycat names:** same name or ticker as a bigger, older coin.
- **Solana liquidity pulls** from PumpSwap, Meteora and Raydium CPMM pools.

launch-check is built for coins in their first 7 days. Older coins still get supply/FDV/holder summaries, but launch,
bundle and liquidity checks are skipped.

It will never tell you a coin is "safe". The best it can say is that these checks found no red flags.

### What it doesn't check (yet)

- Contract traps the sell simulation can't reach: it needs a Uniswap v2-style pool against the wrapped native coin, and
  owner functions are found by selector, so a renamed function is missed.
- Aerodrome and other non-Uniswap pool designs, and any pool DexScreener doesn't list.
- **Who controls Solana pool liquidity.** Pulls from PumpSwap, Meteora and Raydium CPMM pools are checked; who could
  pull them isn't.
- Fake volume that already stopped: the check reads the latest ~140 transactions only.
- Who is behind a wallet. A "person" is any plain wallet, which can be a bot or an exchange; smart-contract wallets
  (Safe, Coinbase Smart Wallet) are labeled "contract".

Check those separately before you buy. Every report ends with a "Couldn't check" list.

### Chains

| Chain | Works with no key | With a free key |
|---|---|---|
| Base | ✅ new launches, in seconds | ✅ older coins too (PublicNode token) |
| BNB Chain | ⚠️ launch, holder and liquidity checks; tracing who funded the first buyers often runs out of time | ✅ PublicNode token |
| Robinhood Chain | ⚠️ same as BNB Chain; 1 to 3 minutes | — |
| Ethereum | ❌ the report says the launch checks didn't run | ✅ PublicNode token |
| HyperEVM | not tested | ✅ PublicNode token |
| Solana | ❌ the public RPC rate-limits | ✅ Helius key (no liquidity checks on Solana) |
| Elysium | ⚠️ slow; funder tracing runs out of time | not tested yet |

A coin that is only minutes old may not be on DexScreener yet. The tool then asks you to pass `--chain`, and the
report says the liquidity checks had nothing to check. Run it again a few minutes later.

Free keys (2 minutes each):
- **PublicNode** personal token, free at [allnodes.com/publicnode](https://www.allnodes.com/publicnode). Covers Ethereum,
  Base, BNB and HyperEVM: `export PUBLICNODE_TOKEN=...`
- **Helius**, free at [helius.dev](https://www.helius.dev). Covers Solana: `export HELIUS_API_KEY=...`

Keys are read from environment variables only. Don't paste a key into a chat with an AI agent; set it in your shell.

## Install

You need Python 3.9 or newer (`python3 --version`; on a Mac, the one installed with Apple's Command Line Tools works).
There's nothing else to install.

```
git clone https://github.com/Gkisokay/crypto-skills
cd crypto-skills
```

**Claude Code:** copy the skill into your skills folder, then just ask Claude to check a coin.

```
mkdir -p ~/.claude/skills && cp -R crypto-skills/launch-check ~/.claude/skills/
```

> check this coin <paste the full token address>

**Codex, Cursor or any other agent:** open the `crypto-skills` folder and tell it:
"read launch-check/SKILL.md and check this coin: `<address>`".

**No AI, just the terminal:**

```
python3 launch-check/launch_check.py <address>
python3 launch-check/launch_check.py <address> --chain base
python3 -m unittest discover -s tests -q
```

A check takes seconds on Base and one to three minutes on Robinhood Chain. On an
older coin, the report says the launch checks didn't run because launch-check is for the first 7 days.

Always paste the full address. Copycat tokens share names and tickers.

## What leaves your machine

launch-check has no server, no account and no analytics. Nothing is sent to the author. It reads no files and writes
none (apart from Python's own `__pycache__` folder), and it reads keys only from the environment variables above.

It does make web requests, and each service below sees your IP address, like any website you visit:

| Service | What it is sent |
|---|---|
| DexScreener (`api.dexscreener.com`) | the token address |
| GeckoTerminal (`api.geckoterminal.com`) | the token address and its chain |
| The chain's RPC endpoint: PublicNode (Ethereum, Base, BNB, HyperEVM), `rpc.mainnet.chain.robinhood.com`, `rpc.elysium.kinetiq.xyz`, `api.mainnet-beta.solana.com`, or Helius / whatever you set in `RPC_<CHAIN>` | the token address, its pool addresses, and the addresses of the creator, the first buyers, their funders and the top holders; your `PUBLICNODE_TOKEN` or `HELIUS_API_KEY` if you set one (to that provider only) |

Your own wallet address is not sent unless it is one of the wallets above. These services can see that your IP
looked up a coin. If you run launch-check through an AI agent, the address and the report also go to that AI provider
as part of your chat.

## Disclaimer

launch-check is a free research tool from an individual, shared as-is under the MIT license. Please read this before you use it.

- **Not advice.** Nothing here, and nothing the tool or an AI agent using it prints, is financial, investment, legal or
  tax advice, or a recommendation to buy, sell or hold anything.
- **It can be wrong or incomplete.** It reads public data through third-party services and applies simple rules. It
  can miss things, mislabel wallets and flag ordinary activity. "No red flags found" does not mean a coin is safe; it
  means these checks found nothing. Every report ends with a list of what it couldn't check.
- **Flags describe on-chain activity, not people or intent.** A flag says what an address did on a public blockchain.
  It does not say who controls that address or why they did it. "Creator" means the address that sent the token's
  first mint transaction, which can be a launchpad. Verify a flag yourself before you repeat it about anyone in
  public; what you post is your responsibility.
- **Use it at your own risk.** You alone are responsible for your decisions and any losses. To the extent the law
  allows, the author is not liable for anything arising from the use of this software or its output. See
  [LICENSE](LICENSE).
- **Your AI agent is your responsibility.** If you run this through Claude, Cursor, Codex or any other agent, you are
  responsible for what that agent does in your sessions: the commands it runs, the keys it uses, the trades it makes
  and anything it posts or sends. This skill only reads public data. It never asks for a private key or seed phrase
  and never signs or sends a transaction; don't give an agent wallet keys in order to use it.
- **Third-party services have their own rules.** The tool calls DexScreener, GeckoTerminal and public RPC endpoints
  (PublicNode, Robinhood Chain, Kinetiq, Solana, and Helius or any endpoint you configure). Each has its own terms,
  rate limits and privacy policy, which apply to you when you run it. They can change or stop working at any time.
- **No affiliation.** This project is independent. It is not affiliated with, endorsed by or sponsored by any
  blockchain, exchange, DEX, launchpad, wallet, data provider or AI company named here. All names and trademarks
  belong to their owners.

## License

MIT; see [LICENSE](LICENSE). Use it, change it, share it.
