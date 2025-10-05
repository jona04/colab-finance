from decimal import Decimal, getcontext
getcontext().prec = 80

Q96 = Decimal(2) ** 96
Q192 = Decimal(2) ** 192
MIN_TICK = -887272
MAX_TICK =  887272

# Tabela de constantes como no TickMath (em Q128.128)
C = [
    int("fffcb933bd6fad37aa2d162d1a594001", 16),
    int("fff97272373d413259a46990580e213a", 16),
    int("fff2e50f5f656932ef12357cf3c7fdcc", 16),
    int("ffe5caca7e10e4e61c3624eaa0941cd0", 16),
    int("ffcb9843d60f6159c9db58835c926644", 16),
    int("ff973b41fa98c081472e6896dfb254c0", 16),
    int("ff2ea16466c96a3843ec78b326b52861", 16),
    int("fe5dee046a99a2a811c461f1969c3053", 16),
    int("fcbe86c7900a88aedcffc83b479aa3a4", 16),
    int("f987a7253ac413176f2b074cf7815e54", 16),
    int("f3392b0822b70005940c7a398e4b70f3", 16),
    int("e7159475a2c29b7443b29c7fa6e889d9", 16),
    int("d097f3bdfd2022b8845ad8f792aa5825", 16),
    int("a9f746462d870fdf8a65dc1f90e061e5", 16),
    int("70d869a156d2a1b890bb3df62baf32f7", 16),
    int("31be135f97d08fd981231505542fcfa6", 16),
    int("9aa508b5b7a84e1c677de54f3e99bc9", 16),
    int("5d6af8dedb81196699c329225ee604", 16),
    int("2216e584f5fa1ea926041bedfe98", 16),
    int("48a170391f7dc42444e8fa2", 16),
]

def get_sqrt_ratio_at_tick(tick: int) -> int:
    if tick < MIN_TICK or tick > MAX_TICK:
        raise ValueError("tick out of range")
    abs_tick = -tick if tick < 0 else tick
    ratio = 1 << 128
    for i in range(20):
        if abs_tick & (1 << i):
            ratio = (ratio * C[i]) >> 128
    if tick > 0:
        ratio = ( (1<<256) - 1 ) // ratio
    # Q64.96 com arred.
    r_shift = ratio >> 32
    if ratio & ((1<<32) - 1) != 0:
        r_shift += 1
    if r_shift > (1<<160) - 1:
        raise OverflowError("sqrtPrice overflow")
    return int(r_shift)

def get_amounts_for_liquidity(sqrtP: int, sqrtA: int, sqrtB: int, L: int):
    if sqrtA > sqrtB:
        sqrtA, sqrtB = sqrtB, sqrtA
    sqrtP = Decimal(sqrtP)
    sqrtA = Decimal(sqrtA)
    sqrtB = Decimal(sqrtB)
    L = Decimal(L)

    if sqrtP <= sqrtA:
        # tudo em token0
        num = L * (sqrtB - sqrtA)
        den = sqrtB * sqrtA
        amount0 = (num * Q96) / den
        amount1 = Decimal(0)
    elif sqrtP < sqrtB:
        # ambos
        num0 = L * (sqrtB - sqrtP)
        den0 = sqrtB * sqrtP
        amount0 = (num0 * Q96) / den0

        num1 = L * (sqrtP - sqrtA)
        amount1 = num1 / Q96
    else:
        # tudo em token1
        amount0 = Decimal(0)
        num1 = L * (sqrtB - sqrtA)
        amount1 = num1 / Q96
    return int(amount0), int(amount1)
