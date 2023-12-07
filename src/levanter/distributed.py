import atexit
import itertools
import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Union

import jax
import ray
from jax._src import clusters, distributed

from levanter.utils.py_utils import logical_cpu_core_count


logger = logging.getLogger(__name__)


_JOBID_PARAM = "SLURM_JOB_ID"
_NODE_LIST_CHOICES = ["SLURM_STEP_NODELIST", "SLURM_JOB_NODELIST", "SLURM_NODELIST"]
_PROCESS_COUNT = "SLURM_NTASKS"
_PROCESS_ID = "SLURM_PROCID"
_LOCAL_PROCESS_ID = "SLURM_LOCALID"
_NUM_NODES = "SLURM_STEP_NUM_NODES"
_TASKS_PER_NODE = "SLURM_STEP_TASKS_PER_NODE"
_VISIBLE_DEVICES = "CUDA_VISIBLE_DEVICES"
_NODE_NAME = "SLURMD_NODENAME"


class LevanterSlurmCluster(clusters.SlurmCluster):
    """
    This class is a copy-paste and modification of the original SlurmCluster class in jax, with a few differences:
    - It uses the SLURM_LOCAL_PROCESS_COUNT to determine how many devices to use
    - It looks in a few places for the node list, since the environment variable is set differently
    depending on how you run
    # TODO: upstream this
    """

    # this is mostly copy paste, but it looks at a different env variable that is set when sbatch is set
    @classmethod
    def get_coordinator_address(cls) -> str:
        # Pick port in ephemeral range [(65535 - 2^12 + 1), 65535]
        id = os.environ[_JOBID_PARAM]
        port = _choose_port(id)

        # Parse the first hostname of the job
        # If we are looking for 'node001',
        # node_list potential formats are 'node001', 'node001,host2',
        # 'node[001-0015],host2', and 'node[001,007-015],host2'.
        node_list = LevanterSlurmCluster._node_list()
        if node_list is None:
            raise ValueError(
                "Could not find node list in environment variables. You must set coordinator_address manually."
            )
        delims = {",", "["}
        ind = next((i for i, ch in enumerate(node_list) if ch in delims), len(node_list))
        if ind == len(node_list) or node_list[ind] == ",":  # Formats: 'node001' or 'node001,host2'
            return f"{node_list[:ind]}:{port}"
        else:  # Formats: 'node[001-0015],host2' or 'node[001,007-015],host2'
            prefix = node_list[:ind]
            suffix = node_list[ind + 1 :]
            delims2 = {",", "-"}
            ind2 = next((i for i, ch in enumerate(suffix) if ch in delims2), None)
            return f"{prefix}{suffix[:ind2]}:{port}"

    @classmethod
    def _node_list(cls):
        return next((os.environ[o] for o in _NODE_LIST_CHOICES if o in os.environ), None)

    @classmethod
    def get_local_device_ids_for_process(cls) -> Optional[List[int]]:
        if _VISIBLE_DEVICES in os.environ:
            all_visible_devices = [int(x) for x in os.environ[_VISIBLE_DEVICES].split(",")]
        else:
            all_visible_devices = list(range(jax.local_device_count()))

        # We want to figure out how many tasks are running on this node
        # the only env variable that is reliably set here is SLURM_STEP_TASKS_PER_NODE
        # which is a comma separated list of the number of tasks per node, except they "helpfully"
        # collapse the list if there are multiple nodes with the same number of tasks e.g.
        # 1(x2),3,4(x3) -> 1,1,3,4,4,4
        # Why they do this is beyond me. It seems like more trouble to be less helpful.
        # So we have to do some parsing to figure out how many tasks are on each node
        # and then figure out which node we are on
        # first replace the repeated values with the number of times they are repeated
        unrolled_tasks_per_node = []

        multi_match = re.compile(r"(\d+)\(x(\d+)\)")
        for x in os.environ[_TASKS_PER_NODE].split(","):
            match = multi_match.match(x)
            if match:
                unrolled_tasks_per_node.extend([int(match.group(1))] * int(match.group(2)))
            else:
                unrolled_tasks_per_node.append(int(x))

        # now we can figure out which node we are on. This is also annoying because the node list
        # is a comma separated list of nodes, but they collapse the list if there are multiple nodes
        # with the same name e.g. node001,node002,node003,node004,node007 -> node[001-004,007]
        #  slurm exposes a command to expand this list for us, but it's not always available
        node_list = LevanterSlurmCluster._node_list()
        if node_list is None:
            raise ValueError(
                "Could not find node list in environment variables. You must set coordinator_address manually."
            )

        node_list = _square_brace_expand(node_list)

        # finally, we can figure out which node we are on
        local_node = os.environ[_NODE_NAME]
        local_node_index = node_list.index(local_node)
        tasks_on_local_node = unrolled_tasks_per_node[local_node_index]

        local_process_id = cls.get_local_process_id()

        if local_process_id is None:
            return None

        if len(all_visible_devices) % tasks_on_local_node != 0:
            raise ValueError(
                f"Number of visible devices ({len(all_visible_devices)}) is not divisible by the number "
                f"of local tasks ({tasks_on_local_node})"
            )

        num_devices_per_local_process = len(all_visible_devices) // tasks_on_local_node

        # select contiguous devices for this process
        begin = local_process_id * num_devices_per_local_process
        return all_visible_devices[begin : begin + num_devices_per_local_process]


def _square_brace_expand(node_list):
    # Find all parts of the sequence including text and number ranges
    parts = re.findall(r"(\[.*?\]|[^\[\]]+)", node_list)

    # This function will generate numbers from a range or a single number string
    def generate_numbers(number_string):
        if "-" in number_string:  # it's a range
            start, end = map(int, number_string.split("-"))
            return [str(i).zfill(len(number_string.split("-")[0])) for i in range(start, end + 1)]
        else:  # it's a single number
            return [number_string]

    # This function will process each part and return a list of strings or a list of lists of strings
    # Process each part to create lists of possible variations
    processed_parts = []
    for part in parts:
        if part.startswith("[") and part.endswith("]"):
            # Extract the number sequences and expand each one
            number_sequences = part.strip("[]").split(",")
            processed_parts.append(
                list(itertools.chain.from_iterable(generate_numbers(seq) for seq in number_sequences))
            )
        else:
            processed_parts.append([part])

    # Compute the Cartesian product of all parts to generate all combinations
    expanded_nodes = ["".join(combination) for combination in itertools.product(*processed_parts)]

    # Join the nodes with commas
    return expanded_nodes


def _choose_port(id):
    port = int(id) % 2**12 + (65535 - 2**12 + 1)
    return port


_already_initialized = False


def auto_ray_cluster(
    address: Optional[str] = None, namespace: Optional[str] = "levanter", start_workers: bool = True, **kwargs
):
    """Initializes ray, automatically discovering the address if it is not provided.
    Currently supports slurm and TPU.

    NB that Ray has Slurm support: https://docs.ray.io/en/latest/cluster/vms/user-guides/community/slurm.html

    We don't use that because it's more geared towards submitting jobs to a ray cluster that is backed by slurm.
    Instead, we have our machines already.
    """
    global _already_initialized

    if _already_initialized:
        logger.warning("auto_ray_cluster has already been called. Ignoring subsequent calls.")
        return

    def _munge_address_port(address: str):
        # the coordinator address typically includes a port that jax wants to use. we want to use our own port
        # we add a deterministic number to the chosen port and then cycle through the ephemeral range
        # this is a hack, but it works
        host, port_str = address.split(":")
        port = int(port_str)
        return host, port

    if address is None:
        # Ray automatically looks at RAY_ADDRESS. We don't want to use our defaulting logic if that is set
        if os.getenv("RAY_ADDRESS") is not None:
            address = os.getenv("RAY_ADDRESS")
            logger.info("Auto-discovered ray address using RAY_ADDRESS: %s", address)
        else:
            coord_address = getattr(distributed.global_state, "coordinator_address", None)

            if coord_address is None:
                logger.info("No auto-discovered ray address found. Using ray.init('local').")
                address = "local"
            else:
                logger.info(f"Auto-discovered ray address using JAX coordinator address: {coord_address}")
                host, port = _munge_address_port(coord_address)

                ray_port = _choose_port(port + 240)
                address = f"{host}:{ray_port}"

                # Explicitly setting the number of CPUs on ray init stops init errors
                num_cpus = logical_cpu_core_count()

                if jax.process_index() == 0:
                    logger.info(f"Starting ray head on port {ray_port}. We are process 0.")
                    logger.info(f"Starting ray with num_cpus set to {num_cpus}.")
                    ret = os.system(f"ray start --head --port {ray_port} --num-cpus {num_cpus} --address {address}")
                    if ret != 0:
                        raise RuntimeError(f"Failed to start ray head with exit code {ret}")
                    else:
                        logger.info(f"Successfully started ray head on port {ray_port}.")

                    # install an atexit handler to kill the head when we exit
                    atexit.register(lambda: os.system("ray stop -g 10 --force"))
                elif start_workers:
                    logger.info(
                        f"Starting ray worker and connecting to {address}. We are process {jax.process_index()}."
                    )
                    logger.info(f"Starting ray with num_cpus set to {num_cpus}.")
                    ret = os.system(f"ray start --address {address} --num-cpus {num_cpus}")
                    if ret != 0:
                        raise RuntimeError(f"Failed to start ray head with exit code {ret}")
                    else:
                        logger.info(f"Successfully started ray worker and connected to {address}.")

    logger.info(f"ray.init(address={repr(address)}, namespace={repr(namespace)}, **{repr(kwargs)})")
    # Ray has retry logic, so we don't need to retry here :fingers-crossed:
    ray.init(address=address, namespace=namespace, **kwargs)
    atexit.register(lambda: ray.shutdown())
    _already_initialized = True


@dataclass(frozen=True)
class DistributedConfig:
    coordinator_address: Optional[str] = None  # if None, we'll use the default coordinator address (for TPU or GPU)
    num_processes: Optional[int] = None
    process_id: Optional[int] = None
    local_device_ids: Optional[Union[int, List[int]]] = None

    def _is_distributed(self):
        if (
            (self.coordinator_address is not None)
            or (self.num_processes is not None)
            or (self.process_id is not None)
            or (self.local_device_ids is not None)
        ):
            return True

        # jax will automatically detect slurm or tpu, so we check those too. This is a bit fragile
        # since it depends on the jax internals, but it's the best we can do
        if any(env.is_env_present() for env in clusters.ClusterEnv._cluster_types):
            return True

        return False

    def initialize(self):
        if self._is_distributed():
            device_ids = self.local_device_ids
            coordinator_address = self.coordinator_address

            if LevanterSlurmCluster.is_env_present():
                if device_ids is None:
                    device_ids = LevanterSlurmCluster.get_local_device_ids_for_process()

                if coordinator_address is None:
                    coordinator_address = LevanterSlurmCluster.get_coordinator_address()

            jax.distributed.initialize(coordinator_address, self.num_processes, self.process_id, device_ids)
            logger.info(
                f"Initialized jax.distributed with {jax.device_count()} devices, {jax.process_count()} processes,"
                f" coordinator_address={coordinator_address}, process_id={self.process_id}, my"
                f" device_ids={device_ids}."
            )
        else:
            logger.info(
                "Not initializing jax.distributed because no distributed config "
                "was provided, and no cluster was detected."
            )


@dataclass
class RayConfig:
    address: Optional[str] = None
    start_workers: bool = True
    auto_start_cluster: bool = True

    def initialize(self):
        if self.auto_start_cluster:
            auto_ray_cluster(address=self.address, start_workers=self.start_workers)
