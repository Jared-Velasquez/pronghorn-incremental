from typing import List
import os
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

# First dump: full dump (entries is empty); create a new IncrementalChain
# Second dump: incremental (len(entries) = 1 < max_chain_length=5)
# ...
# Sixth dump: full dump (len(entries) = 5 >= max_chain_length=5); must create a new IncrementalChain

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

    def __init__(self, base_dir="./chain", max_chain_length=5):
        self.base_dir = base_dir
        self.entries = []
        self.restore_dir = None
        self.restored_depth = 0
        self.max_chain_length = max_chain_length

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

        # Download each entry and create parent symlinks
        prev_local_dir = None
        for entry in chain:
            local_dir = os.path.join(self.base_dir, entry.path)
            os.makedirs(local_dir, exist_ok=True)

            objects = client.list_objects("checkpoints", prefix=entry.path, recursive=True)
            for obj in objects:
                filename = obj.object_name.split("/", maxsplit=1)[1]
                client.fget_object("checkpoints", obj.object_name, os.path.join(local_dir, filename))

            if prev_local_dir is not None:
                rel = os.path.relpath(prev_local_dir, local_dir)
                os.symlink(rel, os.path.join(local_dir, "parent"))

            self.entries.append(local_dir)
            prev_local_dir = local_dir

        self.restore_dir = prev_local_dir
        self.restored_depth = len(chain) - 1  # root is depth 0
        return self.restore_dir

    def clear_soft_dirty(self, pid):
        """Reset all soft-dirty bits so the next dump tracks only new changes
        from the immediately previous dump"""

        clear_refs_path = Path(f"/proc/{pid}/clear_refs")
        try:
            with open(clear_refs_path, 'w') as f:
                f.write('4')
        except Exception as e:
            print(f"Error clearing soft-dirty bits for pid {pid}: {e}")

    def upload_entry(self, client: Minio, entry_dir: str, minio_path: str):
        """Upload a single chain's entry files to MinIO"""

        # CRIU creates a collection of multiple image files to save the state;
        # assume we store in entry_dir

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

    def is_full_dump(self) -> bool:
        """Whether the next dump will be full (no parent) or incremental"""

        return len(self.entries) >= self.max_chain_length or len(self.entries) == 0