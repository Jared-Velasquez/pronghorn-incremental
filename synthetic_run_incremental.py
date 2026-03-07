### Import Packages

import sys
import json
import os

# Ensure kubectl uses the project kubeconfig
_kubeconfig = os.path.expanduser("~/pronghorn-artifact/kubeconfig")
if os.path.exists(_kubeconfig):
    os.environ.setdefault("KUBECONFIG", _kubeconfig)
import datetime
import time
import logging
import subprocess
import requests
import re

from tqdm import tqdm
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

### Configure Request Adapter

retry_strategy = Retry(total=0)
adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=1, pool_maxsize=1)
http = requests.Session()
http.mount("http://", adapter)

### Generate Benchmark Run UID

uid = datetime.datetime.now().strftime("%m-%d-%H:%M:%S")

### Declare Constants

NUM_REQUESTS = int(sys.argv[1])
REQUEST_DELAY = int(sys.argv[2])  # in ms
# argv[3] is runtime (accepted for CLI compatibility but always PyPy)
# argv[4:] are benchmark names; test name is derived from the first benchmark
BENCHMARKS = sys.argv[4:]
test = BENCHMARKS[0] if BENCHMARKS else "run"

filename = "data/incremental-" + test + ".csv"

STRATEGIES = [
    # "cold",
    # "request_centric&max_capacity=20",
    # "request_centric&max_capacity=12&incremental=true&max_chain_depth=3",
    "request_centric&max_capacity=12&incremental=true&max_chain_depth=5",
]
# RATES = [20, 4, 1]
RATES = [20]

### Configure Logging Handlers

log_directory = "logs"
if not os.path.exists(log_directory):
    os.makedirs(log_directory)

data_directory = "data"
if not os.path.exists(data_directory):
    os.makedirs(data_directory)

logger = logging.getLogger()
logging.basicConfig(
    filename="logs/incremental-" + test + ".log",
    format="%(asctime)s %(filename)s: %(message)s",
    filemode="a+",
)
logger.setLevel(logging.DEBUG)


def check_namespace_pods():
    namespace = "openfaas-fn"
    cmd = f"kubectl get pods -n {namespace} --no-headers | wc -l"
    result = subprocess.run(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return int(result.stdout.strip())


def wait_for_deployment_deleted(benchmark, timeout=90):
    """Wait until the benchmark Deployment is fully gone from openfaas-fn."""
    # Brief pause to give faas-netes time to initiate deletion before we watch
    time.sleep(2)
    cmd = f"kubectl wait --for=delete deployment/{benchmark} -n openfaas-fn --timeout={timeout}s"
    result = subprocess.run(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if result.returncode != 0:
        logger.warning(
            "kubectl wait for delete %s returned non-zero: %s", benchmark, result.stderr.strip()
        )
    return result.returncode == 0


def wait_for_pod_ready(benchmark, timeout=60):
    """Poll until the benchmark pod is Running, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        cmd = f"kubectl get pods -n openfaas-fn -l faas_function={benchmark} --no-headers"
        result = subprocess.run(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if "Running" in result.stdout:
            return True
        time.sleep(2)
    logger.warning("Timed out waiting for %s pod to be ready", benchmark)
    return False


def measure_storage_bytes():
    """Return total bytes used by the checkpoints MinIO bucket, or 0 on failure."""
    result = subprocess.run(
        "mc du --json myminio/checkpoints",
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        data = json.loads(result.stdout.strip())
        return int(data.get("size", 0))
    except (json.JSONDecodeError, ValueError):
        logger.warning("Could not parse mc du output: %s", result.stdout)
        return 0


user = "jaredvel25"

with open(filename, "a") as output_file:
    for benchmark in BENCHMARKS:
        for strategy in STRATEGIES:
            for rate in RATES:

                logger.info("Deploying %s function", benchmark)
                deploy_cmd = f"faas-cli deploy --image={user}/{benchmark} --name={benchmark} --env=ENV={strategy},true,{rate}"
                deploy_proc = subprocess.run(deploy_cmd.split(" "), capture_output=True)
                logger.debug(
                    "Deploy command response: %s",
                    deploy_proc.stdout.decode("UTF-8"),
                )
                if deploy_proc.returncode != 0:
                    logger.warning(
                        "Deploy failed (rc=%d): %s",
                        deploy_proc.returncode,
                        deploy_proc.stderr.decode("UTF-8"),
                    )

                wait_for_pod_ready(benchmark)

                logger.info(
                    "Executing strategy: %s for benchmark %s with rate %s",
                    strategy,
                    benchmark,
                    rate,
                )

                rows = []
                nums = re.compile(r"\d+ ms")
                url = f"http://127.0.0.1:8080/function/{benchmark}?mutability=1"
                for index, _ in tqdm(enumerate(range(NUM_REQUESTS))):
                    retries = 0
                    for retry in range(3):
                        try:
                            start_time = datetime.datetime.now()
                            response = http.get(url)
                            end_time = datetime.datetime.now()
                            if not response.ok:
                                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
                            search = nums.search(response.text)
                            if search is None:  # PyPy benchmark
                                body = json.loads(response.text)
                                server_side = body.get("server_time")
                                overhead = body.get("client_overhead", 0)
                            else:  # Java benchmark (fallback, not expected here)
                                server_side = int(search.group(0).split(" ")[0])
                                overhead = 0
                            client_side = (end_time - start_time) / datetime.timedelta(
                                microseconds=1
                            )
                            logger.debug("%s %s %s", server_side, overhead, client_side)
                            rows.append(
                                (index + 1, benchmark, strategy, rate, client_side, server_side, overhead)
                            )
                            time.sleep(REQUEST_DELAY / 1000)
                        except Exception as e:
                            retries += 1
                            logger.warning(
                                "Request %s failed (attempt %s/3): %s", index + 1, retry + 1, e
                            )
                            time.sleep(min(retries**2, 10))
                        else:
                            break

                # Measure storage before cleanup so the metric reflects the full run
                storage_bytes = measure_storage_bytes()
                logger.info(
                    "Storage after run — strategy: %s, benchmark: %s, rate: %s, bytes: %s",
                    strategy,
                    benchmark,
                    rate,
                    storage_bytes,
                )

                for index, benchmark_name, strat, r, client_side, server_side, overhead in rows:
                    output_file.write(
                        f"{index},{benchmark_name},1,{strat},{r},{client_side},{server_side},{overhead},{storage_bytes}\n"
                    )
                output_file.flush()

                logger.info(
                    "Completed strategy: %s for benchmark %s with rate %s",
                    strategy,
                    benchmark,
                    rate,
                )

                clean_cmd = f"faas-cli remove {benchmark}"
                clean_proc = subprocess.run(clean_cmd.split(" "), capture_output=True)
                logger.debug(
                    "Clean command response: %s", clean_proc.stdout.decode("UTF-8")
                )
                wait_for_deployment_deleted(benchmark)

                delete_cmd = f"kubectl delete -f {os.path.expanduser('~/pronghorn-artifact/database/pod.yaml')}"
                delete_proc = subprocess.run(delete_cmd.split(" "), capture_output=True)
                logger.debug(
                    "Delete command response: %s", delete_proc.stdout.decode("UTF-8")
                )

                redeploy_cmd = f"kubectl apply -f {os.path.expanduser('~/pronghorn-artifact/database/pod.yaml')}"
                redeploy_proc = subprocess.run(
                    redeploy_cmd.split(" "), capture_output=True
                )
                logger.debug(
                    "Redeploy command response: %s",
                    redeploy_proc.stdout.decode("UTF-8"),
                )

                minio_cleanup_cmd = "mc rb myminio/checkpoints --force"
                minio_cleanup_proc = subprocess.run(
                    minio_cleanup_cmd.split(" "), capture_output=True
                )
                logger.debug(
                    "MinIO cleanup command response: %s",
                    minio_cleanup_proc.stdout.decode("UTF-8"),
                )

                while check_namespace_pods() > 0:
                    print("Waiting for pods to terminate...")
                    time.sleep(10)
