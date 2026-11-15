"""Solana mint-level trap flags (authorities and Token-2022 extensions)."""

TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def _is_address(value):
    return isinstance(value, str) and 32 <= len(value) <= 44 and all(ch in B58 for ch in value)


def _to_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _fmt_percent(basis_points):
    pct = basis_points / 100.0
    return f"{pct:.4f}".rstrip("0").rstrip(".")


def _fmt_ui_amount(raw_amount, decimals):
    if decimals is None:
        return str(raw_amount)
    ui = raw_amount / (10 ** decimals)
    return f"{ui:,.8f}".rstrip("0").rstrip(".")


def _fee_entry(fee_obj):
    if not isinstance(fee_obj, dict):
        return None
    bps = _to_int(fee_obj.get("transferFeeBasisPoints"))
    max_raw = _to_int(fee_obj.get("maximumFee"))
    if bps is None or max_raw is None:
        return None
    return bps, max_raw


def _fee_desc(bps, max_raw, decimals):
    return f"{_fmt_percent(bps)}% fee (max {_fmt_ui_amount(max_raw, decimals)} tokens)"


def mint_trap_findings(account_value):
    """Return (flags, notes, cant_check) from one getAccountInfo(..., jsonParsed) value object."""
    flags, notes, cant_check = [], [], []
    data = (account_value or {}).get("data")
    parsed = (data or {}).get("parsed") if isinstance(data, dict) else None
    info = (parsed or {}).get("info") if isinstance(parsed, dict) else None
    if not isinstance(info, dict):
        cant_check.append(
            "Mint trap checks did not run because the mint account was not returned as jsonParsed; set HELIUS_API_KEY "
            "(free at helius.dev) or RPC_SOLANA and run again."
        )
        return flags, cant_check

    decimals = info.get("decimals")
    if not isinstance(decimals, int):
        decimals = None

    mint_authority = info.get("mintAuthority")
    if _is_address(mint_authority):
        flags.append(f"Mint authority is still set to {mint_authority}: this address can mint more tokens.")

    freeze_authority = info.get("freezeAuthority")
    if _is_address(freeze_authority):
        flags.append(
            f"Freeze authority is still set to {freeze_authority}: this address can freeze holder token accounts and block transfers."
        )

    program = (data.get("program") if isinstance(data, dict) else "") or ""
    is_token_2022 = ("2022" in str(program).lower()) or ((account_value or {}).get("owner") == TOKEN_2022_PROGRAM)
    if not is_token_2022:
        return flags, notes, cant_check

    extensions = info.get("extensions")
    if extensions is None:
        cant_check.append(
            "Token-2022 extension checks did not run because extensions were missing from the mint account data; set HELIUS_API_KEY "
            "(free at helius.dev) or RPC_SOLANA and run again."
        )
        return flags, notes, cant_check

    if not isinstance(extensions, list):
        return flags, notes, cant_check

    for ext in extensions:
        if not isinstance(ext, dict):
            continue
        name = str(ext.get("extension") or "").strip()
        state = ext.get("state") if isinstance(ext.get("state"), dict) else {}
        key = name.lower()

        if key == "transferfeeconfig":
            authority = state.get("transferFeeConfigAuthority")
            older = _fee_entry(state.get("olderTransferFee"))
            newer = _fee_entry(state.get("newerTransferFee"))
            if newer is None and older is None:
                fee = _fee_entry(state)
                if fee is not None:
                    older = fee

            fee_options = []
            if older is not None:
                fee_options.append(("older", older))
            if newer is not None and newer != older:
                fee_options.append(("newer", newer))
            elif newer is not None and older is None:
                fee_options.append(("newer", newer))

            positive_fee = any(bps > 0 for _, (bps, _) in fee_options)
            authority_set = _is_address(authority)
            if not authority_set and not positive_fee:
                continue

            by_tag = dict(fee_options)
            if len(fee_options) > 1:
                older_txt = _fee_desc(*by_tag["older"], decimals) if "older" in by_tag else None
                newer_txt = _fee_desc(*by_tag["newer"], decimals) if "newer" in by_tag else None
                if authority_set:
                    flags.append(
                        f"Transfer fee authority {authority} can change the fee; older fee is {older_txt} and newer fee is {newer_txt}, "
                        "and current epoch was not checked."
                    )
                elif positive_fee:
                    flags.append(
                        f"Transfers use a fixed fee schedule; older fee is {older_txt} and newer fee is {newer_txt}, and current epoch was not checked. "
                        "The fee authority is revoked, so it can't be raised."
                    )
            elif fee_options:
                bps, max_raw = fee_options[0][1]
                fee_txt = _fee_desc(bps, max_raw, decimals)
                if authority_set:
                    flags.append(f"Transfer fee authority {authority} can change the fee; transfers currently pay {fee_txt}.")
                elif bps > 0:
                    flags.append(
                        f"Transfers pay a fixed {fee_txt}; the fee authority is revoked, so it can't be raised."
                    )
            else:
                continue
            continue

        if key == "transferhook":
            authority = state.get("authority")
            program_id = state.get("programId")
            if not _is_address(program_id):
                continue
            if _is_address(authority):
                flags.append(
                    f"Transfer hook is enabled: authority {authority} controls hook program {program_id}, and that program can block token transfers."
                )
            else:
                flags.append(
                    f"Transfer hook is enabled with program {program_id}; the hook authority is revoked, and that program can still block token transfers."
                )
            continue

        if key == "permanentdelegate":
            delegate = state.get("delegate")
            if _is_address(delegate):
                flags.append(
                    f"Permanent delegate is set to {delegate}: this address can move tokens from any holder account without that holder signing."
                )
            continue

        if key == "defaultaccountstate":
            account_state = state.get("accountState")
            if isinstance(account_state, str) and account_state.lower() == "frozen":
                freeze_auth = info.get("freezeAuthority")
                if _is_address(freeze_auth):
                    flags.append(
                        f"Default account state is frozen: freeze authority {freeze_auth} can keep new token accounts frozen until it thaws them."
                    )
                else:
                    flags.append(
                        "Default account state is frozen and freeze authority is revoked: new token accounts start frozen and cannot be thawed by an authority."
                    )
            continue

        if key == "nontransferable":
            flags.append(
                "Non-transferable is enabled: transfers are disabled for this token."
            )
            continue

        if key == "confidentialtransfermint":
            authority = state.get("authority")
            auto_approve = state.get("autoApproveNewAccounts")
            if isinstance(auto_approve, bool):
                auto_txt = "on" if auto_approve else "off"
            else:
                auto_txt = "unknown"
            if _is_address(authority):
                notes.append(
                    "Confidential transfers are on: balances and amounts can be hidden from checks like this one. "
                    f"Authority {authority} controls account approvals and auto-approve is {auto_txt}."
                )
            else:
                notes.append(
                    "Confidential transfers are on: balances and amounts can be hidden from checks like this one. "
                    f"Confidential transfer authority is revoked and auto-approve is {auto_txt}."
                )
            continue

        if key in ("pausable", "pausableconfig"):
            authority = state.get("authority")
            paused = state.get("paused")
            if _is_address(authority):
                text = f"Pausable extension is enabled: authority {authority} can pause token transfers"
            else:
                text = "Pausable extension is enabled: pause authority is revoked"
            if paused is True:
                text += " and transfers are currently paused."
            elif paused is False:
                text += " and transfers are currently unpaused."
            else:
                text += "."
            flags.append(text)

    return flags, notes, cant_check


def mint_trap_flags(account_value):
    """Backward-compatible wrapper: returns (flags, cant_check)."""
    flags, _notes, cant_check = mint_trap_findings(account_value)
    return flags, cant_check
