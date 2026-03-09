from typing import List, Union

from .. import Parameters, CRStrategy, Checkpoint, WorkloadState, FixedStrategy
import random
import numpy as np
import copy

DEFAULT_MAX_CAPACITY = 14

# Opt 2: Each additional level of chain depth discounts that node's contribution
# to the chain's aggregate score by this factor.  A chain of depth 5 has its
# deepest leaf weighted at 0.9^4 ≈ 0.66×, making the pruner prefer shallower
# chains and naturally limiting unbounded depth growth.
CHAIN_DEPTH_DISCOUNT = 0.9

DEFAULT_P = 0.40
DEFAULT_GAMMA = 0.10

DEFAULT_PERFORMANCE_FN = lambda arr: np.mean(np.array(arr))
# DEFAULT_PERFORMANCE_FN = lambda arr: np.median(np.array(arr))

DEFAULT_EPSILON = 0.5
MIN_WEIGHT_EPSILON = 1  # microseconds (median latency)


class RequestCentricStrategy(CRStrategy):
    def __init__(
        self,
        workload: Parameters,
        pool: List[Checkpoint],
        max_capacity: int = DEFAULT_MAX_CAPACITY,
        p: float = DEFAULT_P,
        gamma: float = DEFAULT_GAMMA,
        performance_fn=DEFAULT_PERFORMANCE_FN,
        eps=DEFAULT_EPSILON,
        incremental: bool = False,
        max_chain_depth: int = 5,
    ) -> None:
        super().__init__(workload, pool)
        self.max_capacity = max_capacity
        self.p = p
        self.gamma = gamma
        self.performance_fn = performance_fn
        self.weights = np.array([0] * workload.max_requests)
        self.incremental = incremental
        self.eps = eps
        self.incremental = incremental
        self.max_chain_depth = max_chain_depth

    @property
    def name(self) -> str:
        return f"RequestCentric{self.max_capacity}_P{self.p}_Gamma{self.gamma}"

    @property
    def strategy(self) -> str:
        return "RequestCentric"

    def _weights_for(self, req_num, scalar=False):
        cur_slice = self.weights[
            req_num : min(req_num + self.workload.eviction, self.workload.max_requests)
        ]
        output = 1000000.0 / (cur_slice + MIN_WEIGHT_EPSILON)
        if scalar:
            output = self.performance_fn(output)
        return output

    def _weights_for_chain(self, root_chkpt: Checkpoint):
        # Opt 2: apply a per-depth discount so deeper chains score lower,
        # biasing the pruner toward shallower chains and fresh full dumps.
        # Root is at depth 0 (no discount); each child level multiplies by
        # CHAIN_DEPTH_DISCOUNT.
        total_weight = self._weights_for(root_chkpt.state.request_number, scalar=True)
        stack = [(root_chkpt, 1)]  # (checkpoint, child_depth)

        while stack:
            current, depth = stack.pop()
            # Brute force find children of checkpoints in pool
            for child in [c for c in self.pool if c.parent_path == current.path]:
                child_weight = self._weights_for(child.state.request_number, scalar=True)
                total_weight += child_weight * (CHAIN_DEPTH_DISCOUNT ** depth)
                stack.append((child, depth + 1))
        return total_weight

    def _prune_pool(self):
        # Default Non Incremental Case 
        if not self.incremental:
            output = []
            by_performance = sorted(
                self.pool,
                key=lambda c: self._weights_for(c.state.request_number, scalar=True),
                reverse=True,
            )
    
            keeping_p = round(self.p * len(by_performance))
            output += by_performance[:keeping_p]
            by_performance = by_performance[keeping_p:]
    
            keeping_gamma = round(self.gamma * len(by_performance))
            output += random.choices(
                by_performance, k=min(keeping_gamma, len(by_performance))
            )
    
            output_chkpts = {chkpt for chkpt in output}
            removed = [chkpt for chkpt in self.pool if chkpt not in output_chkpts]
            for chkpt in removed:
                chkpt.delete()
    
            self.pool[:] = output
            print(
                f"Evicted all but top {keeping_p} by performance and {keeping_gamma} by random"
            )
            assert len(self.pool) <= self.max_capacity
            return 
        
        # Incremental Case 

        # Chain Dependency Map 
        children_map = {}
        for chkpt in self.pool:
            if chkpt.parent_path is not None:
                children_map.setdefault(chkpt.parent_path, []).append(chkpt)

        # Roots 
        roots = [chkpt for chkpt in self.pool if chkpt.parent_path is None] 

        # Sort chains based on weight (reversed)
        chains_sorted = sorted(roots, key=self._weights_for_chain, reverse=True)

        # New pool 
        output = []
    
        for root in chains_sorted:
            # Collect the entire chain in a specific order (parent->child->etc) 
            this_chain = []
            queue = [root] 
            idx = 0
            while idx < len(queue):
                curr = queue[idx]
                this_chain.append(curr)
                if curr.path in children_map:
                    queue.extend(children_map[curr.path])
                idx += 1

            # 3. Add as much of the chain as the "budget" allows
            # This protects the chain integrity (parents kept) while respecting capacity
            remaining_space = self.max_capacity - len(output)
            
            if remaining_space <= 0:
                break
                
            # If the whole chain fits, take it all. 
            # If not, take only the first N items (the root and closest descendants).
            nodes_to_add = this_chain[:remaining_space]
            output.extend(nodes_to_add)
    
        # Cleanup and Assertion --> Into new pool 
        output_set = set(output)
        for chkpt in self.pool:
            if chkpt not in output_set:
                chkpt.delete()
    
        self.pool[:] = output
        print(f"Pruning done. Pool size: {len(self.pool)} / {self.max_capacity}")
        assert len(self.pool) <= self.max_capacity

    def checkpoint_to_use(self) -> Checkpoint:
        if len(self.pool) > self.max_capacity:
            self._prune_pool()

        print(f"Exploiting")

        # Incremental Case: select from leaves only (checkpoints with no children),
        # weighted by individual performance. Restoring from a leaf gives the most
        # up-to-date state; CRIU follows parent symlinks back to the root automatically.
        if self.incremental:
            parent_paths = {chkpt.parent_path for chkpt in self.pool if chkpt.parent_path is not None}
            leaves = [chkpt for chkpt in self.pool if chkpt.path not in parent_paths]
            if not leaves:
                leaves = list(self.pool)
            # Filter out checkpoints with no viable checkpoint window: if
            # request_number + 1 >= max_requests, when_to_checkpoint returns the
            # sentinel 50000 (empty weights slice) and the container immediately
            # evicts after one request with no new checkpoint, causing a stuck loop.
            viable_leaves = [c for c in leaves if c.state.request_number + 1 < self.workload.max_requests]
            if viable_leaves:
                leaves = viable_leaves
            expanded_pool = leaves + [None]
            weights = [self._weights_for(chkpt.state.request_number, scalar=True) if chkpt is not None else 0 for chkpt in expanded_pool]
        # Default Non Incremental Case
        else:
            viable = [c for c in self.pool if c.state.request_number + 1 < self.workload.max_requests]
            candidates = viable if viable else self.pool
            expanded_pool = candidates + [None]
            weights = [self._weights_for(chkpt.state.request_number if chkpt is not None else 0, scalar=True) for chkpt in expanded_pool]

        weights = np.array(weights)
        weights_max = np.amax(weights, keepdims=True)
        weights_shifted = np.exp(weights - weights_max)
        weights = weights_shifted / np.sum(weights_shifted, keepdims=True)
        weights = weights.tolist()
        print(list(zip(weights, expanded_pool)))
        ret_val = random.choices(expanded_pool, weights=weights, k=1)[0]
        print("Choosing", ret_val)
        return ret_val

    def when_to_checkpoint(self, state: WorkloadState) -> int:    # Unchanged, strategies/fixed.py works with this even for incremental case  
        # weights = []
        # if self.temperature >= random.uniform(0, 1):  # explore
        #     print("Checkpoint exploring")
        #     weights = cur_slice
        # else:  # exploit
        #     print("Checkpoint exploiting")
        weights = self._weights_for(state.request_number + 1)
        print(
            self.weights,
            weights,
            "Num Weights: ",
            len(weights),
            "Req Num: ",
            state.request_number,
            "Workload Dict: ",
            self.workload.__dict__,
        )
        interval = list(
            range(state.request_number + 1, state.request_number + len(weights) + 1)
        )
        if not interval:
            return 50000 # do not use a checkpoint
        weights = [self._weights_for(i, scalar=True) for i in interval]
        desired_request = random.choices(
            interval,
            # weights=weights.tolist(),
            weights=weights,
            k=1,
        )[0]

        print(f"Checkpointing at {desired_request}", list(zip(interval, weights)))
        fixed_strat = FixedStrategy(self.workload, self.pool, desired_request)
        return fixed_strat.when_to_checkpoint(state)

    def on_request(self, state: WorkloadState):
        request_num = state.request_number - 1
        if request_num >= len(self.weights):
            return
        cur_weight = self.weights[request_num]
        if cur_weight == 0:
            self.weights[request_num] = state.latencies[-1]
        else:
            self.weights[request_num] = (
                self.eps * state.latencies[-1] + (1 - self.eps) * cur_weight
            )

        self.weights[-1] = self.weights[-2]

    def reset(self) -> None:
        # Note: Checkpoint Deletion should always be the entire checkpoint to avoid orphans 
        for chkpt in self.pool:
            chkpt.delete() 
        self.pool[:] = []
        self.weights = np.array([0] * self.workload.max_requests)

    @property
    def extra_state(self) -> dict:
        return {
            "max_capacity": self.max_capacity,
            "p": self.p,
            "gamma": self.gamma,
            "weights": self.weights.tolist(),
            "eps": self.eps,
            "incremental": self.incremental,
            "max_chain_depth": self.max_chain_depth,
        }
