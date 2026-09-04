"""Rules for what may cross, over the same machinery that hands out ports.

The registry knows which service holds which port, for how long, and whether
its holder is still there. A firewall that knows that can bind a rule to a
service rather than to a number - and close it again when the service is gone.
"""
