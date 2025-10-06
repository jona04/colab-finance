from decimal import Decimal, getcontext
getcontext().prec = 60

def fmt_amount(raw: int, decimals: int, places: int = 6) -> str:
    scale = Decimal(10) ** decimals
    val = Decimal(raw) / scale
    # limita dígitos depois da vírgula para leitura
    q = Decimal(10) ** -places
    return str(val.quantize(q))

def fmt_bool(b: bool) -> str:
    return "✅" if b else "❌"

def fmt_alert_range(obs: dict) -> str:
    pr = obs["prices"]
    cur = pr["current"]; low = pr["lower"]; up = pr["upper"]

    side = "inside" if not obs["out_of_range"] else ("below" if obs["tick"] < obs["lower"] else "above")

    lines = []
    lines.append("⚠️ *RANGE ALERT*")
    lines.append(f"tick: `{obs['tick']:,}` | side: *{side}*")
    lines.append(f"USDC/ETH range: `[ {low['p_t0_t1']:.2f} , {up['p_t0_t1']:.2f} ]`")
    lines.append(f"spot USDC/ETH: `{cur['p_t0_t1']:.2f}`")
    lines.append(f"inRange: `{not obs['out_of_range']}` | pct_outside: `{obs['pct_outside_tick']:.3f}%`")
    return "\n".join(lines)