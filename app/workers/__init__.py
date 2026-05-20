"""Background workers: scheduler, balance loop, replays.

Run as separate containers so the API can scale horizontally without
duplicating scheduler ticks.
"""
