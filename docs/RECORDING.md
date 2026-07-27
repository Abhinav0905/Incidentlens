# Recording the replay GIF

The README embeds `docs/replay.gif`. Record it before the public launch:

1. `incidentlens serve`, open http://127.0.0.1:8000
2. Pick **Checkout failure after payment-api deployment**, click Reconstruct incident
3. Record the replay stage while it plays (Kap or Cleanshot on macOS, ~12–15s, crop to the graph + caption)
4. Export as GIF ≤ 5 MB, save as `docs/replay.gif`

The checkout scenario reads better on camera: change → auth failure → timeouts → customer impact, plus the queue backlog spreading sideways.
