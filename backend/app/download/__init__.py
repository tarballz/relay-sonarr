"""Download-client adapters.

Sonarr knows whether a torrent is *in* the client; only the client itself knows
whether the swarm is alive. Relay reads that second view to tell a download that
is merely slow from one that is never going to finish.
"""
