# Setup: Claude for onchain work, with free keys

Do this once. After it, your AI agent can read the chain for you: the skills in this repo, and any onchain question
you ask it in plain words. Every key here is free. Takes about 15 minutes.

## 1. What you need

- **An AI agent that can run commands on your computer:** [Claude Code](https://code.claude.com/docs), Codex or
  Cursor. Each needs its own account; check their pricing pages for which plans include it.
- **Python 3.9 or newer.** Check with `python3 --version`. On a Mac, the one that comes with Apple's Command Line Tools
  works (`xcode-select --install` if you don't have it). Nothing else to install: the skills use Python's standard
  library only.
- **git**, to download this repo (it comes with the Command Line Tools on a Mac).

## 2. Get the skills

```
git clone https://github.com/Gkisokay/crypto-skills
cd crypto-skills
python3 -m unittest discover -s tests -q
```

The tests need no keys and no internet. If they pass, your Python works.

## 3. Get the free keys

You don't need all of them. Pick the chains you trade.

| Key | Chains | Used by | Free limit | Get it |
|---|---|---|---|---|
| PublicNode personal token | Ethereum, Base, BNB Chain, HyperEVM | launch-check (older coins, Ethereum), cluster-map (Base) | free personal plan | [allnodes.com/publicnode](https://www.allnodes.com/publicnode) |
| Helius API key | Solana | launch-check, cluster-map | 1M credits a month, 10 requests a second | [helius.dev](https://www.helius.dev) |
| Blockscout API key | Robinhood Chain | cluster-map | 100K credits a day, 5 requests a second | [dev.blockscout.com](https://dev.blockscout.com) |

With no keys at all, launch-check still checks new launches through public endpoints: in seconds on Base, more slowly
on BNB Chain and Robinhood Chain. Solana needs Helius: its public endpoint rate-limits too hard.

Sign up, copy the key, and keep the tab open for the next step. **Never paste a key into a chat with an AI.** The
skills read keys from your computer's environment, so the AI never needs to see them.

## 4. Save the keys on your computer

**Mac or Linux.** Open Terminal and add one line per key to your shell's startup file (`~/.zshrc` on a Mac,
`~/.bashrc` on most Linux), replacing the dots with your key:

```
echo 'export PUBLICNODE_TOKEN=...' >> ~/.zshrc
echo 'export HELIUS_API_KEY=...' >> ~/.zshrc
echo 'export BLOCKSCOUT_API_KEY=...' >> ~/.zshrc
source ~/.zshrc
```

**Windows (PowerShell; we haven't tested Windows ourselves):**

```
setx PUBLICNODE_TOKEN "..."
setx HELIUS_API_KEY "..."
setx BLOCKSCOUT_API_KEY "..."
```

Then close and reopen the terminal, and restart your AI agent so it picks up the keys.

## 5. Check each key works

Each command asks for one small piece of public data. It prints the answer, never your key.

**PublicNode** (asks for an old Base balance, which only works with a valid token):

```
curl -s "https://base-rpc.publicnode.com/$PUBLICNODE_TOKEN" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"eth_getBalance","params":["0x4200000000000000000000000000000000000006","0x100"]}'
```

Good: `{"jsonrpc":"2.0","id":1,"result":"0x0"}`. Bad: `Archive requests require a personal token`.

**Helius:**

```
curl -s "https://mainnet.helius-rpc.com/?api-key=$HELIUS_API_KEY" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"getHealth"}'
```

Good: `"result":"ok"`. Bad: `Unauthorized`.

**Blockscout:**

```
curl -s -A "setup-check" "https://api.blockscout.com/v2/api?chain_id=4663&module=block&action=eth_block_number&apikey=$BLOCKSCOUT_API_KEY"
```

Good: a `"result"` with a block number like `"0x6d823ef"`. Bad: `{"error":"Unauthorized"}`.

(On Windows, run these in Git Bash, or write `$env:PUBLICNODE_TOKEN` instead of `$PUBLICNODE_TOKEN` in PowerShell
and use `curl.exe`.)

## 6. Install the skills

**Claude Code:** copy the skill folders into your skills folder, then just ask.

```
mkdir -p ~/.claude/skills
cp -R launch-check cluster-map ~/.claude/skills/
```

> check this coin 0x…   ·   who's behind this token? 0x… on base

**Codex, Cursor or any other agent:** open the `crypto-skills` folder and say "read launch-check/SKILL.md and check
this coin: `<address>`" (or cluster-map/SKILL.md for "who's behind it").

**No AI:** `python3 launch-check/launch_check.py <address>` or
`python3 cluster-map/cluster_map.py <address> --chain base`.

Always paste the full address. Copycat tokens share names and tickers.

## 7. Beyond the skills: ask anything onchain

With these keys set, your agent can read the chain directly. You don't need a skill for every question. Ask in plain
words and let it write the calls:

- "How much ETH does 0x… hold on Base, and when did it first get funded?" (PublicNode)
- "Show me the last 20 transfers out of this Robinhood Chain wallet: 0x…" (Blockscout)
- "What did this Solana wallet buy in the last day?" (Helius)
- "What's this token's price, liquidity and pool?" (DexScreener and GeckoTerminal, no key)
- "What are Base's fees and TVL this month?" (DefiLlama, no key)

Tell it which key is set in which environment variable (just the name, never the value), and ask it to show the
transactions it used, so you can check them yourself.

## 8. Ground rules

- **Read-only.** None of this needs a wallet. Never give an agent your private key or seed phrase, and stop if
  anything asks for one.
- **Keys stay in your environment**, never in a chat, a file you share, or a screenshot.
- **Check before you repeat it.** Tools read public data and can be wrong or incomplete. A flag says what an address
  did, not who controls it or why.
- **Not financial advice.** You make your own calls. See the disclaimer in the [README](README.md).
