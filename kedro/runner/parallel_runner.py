# Copyright 2018-2019 QuantumBlack Visual Analytics Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND
# NONINFRINGEMENT. IN NO EVENT WILL THE LICENSOR OR OTHER CONTRIBUTORS
# BE LIABLE FOR ANY CLAIM, DAMAGES, OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF, OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
# The QuantumBlack Visual Analytics Limited ("QuantumBlack") name and logo
# (either separately or in combination, "QuantumBlack Trademarks") are
# trademarks of QuantumBlack. The License does not grant you any right or
# license to the QuantumBlack Trademarks. You may not use the QuantumBlack
# Trademarks or any confusingly similar mark as a trademark for your product,
#     or use the QuantumBlack Trademarks in any other manner that might cause
# confusion in the marketplace, including but not limited to in advertising,
# on websites, or on software.
#
# See the License for the specific language governing permissions and
# limitations under the License.
"""``ParallelRunner`` is an ``AbstractRunner`` implementation. It can
be used to run the ``Pipeline`` in parallel groups formed by toposort.
"""
import os
import pickle
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from itertools import chain
from multiprocessing.managers import BaseProxy, SyncManager  # type: ignore
from multiprocessing.reduction import ForkingPickler
from pickle import PicklingError
from typing import Any, Iterable, Set

from kedro.io import DataCatalog, DataSetError, MemoryDataSet
from kedro.pipeline import Pipeline
from kedro.pipeline.node import Node
from kedro.runner.runner import AbstractRunner, run_node


class _SharedMemoryDataSet:
    """``_SharedMemoryDataSet`` a wrapper class for a shared MemoryDataSet in SyncManager.
    It is not inherited from AbstractDataSet class.
    """

    def __init__(self, manager: SyncManager):
        """Creates a new instance of ``_SharedMemoryDataSet``,
        and creates shared memorydataset attribute.

        Args:
            manager: An instance of multiprocessing manager for shared objects.

        """
        self.shared_memory_dataset = manager.MemoryDataSet()  # type: ignore

    def __getattr__(self, name):
        # This if condition prevents recursive call when deserializing
        if name == "__setstate__":
            raise AttributeError()
        return getattr(self.shared_memory_dataset, name)

    def save(self, data: Any):
        """Calls save method of a shared MemoryDataSet in SyncManager.
        """
        try:
            self.shared_memory_dataset.save(data)
        except Exception as exc:  # pylint: disable=broad-except
            # Checks if the error is due to serialisation or not
            try:
                pickle.dumps(data)
            except Exception:
                raise DataSetError(
                    "{} cannot be serialized. ParallelRunner implicit memory datasets "
                    "can only be used with serializable data".format(
                        str(data.__class__)
                    )
                )
            else:
                raise exc


class ParallelRunnerManager(SyncManager):
    """``ParallelRunnerManager`` is used to create shared ``MemoryDataSet``
    objects as default data sets in a pipeline.
    """


ParallelRunnerManager.register(  # pylint: disable=no-member
    "MemoryDataSet", MemoryDataSet
)


class ParallelRunner(AbstractRunner):
    """``ParallelRunner`` is an ``AbstractRunner`` implementation. It can
    be used to run the ``Pipeline`` in parallel groups formed by toposort.
    """

    def __init__(self, max_workers: int = None):
        """
        Instantiates the runner by creating a Manager.

        Args:
            max_workers: Number of worker processes to spawn. If not set,
                calculated automatically based on the pipeline configuration
                and CPU core count.

        Raises:
            ValueError: bad parameters passed
        """
        self._manager = ParallelRunnerManager()
        self._manager.start()

        if max_workers is not None and max_workers <= 0:
            raise ValueError("max_workers should be positive")

        # NOTE: `os.cpu_count` might return None in some weird cases.
        # https://github.com/python/cpython/blob/3.7/Modules/posixmodule.c#L11431
        self._max_workers = max_workers or os.cpu_count() or 1

    def __del__(self):
        self._manager.shutdown()

    def create_default_data_set(  # type: ignore
        self, ds_name: str
    ) -> _SharedMemoryDataSet:
        """Factory method for creating the default data set for the runner.

        Args:
            ds_name: Name of the missing data set

        Returns:
            An instance of an implementation of _SharedMemoryDataSet to be used
            for all unregistered data sets.

        """
        return _SharedMemoryDataSet(self._manager)

    @classmethod
    def _validate_nodes(cls, nodes: Iterable[Node]):
        """Ensure all tasks are serializable."""
        unserializable = []
        for node in nodes:
            try:
                ForkingPickler.dumps(node)
            except (AttributeError, PicklingError):
                unserializable.append(node)

        if unserializable:
            raise AttributeError(
                "The following nodes cannot be serialized: {}\nIn order to "
                "utilize multiprocessing you need to make sure all nodes are "
                "serializable, i.e. nodes should not include lambda "
                "functions, nested functions, closures, etc.\nIf you "
                "are using custom decorators ensure they are correctly using "
                "functools.wraps().".format(sorted(unserializable))
            )

    @classmethod
    def _validate_catalog(cls, catalog: DataCatalog, pipeline: Pipeline):
        """Ensure that all data sets are serializable and that we do not have
        any non proxied memory data sets being used as outputs as their content
        will not be synchronized across threads.
        """

        data_sets = catalog._data_sets  # pylint: disable=protected-access

        unserializable = []
        for name, data_set in data_sets.items():
            try:
                ForkingPickler.dumps(data_set)
            except (AttributeError, PicklingError):
                unserializable.append(name)

        if unserializable:
            raise AttributeError(
                "The following data_sets cannot be serialized: {}\nIn order "
                "to utilize multiprocessing you need to make sure all data "
                "sets are serializable, i.e. data sets should not make use of "
                "lambda functions, nested functions, closures etc.\nIf you "
                "are using custom decorators ensure they are correctly using "
                "functools.wraps().".format(sorted(unserializable))
            )

        memory_data_sets = []
        for name, data_set in data_sets.items():
            if (
                name in pipeline.all_outputs()
                and isinstance(data_set, MemoryDataSet)
                and not isinstance(data_set, BaseProxy)
            ):
                memory_data_sets.append(name)

        if memory_data_sets:
            raise AttributeError(
                "The following data sets are memory data sets: {}\n"
                "ParallelRunner does not support output to externally created "
                "MemoryDataSets".format(sorted(memory_data_sets))
            )

    def _get_required_workers_count(self, pipeline: Pipeline):
        """
        Calculate the max number of processes required for the pipeline,
        limit to the number of CPU cores.
        """
        # Number of nodes is a safe upper-bound estimate.
        # It's also safe to reduce it by the number of layers minus one,
        # because each layer means some nodes depend on other nodes
        # and they can not run in parallel.
        # It might be not a perfect solution, but good enough and simple.
        required_processes = len(pipeline.nodes) - len(pipeline.grouped_nodes) + 1

        return min(required_processes, self._max_workers)

    def _run(  # pylint: disable=too-many-locals,useless-suppression
        self, pipeline: Pipeline, catalog: DataCatalog
    ) -> None:
        """The abstract interface for running pipelines.

        Args:
            pipeline: The ``Pipeline`` to run.
            catalog: The ``DataCatalog`` from which to fetch data.

        Raises:
            AttributeError: when the provided pipeline is not suitable for
                parallel execution.
            Exception: in case of any downstream node failure.

        """
        nodes = pipeline.nodes
        self._validate_catalog(catalog, pipeline)
        self._validate_nodes(nodes)

        load_counts = Counter(chain.from_iterable(n.inputs for n in nodes))
        node_dependencies = pipeline.node_dependencies
        todo_nodes = set(node_dependencies.keys())
        done_nodes = set()  # type: Set[Node]
        futures = set()
        done = None
        max_workers = self._get_required_workers_count(pipeline)

        with ProcessPoolExecutor(max_workers=max_workers) as pool:
            while True:
                ready = {n for n in todo_nodes if node_dependencies[n] <= done_nodes}
                todo_nodes -= ready
                for node in ready:
                    futures.add(pool.submit(run_node, node, catalog))
                if not futures:
                    assert not todo_nodes, (todo_nodes, done_nodes, ready, done)
                    break
                done, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        node = future.result()
                    except Exception:
                        self._suggest_resume_scenario(pipeline, done_nodes)
                        raise
                    done_nodes.add(node)

                    # decrement load counts and release any data sets we've finished with
                    # this is particularly important for the shared datasets we create above
                    for data_set in node.inputs:
                        load_counts[data_set] -= 1
                        if (
                            load_counts[data_set] < 1
                            and data_set not in pipeline.inputs()
                        ):
                            catalog.release(data_set)
                    for data_set in node.outputs:
                        if (
                            load_counts[data_set] < 1
                            and data_set not in pipeline.outputs()
                        ):
                            catalog.release(data_set)
