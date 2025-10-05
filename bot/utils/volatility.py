import numpy as np

def rolling_volatility(prices, window=10):
    if len(prices) < 3:
        return 0.0
    log_returns = np.diff(np.log(prices[-window:]))
    return float(np.std(log_returns)) * 100
