"""Markdown alert formatters for Smart Bird.

Formatters are defensive: they lean on ``dict.get`` with sensible defaults so
missing Birdeye fields don't produce ``KeyError`` at alert-fire time.
"""
from __future__ import annotations


_MD_ESCAPE = str.maketrans({
    '_': r'\_',
    '*': r'\*',
    '[': r'\[',
    ']': r'\]',
    '`': r'\`',
})


def _md_escape(value: str) -> str:
    """Escape characters that legacy Telegram Markdown treats specially."""
    return (value or '').translate(_MD_ESCAPE)


def _score_bar(value: int, max_value: int = 25, width: int = 10) -> str:
    """Render a text-based bar: `████████░░ 20/25`."""
    filled = round(value / max_value * width) if max_value > 0 else 0
    filled = max(0, min(width, filled))
    return '█' * filled + '░' * (width - filled) + f' {value}/{max_value}'


def _render_score_chart(score: int, breakdown: dict) -> str:
    """Render a multi-axis score breakdown with bar chart visualization."""
    vv = int(breakdown.get('volume_velocity_score', 0))
    hs = int(breakdown.get('holder_score', 0))
    bp = int(breakdown.get('buy_pressure_score', 0))
    tj = int(breakdown.get('trajectory_score', 0))

    strength = (
        'STRONG' if score >= 85
        else ('MODERATE' if score >= 70
              else ('WEAK' if score >= 50 else 'VERY WEAK'))
    )

    return (
        f'📊 *Score Breakdown* ({score}/100)\n'
        f'`Vol  ` {_score_bar(vv)}\n'
        f'`Hold ` {_score_bar(hs)}\n'
        f'`Buy  ` {_score_bar(bp)}\n'
        f'`Traj ` {_score_bar(tj)}\n'
        f'⚡ Signal: *{strength}*'
    )


def _render_sentiment_bar(sentiment_score: int, sentiment_breakdown: dict) -> str:
    """Render the Layer 4 sentiment section."""
    te = int(sentiment_breakdown.get('trade_engagement', 0))
    vb = int(sentiment_breakdown.get('volume_buzz', 0))
    sp = int(sentiment_breakdown.get('social_presence', 0))
    cg = int(sentiment_breakdown.get('community_growth', 0))

    mood = (
        'HYPED' if sentiment_score >= 80
        else ('BUZZING' if sentiment_score >= 60
              else ('NEUTRAL' if sentiment_score >= 40 else 'QUIET'))
    )

    links = sentiment_breakdown.get('social_links', [])
    links_str = ', '.join(links) if links else 'none found'

    return (
        f'\U0001f4e1 *Sentiment* ({sentiment_score}/100 — {mood})\n'
        f'`Trad ` {_score_bar(te)}\n'
        f'`Buzz ` {_score_bar(vb)}\n'
        f'`Socl ` {_score_bar(sp)}\n'
        f'`Grow ` {_score_bar(cg)}\n'
        f'\U0001f517 Socials: {links_str}'
    )


def format_entry_alert(
    token: dict,
    score: int,
    breakdown: dict,
    smart_money: dict,
    liquidity: dict,
    sentiment_score: int = 0,
    sentiment_breakdown: dict | None = None,
) -> str:
    """Build the entry alert message.

    Parameters
    ----------
    token:        Layer 1 token dict — must contain at least ``address`` and ``symbol``.
    score:        Layer 1 graduation score (0-100).
    breakdown:    Scoring breakdown (unused by the message body today but passed
                  through so callers can log / attach it later).
    smart_money:  Layer 2 hit — ``wallet`` and ``minutes_ago`` required.
    liquidity:    Layer 3 snapshot — ``current_liquidity`` in USD.
    sentiment_score:    Optional Layer 4 buzz score (0-100).
    sentiment_breakdown:
        Optional Layer 4 breakdown dict; when provided a sentiment panel is
        appended after the Layer 1 score chart.
    """
    address = token.get('address', '')
    symbol = _md_escape(token.get('symbol') or '???')
    price = float(token.get('price') or 0.0)
    market_cap = float(token.get('market_cap') or 0.0)

    wallet = smart_money.get('wallet') or ''
    short_wallet = (
        f'{wallet[:4]}...{wallet[-4:]}' if len(wallet) >= 8 else (wallet or 'unknown')
    )
    # wallet: escape the short form, not the raw address used elsewhere.
    short_wallet = _md_escape(short_wallet)
    minutes_ago = int(smart_money.get('minutes_ago') or 0)

    current_liquidity = float(liquidity.get('current_liquidity') or 0.0)

    sentiment_block = (
        f'{_render_sentiment_bar(sentiment_score, sentiment_breakdown)}\n'
        if sentiment_breakdown else ''
    )

    return (
        f"🚨 *SMART BIRD ALERT*\n"
        f"Token: ${symbol} (`{address}`)\n"
        f"Price: ${price:.6f} | MCap: ${market_cap:,.0f}\n"
        f"✅ Smart Money: {short_wallet} entered {minutes_ago}min ago\n"
        f"✅ Liquidity: Healthy (${current_liquidity/1000:.1f}k depth)\n"
        f"\n"
        f"{_render_score_chart(score, breakdown)}\n"
        f"{sentiment_block}"
        f"🔗 Birdeye: https://birdeye.so/token/{address}"
    )


def format_graduation_alert(
    token: dict,
    score: int,
    breakdown: dict,
    sentiment_score: int = 0,
    sentiment_breakdown: dict | None = None,
) -> str:
    """Layer 1 standalone alert — token crossed graduation threshold.

    Fires immediately when a token passes Layer 1, before (and independently of)
    any Layer 2 smart-money confirmation. Intended as an early heads-up.

    ``sentiment_breakdown``, when supplied, renders a Layer 4 buzz panel below
    the score chart.
    """
    address = token.get('address', '')
    symbol = _md_escape(token.get('symbol') or '???')
    price = float(token.get('price') or 0.0)
    market_cap = float(token.get('market_cap') or 0.0)
    holders = int(breakdown.get('holders') or 0)
    buy_pressure = float(breakdown.get('buy_pressure_ratio') or 0.0)
    sentiment_block = (
        f'{_render_sentiment_bar(sentiment_score, sentiment_breakdown)}\n'
        if sentiment_breakdown else ''
    )
    return (
        f"🎯 *GRADUATION WATCH*\n"
        f"Token: ${symbol} (`{address}`)\n"
        f"Price: ${price:.6f} | MCap: ${market_cap:,.0f}\n"
        f"✅ Holders: {holders:,} | Buy Pressure: {buy_pressure*100:.0f}%\n"
        f"\n"
        f"{_render_score_chart(score, breakdown)}\n"
        f"{sentiment_block}"
        f"⏳ Awaiting smart-money confirmation for full alert\n"
        f"🔗 Birdeye: https://birdeye.so/token/{address}"
    )


def format_smart_money_alert(token: dict, smart_money: dict) -> str:
    """Layer 2 standalone alert — tracked alpha wallet bought a Layer-1 passer.

    Fires when Layer 2 confirms on a Layer-1-passer token. The combined
    SMART BIRD ALERT fires shortly after with full liquidity context; this
    alert is the earlier per-layer signal.
    """
    address = token.get('address', '')
    symbol = _md_escape(token.get('symbol') or '???')
    wallet = smart_money.get('wallet') or ''
    short_wallet = (
        f'{wallet[:4]}...{wallet[-4:]}' if len(wallet) >= 8 else (wallet or 'unknown')
    )
    short_wallet = _md_escape(short_wallet)
    minutes_ago = int(smart_money.get('minutes_ago') or 0)
    amount_usd = smart_money.get('amount_usd')
    amount_line = ''
    if amount_usd:
        try:
            amount_line = f"\n💵 Size: ${float(amount_usd):,.0f}"
        except (TypeError, ValueError):
            amount_line = ''
    return (
        f"🐋 *SMART MONEY MOVE*\n"
        f"Token: ${symbol} (`{address}`)\n"
        f"✅ Wallet: {short_wallet} entered {minutes_ago}min ago{amount_line}\n"
        f"🔗 Birdeye: https://birdeye.so/token/{address}"
    )


def format_exit_alert(
    symbol: str,
    drop_pct: float,
    window_minutes: int,
    lp_concentration: float,
    triggered_by: str = 'both',
) -> str:
    """Build the exit alert message.

    ``triggered_by`` is one of ``'liquidity_drop'``, ``'lp_concentration'``
    or ``'both'`` and controls which lines are rendered so we don't show
    a misleading 0% drop when only the concentration breach fired.
    """
    symbol = _md_escape(symbol or '???')
    try:
        drop_pct_f = float(drop_pct)
    except (TypeError, ValueError):
        drop_pct_f = 0.0
    try:
        lp_f = float(lp_concentration)
    except (TypeError, ValueError):
        lp_f = 0.0

    lines = [f"🔴 *EXIT SIGNAL* — ${symbol}"]
    if triggered_by in ('liquidity_drop', 'both'):
        lines.append(
            f"Liquidity dropped {drop_pct_f*100:.0f}% in {int(window_minutes)}min"
        )
    if triggered_by in ('lp_concentration', 'both'):
        lines.append(f"LP concentration: {lp_f*100:.0f}%")
    return '\n'.join(lines)


def format_performance(stats: dict) -> str:
    """Build the /performance response message."""
    total = stats.get('total_alerts', 0)
    if total == 0:
        return '📊 *Smart Bird Performance*\nNo alerts tracked yet. Performance data will appear after alerts fire.'

    lines = ['📊 *Smart Bird Performance*', f'Total alerts tracked: {total}', '']

    for label, key in [('1h', '1h'), ('6h', '6h'), ('24h', '24h')]:
        checked = stats.get(f'checked_{key}', 0)
        wins = stats.get(f'wins_{key}', 0)
        avg_ret = stats.get(f'avg_return_{key}', 0.0)
        if checked == 0:
            lines.append(f'*{label}:* awaiting data')
        else:
            win_rate = (wins / checked * 100) if checked > 0 else 0
            emoji = '🟢' if avg_ret > 0 else '🔴'
            lines.append(
                f'{emoji} *{label}:* {win_rate:.0f}% win rate ({wins}/{checked}) | '
                f'avg return: {avg_ret:+.1f}%'
            )

    recent = stats.get('recent', [])
    if recent:
        lines.append('')
        lines.append('*Recent alerts:*')
        for r in recent:
            sym = _md_escape(r.get('symbol') or '???')
            alert_p = float(r.get('alert_price') or 0)
            check_1h = r.get('check_1h_price')
            if check_1h is not None and alert_p > 0:
                ret = (float(check_1h) - alert_p) / alert_p * 100
                emoji = '🟢' if ret > 0 else '🔴'
                lines.append(f'  {emoji} ${sym}: {ret:+.1f}% (1h)')
            else:
                lines.append(f'  ⏳ ${sym}: pending')

    return '\n'.join(lines)


def format_token_deep_dive(
    address: str,
    overview: dict,
    trades: list[dict],
    holders: list[dict],
    tracked: dict | None,
    score: int,
    breakdown: dict,
    sentiment_score: int = 0,
    sentiment_breakdown: dict | None = None,
) -> str:
    """Build the /token deep-dive response.

    ``sentiment_breakdown``, when supplied, renders a Layer 4 buzz panel below
    the score chart so users can see the full four-layer picture in one view.
    """
    symbol = _md_escape(overview.get('symbol') or '???')
    name = _md_escape(overview.get('name') or 'Unknown')
    price = float(overview.get('price') or 0)
    mc = float(overview.get('mc') or overview.get('marketCap') or overview.get('realMc') or 0)
    liq = overview.get('liquidity')
    if isinstance(liq, dict):
        liq_usd = float(liq.get('usd') or liq.get('USD') or liq.get('value') or 0)
    elif isinstance(liq, (int, float)):
        liq_usd = float(liq)
    else:
        try:
            liq_usd = float(liq) if liq else 0.0
        except (TypeError, ValueError):
            liq_usd = 0.0
    holder_count = int(overview.get('holder') or 0)
    change_1h = float(overview.get('priceChange1h') or overview.get('priceChange1hPercent') or 0)
    change_24h = float(overview.get('priceChange24h') or overview.get('priceChange24hPercent') or 0)

    # Buy/sell pressure from trades
    buys = sells = 0
    for t in (trades or []):
        side = None
        for key in ('side', 'txType', 'type'):
            val = t.get(key)
            if isinstance(val, str):
                low = val.lower()
                if low in ('buy', 'swap_in', 'in'):
                    side = 'buy'
                elif low in ('sell', 'swap_out', 'out'):
                    side = 'sell'
                break
        if side is None and isinstance(t.get('isBuy'), bool):
            side = 'buy' if t['isBuy'] else 'sell'
        if side == 'buy':
            buys += 1
        elif side == 'sell':
            sells += 1
    total_trades = buys + sells
    buy_pct = (buys / total_trades * 100) if total_trades > 0 else 0

    # Top holders concentration
    top_holder_pct = 0.0
    for h in (holders or []):
        val = h.get('percent') or h.get('percentage') or h.get('share') or h.get('percentOfSupply') or 0
        try:
            fv = float(val)
        except (TypeError, ValueError):
            continue
        if fv > 1.0:
            fv = fv / 100.0
        top_holder_pct += fv

    # Pipeline status
    pipeline_line = ''
    if tracked:
        status = tracked.get('status', 'unknown')
        pipeline_line = f'\n📌 Pipeline status: *{status}*'

    change_1h_emoji = '🟢' if change_1h > 0 else ('🔴' if change_1h < 0 else '⚪')
    change_24h_emoji = '🟢' if change_24h > 0 else ('🔴' if change_24h < 0 else '⚪')

    sentiment_block = (
        f'{_render_sentiment_bar(sentiment_score, sentiment_breakdown)}\n'
        if sentiment_breakdown else ''
    )

    return (
        f'🔎 *TOKEN DEEP DIVE*\n'
        f'\n'
        f'*{name}* (${symbol})\n'
        f'`{address}`\n'
        f'\n'
        f'💰 Price: ${price:.8f}\n'
        f'📊 MCap: ${mc:,.0f}\n'
        f'💧 Liquidity: ${liq_usd:,.0f}\n'
        f'👥 Holders: {holder_count:,}\n'
        f'{change_1h_emoji} 1h: {change_1h:+.1f}% | {change_24h_emoji} 24h: {change_24h:+.1f}%\n'
        f'\n'
        f'{_render_score_chart(score, breakdown)}\n'
        f'{sentiment_block}'
        f'🛒 Buy Pressure: {buy_pct:.0f}% buys in last {total_trades} trades\n'
        f'🏦 Top 10 Holder Concentration: {top_holder_pct*100:.0f}%'
        f'{pipeline_line}\n'
        f'\n'
        f'🔗 [Birdeye](https://birdeye.so/token/{address})'
    )
