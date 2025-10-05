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
