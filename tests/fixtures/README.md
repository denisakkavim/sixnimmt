The legacy JSONL fixtures were emitted by the original selection/commit engine
before private `action_counted` events were introduced. Both use seed 12345,
players Alice and Bob, and an action budget of 10. Alice selects her first card;
the negotiated fixture then commits and uncommits. Its historical replay count
is one, even though the live engine counted three actions. These snapshots pin
compatibility with that old selection-only accounting behavior.
