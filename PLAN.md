# Incremental Delta Snapshots — Implementation Plan

## Context

Pronghorn maintains a pool of CRIU checkpoints at various invocation counts. Currently each checkpoint is a full independent dump, but consecutive checkpoints share the vast majority of pages (interpreter, stdlib, heap). CRIU supports incremental dumps via soft-dirty page tracking, dumping only changed pages. The `IncrementalChain` class and `Checkpoint.parent_path` field exist but are not wired into the actual lifecycle. This plan fixes the gaps identified in the review (missing CRIU commands, no max depth enforcement, upload bug, broken error handling, no integration with main.py/orchestrator.py) and delivers a working end-to-end incremental delta snapshot system.

---

## Step 1 (Jared, DONE): Fix `IncrementalChain` bugs and gaps

**Why:** The `IncrementalChain` class is the core data structure that manages the chain of parent-to-child checkpoint relationships, constructs CRIU dump/restore commands with the correct incremental flags, and handles uploading/downloading checkpoint files. Without fixing its bugs — broken chain traversal, incomplete file uploads, missing CRIU command builders, and no max-depth enforcement — incremental dumps would either fail to produce valid CRIU images, fail to restore, or grow unbounded chains that degrade restore performance.

**File:** `agent-python/incremental.py`

**Depends on:** Nothing — this is the foundational data structure.

### 1a. Add `max_depth` parameter and enforce it
- Add `max_depth` param to `__init__` (default 5)
- Update `is_full_dump()` to return `True` when `self.restored_depth >= self.max_depth` OR `len(self.entries) == 0`
- Set `self.restored_depth` at the end of `setup_for_restore()` to `len(chain) - 1` (depth of the leaf checkpoint)

### 1b. Fix `upload_entry` — upload ALL files, not just `pages-*.img`
- Remove the `pages-*.img` filter. A CRIU dump directory contains `core-*.img`, `mm-*.img`, `fdinfo-*.img`, `pstree.img`, etc. — all are required for restore.
- Keep `get_entry_size` filtered to `pages-*.img` (that's a metrics method, measuring delta size is correct).

### 1c. Fix `get_chain_depth` — handle broken chains
- Change `path_index[current.parent_path]` to `path_index.get(current.parent_path)` with a `None` check, matching the pattern in `setup_for_restore`.

### 1d. Fix symlink creation for nested paths
- In `setup_for_restore`, compute the relative path between `local_dir` and `prev_local_dir` using `os.path.relpath` instead of assuming flat sibling directories.

### 1e. Add `build_dump_cmd` method
Constructs the CRIU dump command. Logic:
```python
def build_dump_cmd(self, pid: int, output_dir: str, prev_dir: str = None) -> str:
    cmd = f"criu dump -t {pid} -v3 --tcp-established --leave-running -D {output_dir}"
    if prev_dir is not None:
        # --prev-images-dir is relative to -D
        rel = os.path.relpath(prev_dir, output_dir)
        cmd += f" --prev-images-dir {rel} --track-mem"
    return cmd
```
- When `is_full_dump()` is True → `prev_dir=None` (full dump, same as today)
- When incremental → `prev_dir` points to the parent's local checkpoint directory

### 1f. Add `build_restore_cmd` method
```python
def build_restore_cmd(self) -> str:
    return f"criu restore --restore-detached --tcp-close -d -v3 -D {self.restore_dir}"
```
CRIU automatically follows `parent` symlinks during restore — no extra flags needed.

---

## Step 2 (Jared, DONE): Add incremental mode to the configuration/strategy layer

**Why:** The strategy configuration system is how the agent learns which orchestration mode to use. Currently, `cr_deserialize` in `utils.py` parses the ENV strategy string to instantiate strategy objects, and `RequestCentricStrategy` serializes/deserializes its parameters across container lifecycles. Neither has any concept of `incremental` or `max_chain_depth`. Without wiring these parameters through the configuration layer, the agent will never know incremental mode is enabled — Steps 1, 3–5 build all the machinery but nothing ever turns it on.

**Files:** `agent-python/orchestration/utils.py`, `agent-python/orchestration/strategies/request_centric.py`

**Depends on:** Nothing (configuration plumbing, independent of Step 1).

### 2a. Parse `incremental` and `max_chain_depth` from the ENV strategy string
In `cr_deserialize` (`utils.py:9-19`), the empty-payload branch does literal string comparisons and falls through to a bare `RequestCentricStrategy(Parameters(), [])`. It discards all `&`-separated parameters.

Fix: parse the strategy string into key-value pairs:
```python
strategy_env = os.getenv("ENV").split(",")[0]
params = {}
if "&" in strategy_env:
    parts = strategy_env.split("&")
    strategy_name = parts[0]
    for part in parts[1:]:
        key, value = part.split("=")
        params[key] = value
else:
    strategy_name = strategy_env
```
Then pass `incremental=params.get("incremental", "false") == "true"` and `max_chain_depth=int(params.get("max_chain_depth", "5"))` to the strategy constructor, along with `max_capacity`.

### 2b. Add `incremental` and `max_chain_depth` fields to `RequestCentricStrategy`
- Add `incremental: bool = False` and `max_chain_depth: int = 5` parameters to `__init__`
- Store as instance variables
- Include in `extra_state` property so they survive serialization:
  ```python
  @property
  def extra_state(self) -> dict:
      return {
          ...existing fields...,
          "incremental": self.incremental,
          "max_chain_depth": self.max_chain_depth,
      }
  ```
- Update the deserialized branch in `cr_deserialize` (line 29-38) to read these fields from the JSON payload and pass them to the constructor

### 2c. Propagate incremental config to the agent
The orchestrator's `on_container_started()` return dict must include `incremental` and `max_chain_depth` so that `main.py` knows whether to use `IncrementalChain`. This replaces the separate `MAX_CHAIN_DEPTH` env var approach (resolves flaw H1 — single source of truth through the strategy object).

---

## Step 3 (Erin): Modify `orchestrator.py` to pass chain metadata

**Why:** The orchestrator is the intermediary between the scheduling logic (which selects which checkpoint to restore) and the agent (which performs the actual CRIU operations). Currently it only passes the checkpoint's storage path to the agent — but incremental restore requires the full `Checkpoint` object (to read `parent_path`) and the pool list (to walk the ancestor chain). Without this metadata flowing through the orchestrator, the agent has no way to know whether a checkpoint is incremental or which ancestors to download, and newly created checkpoints cannot record their `parent_path` back into the pool.

**File:** `agent-python/orchestrator.py`

**Depends on:** Step 1 (the orchestrator must serialize `Checkpoint` objects whose `parent_path` field is consumed by the fixed `IncrementalChain`) and Step 2 (the strategy object now carries `incremental` and `max_chain_depth` fields that must be included in the return dict).

### 3a. `on_container_started()` — return chain info and incremental config
Currently returns: `{ from_checkpoint, checkpoint_location, will_checkpoint_at }`

Add to the return dict:
- `checkpoint_object`: the serialized `Checkpoint` (including `parent_path`) — use `checkpoint.serialize()`
- `pool`: the serialized pool list — use `[chkpt.serialize() for chkpt in pool]`
- `incremental`: from `orch.strategy.incremental`
- `max_chain_depth`: from `orch.strategy.max_chain_depth`

### 3b. `on_container_checkpoint(path, parent_path)` — accept parent_path
- Add `parent_path` parameter (default `None` for backward compatibility)
- Set `checkpoint.parent_path = parent_path` when creating the new `Checkpoint` object
- This is the key field that links the chain together in the pool

---

## Step 4 (Mike): Wire `IncrementalChain` into `main.py`

**Why:** `main.py` is the agent entrypoint that actually executes CRIU dump and restore operations. Currently it uses hardcoded full-dump commands and flat single-checkpoint downloads. Without integrating `IncrementalChain` here, the agent will never issue `--prev-images-dir` or `--track-mem` flags during dumps, never download ancestor chains before restoring, and never call `clear_soft_dirty` after restore — meaning incremental deltas are never actually created or used at runtime.

**File:** `agent-python/main.py`

**Depends on:** Step 1 (uses `IncrementalChain` methods), Step 2 (strategy carries `incremental`/`max_chain_depth`), and Step 3 (consumes `checkpoint_object`, `pool`, `incremental`, `max_chain_depth` from the orchestrator, passes `parent_path` back via `on_container_checkpoint`).

### 4a. Add module-level globals for chain state
Declare at module level (alongside existing `client: Minio`):
```python
chain: IncrementalChain = None
last_checkpoint_path: str = None  # tracks parent_path for next dump
```
This resolves flaws C4 and C5 — the chain instance and restored checkpoint path are shared between `init()` and `after_request()`.

### 4b. Restore path (in `init()`)
If `state["incremental"]` is True and `state["from_checkpoint"]` is True:
1. Instantiate `IncrementalChain(base_dir="./chain", max_depth=state["max_chain_depth"])`
2. Deserialize `checkpoint_object` and `pool` from the orchestrator return dict (requires importing `Checkpoint` and calling `Checkpoint.deserialize(payload, client)`)
3. Call `chain.setup_for_restore(client, checkpoint, pool)` — downloads the full ancestor chain and creates symlinks
4. Use `chain.build_restore_cmd()` instead of hardcoded `criu restore` command
5. After restore succeeds, call `get_pypy_pid()` and then `chain.clear_soft_dirty(pid)` — add a short retry if `pgrep` fails immediately after detached restore
6. Set `last_checkpoint_path = state["checkpoint_location"]`

If `state["incremental"]` is True but `state["from_checkpoint"]` is False (cold start):
1. Instantiate `IncrementalChain(base_dir="./chain", max_depth=state["max_chain_depth"])` with empty entries
2. Proceed with cold start as normal
3. `last_checkpoint_path` stays `None`

If `state["incremental"]` is False, use existing full-dump logic unchanged.

### 4c. Dump path (in `after_request()`)
If `chain` is not None (incremental mode):
1. Clean and create output dir: `rm -rf ./chain/{checkpoint_location} && mkdir -p ./chain/{checkpoint_location}`
2. Determine `prev_dir`: if `chain.is_full_dump()` → `None`, else → `chain.entries[-1]`
3. Call `chain.build_dump_cmd(pid, output_dir, prev_dir)` and execute
4. Call `chain.upload_entry(client, output_dir, checkpoint_location)` to upload to MinIO
5. Append `output_dir` to `chain.entries`
6. Pass `parent_path` to `on_container_checkpoint()`:
   - If full dump: `parent_path=None`
   - If incremental: `parent_path = last_checkpoint_path`
7. Update `last_checkpoint_path = checkpoint_location`

If `chain` is None, use existing full-dump logic unchanged.

---

## Step 5 (Dao): Chain-aware pool pruning in `request_centric.py`

**Why:** The pool pruning logic currently treats each checkpoint as independent and can freely delete any checkpoint to stay within capacity. With incremental deltas, checkpoints form parent-child chains — deleting a parent checkpoint orphans all its descendants, making them unrestorable (CRIU restore follows `parent` symlinks and fails if ancestor images are missing). Without chain-aware pruning, the system will silently corrupt the pool by deleting checkpoints that other checkpoints depend on, leading to restore failures at runtime.

**File:** `agent-python/orchestration/strategies/request_centric.py`

**Depends on:** Steps 1–4 — pruning operates on `parent_path` fields that are set by the orchestrator (Step 3) and populated during the dump lifecycle (Step 4). The chain depth logic relies on `IncrementalChain.is_full_dump` (Step 1).

### 5a. Build chain dependency graph before pruning
In `_prune_pool()`, before selecting which checkpoints to keep:
1. Build a `children` map: `{checkpoint.path: [child checkpoints]}` by scanning `parent_path` fields
2. Build a `roots` set: checkpoints where `parent_path is None` (full dumps)

### 5b. Protect chain integrity during pruning
When deciding to delete a checkpoint:
- If it has children that are being kept → do NOT delete it (would break their restore chain)
- If deleting, also delete all its descendants (cascade)

Simplest safe approach: prune at the **chain level**, not individual checkpoint level.
- Group checkpoints by their root (walk each to its root)
- Score each chain by the best-performing checkpoint it contains
- Keep/discard entire chains
- Within kept chains, individual checkpoints can still be pruned if they are leaves with no children

### 5c. Guard `reset()` for chain safety
`reset()` (`request_centric.py:153-156`) calls `chkpt.delete()` on every checkpoint. This is safe (it deletes the entire pool), but document that it must always delete all checkpoints — never a subset — to avoid orphaning descendants.

### 5d. Enforce max chain depth at pruning time
- If any chain exceeds `max_depth`, mark it for replacement: keep it for now but flag that the next dump from its leaf should be a full dump (this is already handled by `is_full_dump` in Step 1a)

---

## Step 6 (all?): Evaluation changes in `synthetic_run.py`

**Why:** Incremental deltas are only valuable if they actually reduce storage usage without unacceptably degrading restore latency. Without adding incremental strategy variants to the benchmark suite and collecting storage and chain-depth metrics, there is no way to verify that the implementation works end-to-end under realistic workloads or to quantify the storage savings vs. restore-time tradeoff that justifies the added complexity.

**File:** `synthetic_run.py`

**Depends on:** Steps 1–5 — the entire incremental delta system must be functional before it can be benchmarked end-to-end.

### 6a. Add incremental strategy variants
Add to `STRATEGIES`:
```python
"request_centric&max_capacity=12&incremental=true&max_chain_depth=5"
```
Compare against the existing `request_centric&max_capacity=12` (full dumps) to measure delta.

### 6b. Add storage metrics
After each benchmark run, before cleanup:
- Query MinIO for total storage used by checkpoints bucket
- Log to CSV: add `storage_bytes` column

### 6c. Add chain depth metrics
- Log the chain depth of each restored checkpoint to CSV: add `chain_depth` column
- This enables analysis of restore latency vs. chain depth

---

## Files Modified (Summary)

| File | Changes |
|------|---------|
| `agent-python/incremental.py` | Fix bugs, add max_depth, add build_dump_cmd/build_restore_cmd, fix upload_entry |
| `agent-python/orchestration/utils.py` | Parse `incremental`/`max_chain_depth` from ENV strategy string |
| `agent-python/orchestration/strategies/request_centric.py` | Add `incremental`/`max_chain_depth` fields, chain-aware pruning |
| `agent-python/orchestrator.py` | Pass chain metadata (checkpoint object, pool, parent_path, incremental config) |
| `agent-python/main.py` | Wire IncrementalChain into restore and dump paths, add chain/path globals |
| `agent-python/orchestration/checkpoint.py` | No changes needed (parent_path already exists) |
| `synthetic_run.py` | Add incremental strategy variants, storage/depth metrics |

---

## Verification

1. **Unit test chain logic** (Step 1): Create a test that builds a chain of 3 checkpoints, verifies `get_chain_depth` returns correct values, `is_full_dump` triggers at max depth, and `build_dump_cmd` produces correct flags
2. **Config round-trip** (Step 2): Verify that `cr_deserialize` correctly parses `"request_centric&max_capacity=12&incremental=true&max_chain_depth=5"`, that the resulting strategy has `incremental=True` and `max_chain_depth=5`, and that these survive a serialize/deserialize cycle
3. **Integration test with CRIU** (Steps 3–4): On a Linux machine, run a simple process, do a full dump, clear soft-dirty, do an incremental dump, verify the incremental dump dir is smaller, then restore from the incremental and verify the process resumes
4. **Pruning safety** (Step 5): Verify that deleting a chain doesn't leave orphaned children in the pool by checking that all `parent_path` references resolve after pruning
5. **End-to-end via synthetic_run.py** (Step 6): Run the benchmark comparing `request_centric` (full) vs `request_centric&incremental=true` — verify storage reduction and comparable restore latency

---

## Considerations

### Can aggressive pruning on original Pronghorn beat incremental deltas on storage?

**Short answer:** Yes on raw bytes, but at the cost of latency — making it the wrong comparison.

Pronghorn's pruning (Section 3.4 of the paper, `_prune_pool` in `request_centric.py`) is already aggressive: it keeps only the top `p=40%` by performance plus `gamma=10%` random from the remainder each time the pool exceeds `C`. With `C=12`, the pool oscillates between ~6 and 13 checkpoints, averaging ~9.5 full dumps in storage.

The paper explicitly acknowledges that cloud providers can reduce storage by lowering `C` (Section 5.3: *"setting C=2 instead of C=12"*). A provider could set `C=2` with full dumps and achieve storage far below any incremental system at `C=12`:

| Config | Storage (Python, ~55MB/snapshot) | Restore points |
|--------|----------------------------------|----------------|
| Full dumps, `C=12` | ~660MB | 12 |
| Incremental, `C=12`, depth 5 | ~215MB (3 full + 9 deltas) | 12 |
| Full dumps, `C=2` | ~110MB | 2 |

However, the paper's entire contribution demonstrates that a diverse checkpoint pool is essential for finding high-performance snapshots via exploration-exploitation. With `C=2`, the system retains only 2 restore points, collapsing the exploration-exploitation tradeoff and degrading latency — the very metric Pronghorn optimizes (37.2% median improvement at `C=12`).

**The value proposition of incremental deltas is not to compete with capacity reduction, but to make it unnecessary.** Incremental deltas achieve `C=2`-level storage costs (~215MB vs ~660MB, a ~67% reduction) while preserving `C=12`-level checkpoint diversity and latency. The correct comparison is always at the same `C`: same number of restore points, same latency characteristics, but dramatically less storage per checkpoint.

**Key evaluation implication:** Step 6's benchmarks must compare at the same `C` to be meaningful. If we also include a `C=2` full-dump baseline, we should report both storage *and* latency to show that our incremental system at `C=12` achieves comparable storage to `C=2` full dumps without the latency penalty.

### Chain-level pruning degrades pool diversity

**The problem:** Step 5b proposes pruning at the chain level — keeping or discarding entire chain-families as units. With `C=12` and `max_chain_depth=5`, a chain of depth 5 consumes 5 pool slots (1 full dump root + 4 incremental deltas). The pool can hold at most 2 independent chains of length 5 (10 slots) plus 2 loose checkpoints. The structure is actually a forest of trees, not independent linear chains, since multiple containers can restore from the same checkpoint and branch — so the pool likely contains 2–3 tree roots with various branch depths.

Pronghorn's pruning algorithm scores each checkpoint individually, keeps the top `p=40%` by performance, and randomly samples `gamma=10%` from the rest for exploration. With 12 independent full dumps, that's 12 individually scorable, individually prunable units — maximum diversity and exploration surface. Chain-level pruning collapses this to 2–3 pruning units. "Keep top 40%" becomes "keep 1 tree." Exploration is effectively dead — the system locks into whichever chain-family scored well early, undermining Pronghorn's core exploration-exploitation mechanism.

**Possible mitigations:**

1. **Leaf-only pruning** — only prune checkpoints with no children. Chain integrity is guaranteed by construction. Roots and intermediates are "pinned" while they have descendants but become prunable once their children are removed. This recovers per-checkpoint granularity on the frontier of each tree.

2. **Shorter max depth (2–3 instead of 5)** — more roots, more independent pruning units. With depth 2 and `C=12`, ~6 roots exist, much closer to original pruning dynamics. Less storage savings per chain.

3. **Hybrid pool** — reserve some slots for independent full dumps (freely prunable for exploration) and some for incremental chains (storage savings on proven high-performers).

4. **Promote-on-prune** — when deleting a parent, merge its pages into one child to make it a standalone full dump. Expensive (requires CRIU `dedup` or manual page merging) but fully preserves diversity.

#### Leaf-only pruning: advantages and disadvantages

**Advantages:**
- Recovers per-checkpoint granularity on the pool frontier — any leaf can be individually scored and pruned, preserving Pronghorn's exploration-exploitation dynamics far better than chain-level pruning.
- Chain integrity is guaranteed by construction: you never delete a node that has children, so no descendant is ever orphaned. No need for cascade-delete logic or chain-grouping (Step 5a/5b simplifies significantly).
- Simple to implement — a single `has_children(checkpoint, pool)` check before allowing deletion.
- Graceful degradation: even if some slots are pinned, the remaining prunable leaves still provide meaningful diversity compared to the 2–3 pruning units of chain-level pruning.

**Disadvantages:**
- Roots and intermediate nodes are "pinned" — they occupy pool slots but cannot be removed while they have descendants. This is dead weight: unprunable checkpoints that may have poor performance scores but still consume capacity.
- Slow reclamation: freeing a depth-5 chain takes up to 5 pruning rounds (peel one leaf per round). If the pool is full of deep chains with pinned intermediates, it may take many rounds before enough capacity is freed for new exploration.
- A poorly-performing root that spawned many branches cannot be removed until *all* its descendants are pruned first — even if the root is the worst checkpoint in the pool. The pruning algorithm must work around it.
- Can lead to pool stagnation: if most checkpoints are pinned intermediates, the effective number of prunable candidates shrinks, reducing the pruning algorithm's ability to make room for new checkpoints. In the worst case (all chains at max depth, heavy branching), nearly all pool slots could be pinned.
- Storage reclamation is gradual rather than immediate — if a burst of new checkpoints demands space, leaf-only pruning may not free slots fast enough, potentially requiring a fallback to chain-level or cascade pruning.

**Recommendation:** Leaf-only pruning is the strongest starting point — it's simple, safe, and preserves the most diversity. Shorter `max_chain_depth` (2–3) further mitigates the pinning and slow-reclamation downsides by limiting how much dead weight any single chain can create. The combination of leaf-only pruning + low max depth likely captures most of the storage savings while preserving most of the pool diversity. Step 6 benchmarks should compare chain-level vs. leaf-only pruning at various depths to quantify the tradeoff empirically.

---

## Flaws and Missing Steps

The following issues were identified by reviewing the plan against the actual codebase. Issues marked RESOLVED have been addressed by plan revisions; remaining OPEN issues must still be addressed during implementation.

### Critical (would cause runtime failures)

**~~C1. `cr_deserialize` does not parse `incremental` or `max_chain_depth` from ENV~~ RESOLVED by new Step 2**

**C2. `--track-mem` must be on full dumps too, not just incremental dumps**

Step 1e's `build_dump_cmd` only adds `--track-mem` when `prev_dir is not None` (incremental). But `--track-mem` enables kernel memory change tracking *going forward* from that dump. If the initial full dump after a cold start (where `clear_soft_dirty` was never called) omits `--track-mem`, the subsequent incremental dump has no soft-dirty information — CRIU will either error or produce a dump identical in size to a full dump. Fix: always include `--track-mem` when incremental mode is enabled, regardless of whether the current dump is full or incremental.

**C3. `is_full_dump()` uses frozen `restored_depth` — max depth never enforced within a lifecycle**

Step 1a says `is_full_dump()` should check `self.restored_depth >= self.max_depth`. But `restored_depth` is set once in `setup_for_restore()` and never updated. If a container restores from a depth-2 chain and then creates 5 more dumps, `restored_depth` is still 2 — the max-depth check never fires. The correct check should use `len(self.entries) >= self.max_depth` (since `entries` grows with each dump) or track dumps-since-restore separately.

**~~C4. `IncrementalChain` instance not declared as a global in `main.py`~~ RESOLVED by Step 4a**

**~~C5. Restored checkpoint path not carried from `init()` to `after_request()`~~ RESOLVED by Step 4a (`last_checkpoint_path` global)**

### High Severity (would cause incorrect behavior)

**~~H1. `max_chain_depth` propagation is inconsistent~~ RESOLVED by Step 2c + Step 3a (single source of truth through strategy object -> orchestrator return dict)**

**H2. `Checkpoint.delete()` has no chain-awareness guard**

`delete()` (`checkpoint.py:32-40`) removes all MinIO objects under the checkpoint's path prefix. With incremental deltas, deleting a parent's MinIO objects makes all descendants unrestorable. While Step 5 adds chain-aware pruning to `_prune_pool()`, there's no guard on `delete()` itself. Other code paths call it too — `strategy.reset()` in `request_centric.py:153-156` calls `chkpt.delete()` on every checkpoint in the pool. Step 5c documents that `reset()` is safe because it deletes everything, but any future partial-delete code path would be dangerous.

**~~H3. `clear_soft_dirty` PID acquisition not addressed in `init()`~~ RESOLVED by Step 4b item 5**

**~~H4. Dump output directory not cleaned before use~~ RESOLVED by Step 4c item 1**

### Medium Severity (edge cases / robustness)

**~~M1. Pool serialization format unspecified~~ RESOLVED by Step 3a (explicit `checkpoint.serialize()` and `[chkpt.serialize() for chkpt in pool]`) and Step 4b item 2 (explicit `Checkpoint.deserialize`)**

**M2. Mixed full/incremental pool transition not addressed**

When incremental mode is first enabled, existing checkpoints all have `parent_path=None`. New incremental checkpoints will coexist with them. Step 5's chain-aware pruning should handle this (old checkpoints are independent roots), but should be verified during testing.

### Summary Table

| # | Status | Severity | Issue | Fix location |
|---|--------|----------|-------|-------------|
| C1 | RESOLVED | Critical | `cr_deserialize` doesn't parse `incremental`/`max_chain_depth` | Step 2 |
| C2 | OPEN | Critical | `--track-mem` missing on full dumps after cold start | Step 1e: `build_dump_cmd` |
| C3 | OPEN | Critical | `is_full_dump()` uses frozen `restored_depth` | Step 1a: use `len(self.entries)` |
| C4 | RESOLVED | Critical | `IncrementalChain` not declared as global | Step 4a |
| C5 | RESOLVED | Critical | Restored checkpoint path not carried to `after_request()` | Step 4a |
| H1 | RESOLVED | High | `max_chain_depth` propagation inconsistent | Steps 2c + 3a |
| H2 | OPEN | High | `Checkpoint.delete()` has no chain guard | Step 5c (documented) |
| H3 | RESOLVED | High | `clear_soft_dirty` PID timing in `init()` | Step 4b |
| H4 | RESOLVED | High | Dump output dir not cleaned before use | Step 4c |
| M1 | RESOLVED | Medium | Pool serialization format unspecified | Steps 3a + 4b |
| M2 | OPEN | Medium | Mixed pool transition not addressed | Step 5 (verify in testing) |

---

## CRIU Flag Reference

| Flag | Purpose |
|------|---------|
| `--prev-images-dir <rel_path>` | Path (relative to `-D`) to parent checkpoint images |
| `--track-mem` | Enable kernel memory change tracking for subsequent incremental dumps |
| `--leave-running` | Keep process alive after dump (already used) |
| `--tcp-established` | Dump TCP connections (already used) |
| `--restore-detached` | Restore in background (already used) |

Sources: [CRIU Incremental Dumps](https://criu.org/Incremental_dumps), [CRIU Man Page](https://manpages.debian.org/unstable/criu/criu.8.en.html), [CRIU Memory Changes Tracking](https://criu.org/Memory_changes_tracking)
