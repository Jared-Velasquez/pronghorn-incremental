from . import ColdStartStrategy, FixedStrategy, RequestCentricStrategy
from . import Checkpoint, Parameters
from .strategies.request_centric import DEFAULT_MAX_CAPACITY
import json
import os
from minio import Minio
import numpy as np


def _parse_strategy_env(strategy_env: str):
    """Parse a strategy string into (strategy_name, params_dict).

    Example: 'request_centric&max_capacity=12&incremental=true' -> ('request_centric', {'max_capacity': '12', 'incremental': 'true'})
    
    """
    if "&" in strategy_env:
        parts = strategy_env.split("&")
        strategy_name = parts[0]
        params = {}
        for part in parts[1:]:
            key, value = part.split("=")
            params[key] = value
    else:
        strategy_name = strategy_env
        params = {}
    return strategy_name, params


def cr_deserialize(payload: str, client: Minio):
    if not payload:
        strategy_env = os.getenv("ENV").split(",")[0]
        print(f"Using strategy: {strategy_env}")
        strategy_name, params = _parse_strategy_env(strategy_env)
        if strategy_name == "cold":
            return ColdStartStrategy(Parameters(), [])
        elif strategy_name == "fixed":
            request_to_checkpoint = int(params.get("request_to_checkpoint", "1"))
            return FixedStrategy(Parameters(), [], request_to_checkpoint)
        else: # incremental strategy added here
            max_capacity = int(params.get("max_capacity", str(DEFAULT_MAX_CAPACITY)))
            incremental = params.get("incremental", "false") == "true"
            max_chain_depth = int(params.get("max_chain_depth", "5"))
            return RequestCentricStrategy(
                Parameters(), [],
                max_capacity=max_capacity,
                incremental=incremental,
                max_chain_depth=max_chain_depth,
            )
    obj = json.loads(payload)
    workload = Parameters.deserialize(obj["workload"])
    pool = [Checkpoint.deserialize(chkpt, client) for chkpt in obj["pool"]]
    print("Deserialized pool: ", pool)
    strategy = obj["strategy"]
    if strategy == "ColdStart":
        return ColdStartStrategy(workload, pool)
    elif strategy == "Fixed":
        return FixedStrategy(workload, pool, obj["request_to_checkpoint"])
    elif strategy == "RequestCentric":
        strategy = RequestCentricStrategy(
            workload,
            pool,
            obj["max_capacity"],
            obj["p"],
            obj["gamma"],
            eps=obj["eps"],
            incremental=obj.get("incremental", False),
            max_chain_depth=obj.get("max_chain_depth", 5),
        )
        strategy.weights = np.array(obj["weights"])
        return strategy
