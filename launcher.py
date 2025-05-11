# filepath: c:\Users\alexb\OneDrive\Bureau\Trucs\AwaBot\Flake\launcher.py
# extremely old code (2+ years)

import asyncio
import logging
import multiprocessing
import signal
import os
import sys
import time
import requests
import ipc

from bot import ClusterBot

TOKEN = ""

shardsPerCluster = 10  # how many shards you'd like per cluster e.g. cluster 1 = shard 0-14, cluster 2 = shard 15-29, etc.
shardCount = 1  # keep as "auto" if you want to automatically get the shard count from discord, otherwise set to a number.

log = logging.getLogger("Cluster#Launcher")
log.setLevel(logging.DEBUG)
hdlr = logging.StreamHandler()
hdlr.setFormatter(logging.Formatter("[Launcher] %(message)s"))
log.handlers = [hdlr]

CLUSTER_NAMES = (
    "Alpha",
    "Beta",
    "Charlie",
    "Delta",
    "Echo",
    "Foxtrot",
    "Golf",
    "Hotel",
    "India",
    "Juliett",
    "Kilo",
    "Mike",
    "November",
    "Oscar",
    "Papa",
    "Quebec",
    "Romeo",
    "Sierra",
    "Tango",
    "Uniform",
    "Victor",
    "Whisky",
    "X-ray",
    "Yankee",
    "Zulu",
)
NAMES = iter(CLUSTER_NAMES)


class Launcher:
    def __init__(self, loop, *, ipc_enabled=False):
        log.info("Cluster Launcher Starting")
        self.cluster_queue = []
        self.clusters = []

        self.fut = None
        self.loop = loop
        self.alive = True

        self.keep_alive = None
        self.init = time.perf_counter()

        self.start_ipc = ipc_enabled
        self.ipc_process = None

    def get_shard_count(self):
        if isinstance(shardCount, int) and shardCount != "auto":
            return shardCount
        else:
            headers = {
                "Authorization": "Bot " + TOKEN,
                "User-Agent": "DiscordBot (PyCord, https://pycord.dev) Python/3.x aiohttp/3.x",
            }
            try:
                data = requests.get(
                    "https://discord.com/api/v10/gateway/bot", headers=headers
                )
                data.raise_for_status()
                content = data.json()
                log.info(
                    f"Successfully got shard count of {content['shards']} ({data.status_code, data.reason})"
                )
                return content["shards"]
            except requests.exceptions.RequestException as e:
                log.error(f"Error getting shard count: {e}")
                raise ValueError(
                    "Failed to automatically determine shard count."
                ) from e

    def start(self):
        self.fut = asyncio.ensure_future(self.startup(), loop=self.loop)

        try:
            self.loop.run_forever()
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt received, shutting down.")
            self.loop.run_until_complete(self.shutdown())
        finally:
            self.cleanup()

    def cleanup(self):
        if not self.loop.is_closed():
            self.loop.stop()
            for task in asyncio.all_tasks(self.loop):
                task.cancel()
            self.loop.close()
        log.info("Launcher cleanup complete.")

    def task_complete(self, task):
        if task.cancelled():
            return
        if task.exception():
            log.error("A critical task raised an exception:", exc_info=task.exception())
            if self.keep_alive is task and self.alive:
                log.info("Rebooter task failed. Attempting to restart it.")
                self.keep_alive = self.loop.create_task(self.rebooter())
                self.keep_alive.add_done_callback(self.task_complete)

    async def startup(self):
        if self.start_ipc:
            log.info("IPC server starting up")
            self.ipc_process = multiprocessing.Process(target=ipc.start, daemon=True)
            self.ipc_process.start()
            log.info(f"IPC process started with PID {self.ipc_process.pid}")

        try:
            current_shard_count = self.get_shard_count()
        except ValueError as e:
            log.critical(f"Could not determine shard count: {e}. Aborting startup.")
            return

        shards = list(range(current_shard_count))
        size = [
            shards[x : x + shardsPerCluster]
            for x in range(0, len(shards), shardsPerCluster)
        ]
        log.info(f"Preparing {len(size)} clusters for {current_shard_count} shards.")
        for shard_ids in size:
            cluster_name = next(NAMES, f"Cluster-{len(self.clusters) + 1}")
            self.cluster_queue.append(
                Cluster(self, cluster_name, shard_ids, current_shard_count)
            )

        await self.start_all_clusters()
        self.keep_alive = self.loop.create_task(self.rebooter())
        self.keep_alive.add_done_callback(self.task_complete)
        log.info(f"Startup completed in {time.perf_counter()-self.init:.2f}s")

    async def shutdown(self):
        log.info("Shutting down clusters...")
        self.alive = False
        if self.keep_alive:
            self.keep_alive.cancel()

        for cluster in self.clusters:
            log.info(f"Stopping Cluster#{cluster.name}")
            cluster.stop()

        if self.ipc_process and self.ipc_process.is_alive():
            log.info(f"Stopping IPC process (PID: {self.ipc_process.pid})")
            try:
                os.kill(self.ipc_process.pid, signal.SIGINT)
                self.ipc_process.join(timeout=5)
                if self.ipc_process.is_alive():
                    log.warning(
                        f"IPC process (PID: {self.ipc_process.pid}) did not terminate gracefully, forcing."
                    )
                    self.ipc_process.terminate()
                self.ipc_process.close()
            except Exception as e:
                log.error(f"Error stopping IPC process: {e}")

        log.info("Launcher shutdown sequence complete.")

    def get_cluster_name(self):
        if self.clusters:
            return self.clusters[0].name
        return None

    async def rebooter(self):
        while self.alive:
            if not self.clusters and self.alive:
                log.warning("All clusters appear to be dead.")

            if self.start_ipc and self.ipc_process and not self.ipc_process.is_alive():
                log.critical(
                    "IPC websocket server process died. Attempting to restart IPC..."
                )
                self.ipc_process = multiprocessing.Process(
                    target=ipc.start, daemon=True
                )
                self.ipc_process.start()
                log.info(f"IPC process restarted with PID {self.ipc_process.pid}")

            to_remove = []
            for cluster in self.clusters:
                if not cluster.process or not cluster.process.is_alive():
                    log.warning(f"Cluster#{cluster.name} process is not alive.")
                    if cluster.process and cluster.process.exitcode != 0:
                        log.error(
                            f"Cluster#{cluster.name} exited with code {cluster.process.exitcode}. Attempting restart."
                        )
                        try:
                            await cluster.start(force=True)
                            log.info(f"Cluster#{cluster.name} restarted.")
                        except Exception as e:
                            log.error(f"Failed to restart Cluster#{cluster.name}: {e}")
                            to_remove.append(cluster)
                    else:
                        log.info(f"Cluster#{cluster.name} exited cleanly.")
                        to_remove.append(cluster)

            for rem_cluster in to_remove:
                log.info(f"Removing cluster {rem_cluster.name} from active list.")
                self.clusters.remove(rem_cluster)

            if self.cluster_queue and self.alive:
                log.info(
                    f"{len(self.cluster_queue)} clusters in queue. Attempting to start one."
                )
                await self.start_cluster_from_queue()

            await asyncio.sleep(15)

    async def start_all_clusters(self):
        while self.cluster_queue:
            await self.start_cluster_from_queue()
        log.info("All initial clusters launched.")

    async def start_cluster_from_queue(self):
        if self.cluster_queue:
            cluster_to_start = self.cluster_queue.pop(0)
            log.info(f"Starting Cluster#{cluster_to_start.name}")
            try:
                success = await cluster_to_start.start()
                if success:
                    log.info(f"Cluster#{cluster_to_start.name} started successfully.")
                    self.clusters.append(cluster_to_start)
                else:
                    log.error(
                        f"Cluster#{cluster_to_start.name} failed to start. Re-queueing."
                    )
                    self.cluster_queue.append(cluster_to_start)
            except Exception as e:
                log.error(
                    f"Exception starting Cluster#{cluster_to_start.name}: {e}. Re-queueing."
                )
                self.cluster_queue.append(cluster_to_start)
            await asyncio.sleep(5)


class Cluster:
    def __init__(self, launcher, name, shard_ids, max_shards):
        self.launcher = launcher
        self.process = None

        self.kwargs = dict(
            token=TOKEN,
            command_prefix="-",
            shard_ids=shard_ids,
            shard_count=max_shards,
            cluster_name=name,
        )
        self.name = name
        self.log = logging.getLogger(f"Cluster#{name}")
        self.log.setLevel(logging.DEBUG)
        if not self.log.handlers:
            hdlr = logging.StreamHandler()
            hdlr.setFormatter(
                logging.Formatter(f"[Cluster#{name}] %(levelname)s %(message)s")
            )
            self.log.handlers = [hdlr]
        self.log.info(
            f"Initialized with shard ids {shard_ids}, total shards {max_shards}"
        )

    def wait_close(self):
        if self.process:
            return self.process.join()
        return None

    async def start(self, *, force=False):
        if self.process and self.process.is_alive():
            if not force:
                self.log.warning(
                    "Start called with already running cluster, pass `force=True` to override"
                )
                return False
            self.log.info("Terminating existing process for forced restart.")
            self.process.terminate()
            self.process.join(timeout=5)
            if self.process.is_alive():
                self.log.warning("Process did not terminate, attempting kill.")
                self.process.kill()
                self.process.join(timeout=5)
            self.process.close()
            self.process = None

        stdout_conn, stdin_conn = multiprocessing.Pipe(duplex=False)

        current_kwargs = self.kwargs.copy()
        current_kwargs["pipe"] = stdin_conn

        try:
            self.process = multiprocessing.Process(
                target=ClusterBot, kwargs=current_kwargs, daemon=True
            )
            self.process.start()
            self.log.info(f"Process started with PID {self.process.pid}")

            stdin_conn.close()

            signal_received = await self.launcher.loop.run_in_executor(
                None, stdout_conn.recv
            )

            if signal_received == 1:
                self.log.info("Process signaled successful startup.")
                stdout_conn.close()
                return True
            else:
                self.log.error(
                    f"Process signaled failure or unexpected value: {signal_received}. Terminating."
                )
                self.process.terminate()
                self.process.join(timeout=5)
                stdout_conn.close()
                return False
        except Exception as e:
            self.log.error(f"Exception during cluster start: {e}")
            if self.process and self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=5)
            if "stdout_conn" in locals() and not stdout_conn.closed:
                stdout_conn.close()
            if "stdin_conn" in locals() and not stdin_conn.closed:
                stdin_conn.close()
            return False

    def stop(self, sign=signal.SIGINT):
        self.log.info(f"Shutting down with signal {sign!r}")
        if self.process and self.process.is_alive():
            try:
                os.kill(self.process.pid, sign)
            except ProcessLookupError:
                self.log.warning(
                    f"Process with PID {self.process.pid} not found. Already terminated?"
                )
            except Exception as e:
                self.log.error(
                    f"Error sending signal {sign!r} to PID {self.process.pid}: {e}"
                )
        else:
            self.log.info("Process not running or already terminated.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s"
    )

    main_loop = asyncio.get_event_loop()

    launcher_instance = Launcher(main_loop, ipc_enabled=True)

    try:
        launcher_instance.start()
    except KeyboardInterrupt:
        log.info("Launcher main KeyboardInterrupt. Shutting down.")
        if not main_loop.is_closed():
            main_loop.run_until_complete(launcher_instance.shutdown())
    finally:
        log.info("Launcher process exiting.")
        if not main_loop.is_closed():
            main_loop.close()
