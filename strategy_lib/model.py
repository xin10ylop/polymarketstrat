"""Binary option fair value for BTC up/down windows.

Up token fair value at time t: P(S_close > K) for a driftless diffusion:
    fv = Phi( (S_t - K) / (S_t * sigma * sqrt(tau)) )
sigma: per-sqrt-second log-return vol (from Binance rolling std of 1s returns).
tau: seconds remaining to close.
"""
import numpy as np
from scipy.stats import norm


def fair_value(S, K, sigma, tau, vol_floor=1e-6):
    """Vectorized. S: spot now; K: price to beat; sigma: per-sqrt(s) logret vol; tau: s to close."""
    S = np.asarray(S, dtype="float64")
    K = np.asarray(K, dtype="float64")
    sigma = np.maximum(np.asarray(sigma, dtype="float64"), vol_floor)
    tau = np.maximum(np.asarray(tau, dtype="float64"), 1e-9)
    d = np.log(S / K) / (sigma * np.sqrt(tau))
    return norm.cdf(d)


def implied_prob_from_mid(bid, ask):
    mid = (bid + ask) / 2.0
    return mid
