import os
import sys
from pathlib import Path
import subprocess
import time

from orchestrator import (
    on_container_started,
    on_container_checkpoint,
    on_container_request,
)

from orchestration import Checkpoint
from incremental import IncrementalChain
from minio import Minio
from minio.error import S3Error
from datetime import datetime

LOG_FNAME = "requestLog.txt"
CHECKPOINTS_BUCKET = "checkpoints"

# What does main.py do?

# entry point for the agent process; runs as a sidecar alongside the actual function.
# Performs three things:

# 1. Startup: calls on_container_started() in orchestrator.py. If the orchestrator says
# from_checkpoint = True, then main.py will download the checkpoint from MinIO and restore it using CRIU.
# Otherwise, it will cold-start a fresh pypy3 process.

# 2. Request monitoring loop (after init() called): polls a requestLog.txt file every 10ms; when the function
# # writes a latency to that file, main.py picks it up and calls after_request(latency) 

# 3. after_request: calls on_container_request(latency) in orchestrator.py. If the response says
# should_checkpoint = True, then main.py will checkpoint the function using CRIU at the returned
# checkpoint_location path, then calls on_container_checkpoint(checkpoint_location) in orchestrator.py to register it. 
# If the response says should_evict = True, kill the process


client: Minio = None
chain: IncrementalChain = None
last_checkpoint_path: str = None


def setup_minio():
    print("Setting up MinIO...")

    global client
    client = Minio(
        "minio-svc.stores.svc.cluster.local:9000",
        access_key="minioadmin",
        secret_key="minioadmin",
        secure=False,
    )

    found = client.bucket_exists(CHECKPOINTS_BUCKET)
    if not found:
        client.make_bucket(CHECKPOINTS_BUCKET)
        print("Created checkpoints bucket")
    else:
        print("Checkpoints bucket already exists")


def get_pypy_pid(retries=1, retry_delay=0.1):
    for attempt in range(retries):
        try:
            output = subprocess.check_output("pgrep pypy3", shell=True).decode(
                sys.stdout.encoding
            )
            # pgrep can return multiple pids; use the first one for CRIU commands.
            return output.strip().splitlines()[0]
        except subprocess.CalledProcessError:
            if attempt == retries - 1:
                raise
            time.sleep(retry_delay)


def get_java_pid():
    return subprocess.check_output("pgrep java", shell=True).decode(sys.stdout.encoding)


def after_request(latency):
    global chain, last_checkpoint_path

    passed, state = on_container_request(
        float(latency)
    )  # execute_callback(CallbackRoutes.REQUEST, params={"latency": latency})
    if state["should_checkpoint"]:
        pid = get_pypy_pid()
        path = state["checkpoint_location"]

        if chain is not None:
            output_dir = f"./chain/{path}"
            prepare_cmd = f"rm -rf {output_dir} && mkdir -p {output_dir}"
            if os.system(prepare_cmd):
                print("Failed to prepare checkpoint output directory!")
                sys.exit(1)

            prev_dir = None if chain.is_full_dump() else chain.entries[-1]
            dump_cmd = chain.build_dump_cmd(pid, output_dir, prev_dir)
            if os.system(dump_cmd):
                print("Checkpointing failed!")
                sys.exit(1)
            else:
                print("Checkpointing succeeded! Uploading to MinIO...")
                chain.upload_entry(client, output_dir, path)
                if prev_dir is None:
                    # Full dump starts a fresh chain root.
                    chain.entries = [output_dir]
                else:
                    chain.entries.append(output_dir)

                parent_path = None if prev_dir is None else last_checkpoint_path
                on_container_checkpoint(path, parent_path=parent_path)
                last_checkpoint_path = path
        else:
            cmd = f"rm -rf checkpoint && mkdir -p checkpoint && criu dump -t {pid} -v3 --tcp-established --leave-running -D ./checkpoint"
            if os.system(cmd):
                print("Checkpointing failed!")
                sys.exit(1)
            else:
                print("Checkpointing succeeded! Uploading to MinIO...")
                for root, dirs, files in os.walk("./checkpoint"):
                    for file in files:
                        file_path = os.path.join(root, file)
                        print("Uploading", file, "at", file_path)
                        client.fput_object(
                            CHECKPOINTS_BUCKET, f"{path}/{file}", file_path=file_path
                        )

                on_container_checkpoint(path)

    evictions_env = os.getenv("ENV").split(",")[1]
    if evictions_env == "true":
        if state["should_evict"]:
            print("Received eviction notice")
            pid = get_pypy_pid()
            print(pid)
            os.system(f"kill -9 {pid}")
            os.system("killall pypy3")
            print("Killed Python Process")
            sys.exit(0)
    else:
        pass


def init():
    global chain, last_checkpoint_path

    print("Executing start callback")
    passed, state = on_container_started()
    print("Callback response:", state)

    try:
        Path(LOG_FNAME).unlink()
    except FileNotFoundError:
        pass
    Path(LOG_FNAME).touch()

    incremental = state.get("incremental", False)
    if incremental:
        chain = IncrementalChain(
            base_dir="./chain",
            max_chain_length=state.get("max_chain_depth", 5),
        )
    else:
        chain = None

    if state["from_checkpoint"]:
        if incremental:
            checkpoint_payload = state.get("checkpoint_object")
            if checkpoint_payload is None:
                print("Missing checkpoint object for incremental restore!")
                sys.exit(1)

            checkpoint = Checkpoint.deserialize(checkpoint_payload, client)
            pool = [
                Checkpoint.deserialize(chkpt_payload, client)
                for chkpt_payload in state.get("pool", [])
            ]
            chain.setup_for_restore(client, checkpoint, pool)
            cmd = chain.build_restore_cmd()
            last_checkpoint_path = state["checkpoint_location"]
        else:
            dir = "./restore"
            os.system("rm -rf ./restore")
            os.system("mkdir restore")
            objects_to_retrieve = client.list_objects(
                CHECKPOINTS_BUCKET, prefix=state["checkpoint_location"], recursive=True
            )
            for obj in objects_to_retrieve:
                name: str = obj.object_name
                filename = name.split("/", maxsplit=1)[1]
                client.fget_object(CHECKPOINTS_BUCKET, name, f"{dir}/{filename}")

            cmd = f"criu restore --restore-detached --tcp-close -d -v3 -D {dir}"
            last_checkpoint_path = state["checkpoint_location"]
    else:
        # cmd = "setsid java -Xmx128m -Xms128m com.openfaas.entrypoint.App < /dev/null &> app.log &"
        # cmd = "setsid java -XX:-UsePerfData -Xmx128m -Xms128m com.openfaas.entrypoint.App < /dev/null &> /dev/null &"
        cmd = "setsid pypy3 index.py < /dev/null &> /dev/null &"
        last_checkpoint_path = None

    print("Executing command:", cmd)
    if os.system(cmd):
        print("Init command failed")
        sys.exit(1)

    if incremental and state["from_checkpoint"]:
        pid = get_pypy_pid(retries=10, retry_delay=0.2)
        chain.clear_soft_dirty(pid)


if __name__ == "__main__":
    setup_minio()
    init()

    last_count = 0
    while True:
        with open(LOG_FNAME, "r") as fp:
            contents = fp.readlines()
            count = len(contents)
            if count > last_count:
                for latency in contents[last_count:]:
                    latency = latency.strip()
                    if not latency:
                        continue
                    print("Detected latency:", latency)
                    after_request(latency)
                last_count = count

        time.sleep(10 / 1000)  # 10 ms
