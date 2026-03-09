from typing import List
import os
import shutil
import struct
from pathlib import Path

from orchestration.checkpoint import Checkpoint

from minio import Minio

# How does this look in MinIO?

# MinIO checkpoints bucket:
# uuid_1/
#   ...
#   .img files
# uuid_2/
#   ...
#   .img files

# parent-child chain lives in the Checkpoint objects in the pool:

# Checkpoint(path="uuid_A", parent_path=None); full dump, root of chain
# Checkpoint(path="uuid_B", parent_path="uuid_A"); child of uuid_A
# Checkpoint(path="uuid_C", parent_path="uuid_B"); child of uuid_B

# ------------------------------------

# Dump Flow (build_dump_cmd() -> run cmd -> upload_entry()) in orchestrator.py:

# First dump: full dump (entries is empty — cold start)
# Subsequent dumps: incremental until total chain depth (restored_depth + new deltas) >= max_chain_length
# Once that threshold is hit, the next dump is a full dump (new chain root)

# ------------------------------------

# Restore flow (setup_for_restore() -> build_restore_cmd() -> run cmd) in orchestrator.py:

# How to restore uuid_C? Call setup_for_restore with checkpoint C; it will walk parent_path links
# to build the chain [C, B, A], then reverse it [A, B, C]

# Then download + create symlinks:
# Download A to ./chain/uuid_A
# Download B to ./chain/uuid_B, create symlink ./chain/uuid_B/parent -> ../uuid_A
# Download C to ./chain/uuid_C, create symlink ./chain/uuid_C/parent -> ../uuid_B

# setup_for_restore will return ./chain/uuid_C as the restore_dir, and the 
# CRIU restore command will automatically follow parent symlinks to find the 
# full chain of images (CRIU specifically looks for a symlink called parent inside the image directory)

class IncrementalChain:
    """Manages a SINGLE local chain of CRIU checkpoint directories"""

    # Opt 1: if a delta's pages exceed this fraction of the last full dump size, force a
    # fresh full dump next cycle instead of continuing the chain.
    SIZE_RATIO_THRESHOLD = 0.80

    # Opt 4: if more than this fraction of pages are dirty after the first post-restore
    # request, the workload is too write-heavy for incremental to help; force full dumps.
    DIRTY_RATE_THRESHOLD = 0.70

    def __init__(self, base_dir="./chain", max_chain_length=5):
        self.base_dir = base_dir
        self.entries = []
        self.restore_dir = None
        self.restored_depth = 0
        self.max_chain_length = max_chain_length

        # Opt 1: size-threshold guard state
        self.last_full_dump_size: int = 0
        self._force_full_next: bool = False

        # Opt 4: post-restore dirty-rate sampling state
        self.pending_dirty_check: bool = False

        if os.path.exists(base_dir):
            shutil.rmtree(base_dir)
            print(f"IncrementalChain: cleared stale chain dir {base_dir}")
        os.makedirs(base_dir)

    def setup_for_restore(self, client: Minio, checkpoint: Checkpoint, pool: List[Checkpoint]):
        """Download full ancestor chain from MinIO for this checkpoint

        Walks checkpoint.parent_path up the chain, downloads each entry,
        creates parent symlinks, and sets self.restore_dir to the
        target dentry

        Args:
            client: MinIO client
            checkpoint: target Checkpoint to restore from
            pool: full pool list (to resolve parent_path -> Checkpoint objects)
        Returns:
            str: local directory path for criu restore -D
        
        """

        # Walk from leaf to root, collecting the chain in reverse order
        path_index = {c.path: c for c in pool}
        chain = []
        current = checkpoint
        while current is not None:
            chain.append(current)
            current = path_index.get(current.parent_path) if current.parent_path else None
        chain.reverse()  # now ordered root -> ... -> leaf

        # Download each entry and create parent symlinks.
        # For ancestor entries (not the leaf), only pages-*.img and pagemap-*.img
        # are needed by CRIU to walk the page chain; all other metadata is read
        # from the leaf only.
        prev_local_dir = None
        leaf_path = chain[-1].path
        for entry in chain:
            local_dir = os.path.join(self.base_dir, entry.path)
            os.makedirs(local_dir, exist_ok=True)

            is_leaf = entry.path == leaf_path
            objects = client.list_objects("checkpoints", prefix=entry.path, recursive=True)
            for obj in objects:
                filename = obj.object_name.split("/", maxsplit=1)[1]
                if not is_leaf and not (filename.startswith("pages-") or filename.startswith("pagemap-")):
                    continue
                client.fget_object("checkpoints", obj.object_name, os.path.join(local_dir, filename))

            if prev_local_dir is not None:
                rel = os.path.relpath(prev_local_dir, local_dir)
                os.symlink(rel, os.path.join(local_dir, "parent"))

            prev_local_dir = local_dir

        self.restore_dir = prev_local_dir
        self.restored_depth = len(chain) - 1  # root is depth 0
        # Keep only the leaf in entries so is_full_dump() doesn't trigger based on
        # restored chain depth. The symlink chain is already set up locally, so
        # entries[-1] is sufficient as prev_dir for the next incremental dump.
        self.entries = [prev_local_dir]
        return self.restore_dir

    def clear_soft_dirty(self, pid):
        """Reset all soft-dirty bits so the next dump tracks only new changes
        from the immediately previous dump.

        Also arms the post-restore dirty-rate check (Opt 4): after the next
        request completes, check_dirty_rate() should be called to measure how
        much memory the workload dirtied, and decide whether incremental dumps
        are worth continuing.
        """

        clear_refs_path = Path(f"/proc/{pid}/clear_refs")
        try:
            with open(clear_refs_path, 'w') as f:
                f.write('4')
            self.pending_dirty_check = True  # Opt 4: arm the check
        except Exception as e:
            print(f"Error clearing soft-dirty bits for pid {pid}: {e}")

    def upload_entry(self, client: Minio, entry_dir: str, minio_path: str):
        """Upload a single chain's entry files to MinIO"""

        if not os.path.isdir(entry_dir):
            raise ValueError(f"Invalid entry_dir {entry_dir}")

        for root, dirs, files in os.walk(entry_dir):
            for file in files:
                local_path = os.path.join(root, file)

                try:
                    client.fput_object(
                        bucket_name="checkpoints",
                        object_name=os.path.join(minio_path, file),
                        file_path=local_path
                    )
                except Exception as e:
                    print(f"Error uploading {local_path} to MinIO: {e}")

    def get_entry_size(self, entry_dir):
        """Return total bytes of pages-*.img in entry_dir"""

        if not os.path.isdir(entry_dir):
            raise ValueError(f"Invalid entry_dir {entry_dir}")

        total_size = 0

        for root, dirs, files in os.walk(entry_dir):
            for file in files:
                if file.startswith("pages-") and file.endswith(".img"):
                    # TODO: should we use pathlib for this instead?
                    local_path = os.path.join(root, file)
                    total_size += os.path.getsize(local_path) # gets size in bytes

        return total_size

    def get_chain_depth(self, checkpoint: Checkpoint, pool: List[Checkpoint]) -> int:
        """Walk parent_path chain to compute depth of a checkpoint"""

        depth = 0
        current = checkpoint
        path_index = {c.path: c for c in pool}

        while current is not None and current.parent_path is not None:
            current = path_index.get(current.parent_path)
            if current is None:
                break
            depth += 1

        return depth

    def build_dump_cmd(self, pid: int, output_dir: str, prev_dir: str = None) -> str:
        """Construct CRIU dump command with incremental flags when applicable.

        Args:
            pid: PID of the process to dump
            output_dir: directory to write dump images to (-D flag)
            prev_dir: parent checkpoint directory for incremental dump, or None for full dump
        """
        cmd = f"criu dump -t {pid} -v3 --tcp-established --leave-running --track-mem -D {output_dir}"
        if prev_dir is not None:
            rel = os.path.relpath(prev_dir, output_dir)
            cmd += f" --prev-images-dir {rel}"
        return cmd

    def build_restore_cmd(self) -> str:
        """Construct CRIU restore command. CRIU follows parent symlinks automatically."""
        return f"criu restore --restore-detached --tcp-close -d -v3 -D {self.restore_dir}"

    def record_dump(self, entry_dir: str, was_full: bool):
        """Update size-threshold state after a successful dump (Opt 1).

        If the dump was a full dump, record its page size as the baseline for
        subsequent delta comparisons.  If it was an incremental dump and its
        page size is >= SIZE_RATIO_THRESHOLD of the last full dump, flag the
        next dump to be forced full (the delta is too large to be worthwhile).
        """
        size = self.get_entry_size(entry_dir)
        if was_full:
            self.last_full_dump_size = size
            self._force_full_next = False
            print(f"IncrementalChain: full dump size recorded as {size} bytes")
        elif self.last_full_dump_size > 0:
            ratio = size / self.last_full_dump_size
            print(f"IncrementalChain: delta size {size} bytes ({ratio:.1%} of full dump)")
            if ratio >= self.SIZE_RATIO_THRESHOLD:
                self._force_full_next = True
                print(
                    f"IncrementalChain: delta too large ({ratio:.1%} >= {self.SIZE_RATIO_THRESHOLD:.0%}), "
                    "forcing full dump next cycle"
                )

    def count_soft_dirty_ratio(self, pid: str) -> float:
        """Estimate the fraction of mapped pages that are soft-dirty (Opt 4).

        Reads /proc/{pid}/maps for the virtual address layout, then reads
        /proc/{pid}/pagemap (bit 55 = soft-dirty) for each region.
        Returns a value in [0.0, 1.0]; returns 1.0 on any error so the caller
        defaults to the conservative (force-full-dump) path.
        """
        PAGE_SIZE = 4096
        total_pages = 0
        dirty_pages = 0
        try:
            maps_path = f"/proc/{pid}/maps"
            pagemap_path = f"/proc/{pid}/pagemap"
            with open(maps_path, 'r') as maps_file, open(pagemap_path, 'rb') as pagemap_file:
                for line in maps_file:
                    parts = line.split()
                    if not parts:
                        continue
                    addrs = parts[0].split('-')
                    start = int(addrs[0], 16)
                    end = int(addrs[1], 16)
                    num_pages = (end - start) // PAGE_SIZE
                    if num_pages <= 0:
                        continue
                    pagemap_file.seek((start // PAGE_SIZE) * 8)
                    data = pagemap_file.read(num_pages * 8)
                    count = len(data) // 8
                    if count == 0:
                        continue
                    entries = struct.unpack(f'{count}Q', data[:count * 8])
                    for entry in entries:
                        if entry & (1 << 55):  # soft-dirty bit
                            dirty_pages += 1
                    total_pages += count
        except Exception as e:
            print(f"count_soft_dirty_ratio error for pid {pid}: {e}")
            return 1.0
        if total_pages == 0:
            return 0.0
        return dirty_pages / total_pages

    def check_dirty_rate(self, pid: str):
        """If a dirty-rate check is pending, measure and act on it (Opt 4).

        Should be called once after the first request following a restore +
        clear_soft_dirty().  If the dirty ratio exceeds DIRTY_RATE_THRESHOLD,
        force the next dump to be full (the workload writes too much memory for
        incremental deltas to produce meaningful savings).
        """
        if not self.pending_dirty_check:
            return
        self.pending_dirty_check = False
        ratio = self.count_soft_dirty_ratio(pid)
        print(f"IncrementalChain: post-restore dirty ratio = {ratio:.1%}")
        if ratio >= self.DIRTY_RATE_THRESHOLD:
            self._force_full_next = True
            print(
                f"IncrementalChain: dirty ratio {ratio:.1%} >= {self.DIRTY_RATE_THRESHOLD:.0%}, "
                "forcing full dump next cycle"
            )

    def is_full_dump(self) -> bool:
        """Whether the next dump will be full (no parent) or incremental.

        Returns True if:
        - cold start (no entries yet)
        - max chain depth reached
        - size-threshold guard fired (Opt 1): last delta was >= SIZE_RATIO_THRESHOLD of full
        - dirty-rate check fired (Opt 4): post-restore dirty ratio too high
        """
        if self._force_full_next:
            return True
        if len(self.entries) == 0:
            return True  # cold start
        # entries[0] is the restored leaf (or the full dump just taken).
        # len(entries) - 1 counts new deltas added in this container run.
        total_depth = self.restored_depth + (len(self.entries) - 1)
        return total_depth >= self.max_chain_length