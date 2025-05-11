# filepath: c:\Users\alexb\OneDrive\Bureau\Trucs\AwaBot\Flake\ipc.py
import asyncio
import signal
import websockets
import json  # Added for json operations
import logging  # Added for logging

# Setup logging for IPC server
ipc_logger = logging.getLogger("IPCServer")
ipc_logger.setLevel(logging.INFO)
# Add a stream handler if not already configured (e.g., by a root logger)
if not ipc_logger.handlers:
    stream_handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    stream_handler.setFormatter(formatter)
    ipc_logger.addHandler(stream_handler)

CLIENTS = {}  # Stores {cluster_name: websocket_client}


async def dispatch(data, sender_cluster_name=None):
    """Dispatches data to all connected clients except the sender (if specified)."""
    if isinstance(data, bytes):
        try:
            data_str = data.decode("utf-8")
        except UnicodeDecodeError:
            ipc_logger.error(f"Could not decode bytes to UTF-8: {data!r}")
            return
    elif isinstance(data, str):
        data_str = data
    else:
        ipc_logger.error(f"Dispatch received data of unexpected type: {type(data)}")
        return

    for cluster_name, client in CLIENTS.items():
        if client == sender_cluster_name:
            continue
        if client.open:
            try:
                await client.send(data_str)
                ipc_logger.info(f"> Sent to Cluster[{cluster_name}]")
            except websockets.exceptions.ConnectionClosed:
                ipc_logger.warning(
                    f"Attempted to send to Cluster[{cluster_name}] but connection was closed."
                )
            except Exception as e:
                ipc_logger.error(f"Error sending to Cluster[{cluster_name}]: {e}")
        else:
            ipc_logger.warning(f"Cluster[{cluster_name}] websocket not open. Skipping.")


# pylint: disable=unused-argument
async def serve(websocket, path):
    """Handles new websocket connections from bot clusters."""
    peer_name = websocket.remote_address
    ipc_logger.info(f"New connection attempt from {peer_name}")
    cluster_name_bytes = await websocket.recv()

    try:
        cluster_name = cluster_name_bytes.decode("utf-8")
    except UnicodeDecodeError:
        ipc_logger.error(
            f"Failed to decode cluster name from {peer_name}: {cluster_name_bytes!r}"
        )
        await websocket.close(1003, "Invalid cluster name encoding")
        return

    if not cluster_name.strip():
        ipc_logger.warning(
            f"Empty cluster name received from {peer_name}. Closing connection."
        )
        await websocket.close(1008, "Cluster name cannot be empty")
        return

    if cluster_name in CLIENTS:
        ipc_logger.warning(
            f"! Cluster[{cluster_name}] from {peer_name} attempted reconnection while already connected."
        )
        await websocket.close(4029, "already connected")
        return

    CLIENTS[cluster_name] = websocket
    ipc_logger.info(f"$ Cluster[{cluster_name}] ({peer_name}) connected successfully.")

    try:
        await websocket.send(
            json.dumps({"status": "ok", "message": f"Connected as {cluster_name}"})
        )

        async for msg in websocket:
            ipc_logger.info(f"< Received from Cluster[{cluster_name}]: {msg!r}")
            await dispatch(msg, sender_cluster_name=cluster_name)

    except websockets.exceptions.ConnectionClosedError as e:
        ipc_logger.info(f"ConnectionClosedError for Cluster[{cluster_name}]: {e}")
    except websockets.exceptions.ConnectionClosedOK as e:
        ipc_logger.info(f"ConnectionClosedOK for Cluster[{cluster_name}]: {e}")
    except Exception as e:
        ipc_logger.error(
            f"Error during communication with Cluster[{cluster_name}]: {e}",
            exc_info=True,
        )
        if websocket.open:
            await websocket.close(1011, "Internal server error")
    finally:
        if cluster_name in CLIENTS:
            CLIENTS.pop(cluster_name)
            ipc_logger.info(f"$ Cluster[{cluster_name}] ({peer_name}) disconnected.")
        else:
            ipc_logger.info(
                f"Connection from {peer_name} ended before full registration or was already removed."
            )


# pylint: enable=unused-argument


async def main():
    """Main coroutine to start the IPC server."""
    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    try:
        if hasattr(signal, "SIGINT"):
            loop.add_signal_handler(signal.SIGINT, stop.set_result, None)
        if hasattr(signal, "SIGTERM"):
            loop.add_signal_handler(signal.SIGTERM, stop.set_result, None)
    except (
        NotImplementedError,
        RuntimeError,
    ):  # Catch RuntimeError as well, as it can be raised in some cases
        ipc_logger.info(
            "Signal handlers SIGINT/SIGTERM are not fully supported on this platform for asyncio. "
            "The IPC server will rely on the parent process for termination signals."
        )

    host = "localhost"
    port = 6001

    ipc_logger.info(f"Starting IPC server on {host}:{port}")
    try:
        async with websockets.serve(serve, host, port) as server:
            ipc_logger.info(f"IPC Server is running.")
            await stop
    except OSError as e:
        ipc_logger.critical(f"Could not start IPC server on {host}:{port}: {e}")
        return
    except Exception as e:
        ipc_logger.critical(
            f"An unexpected error occurred with the IPC server: {e}", exc_info=True
        )
    finally:
        ipc_logger.info("IPC Server shutting down...")
        for ws_client in CLIENTS.values():
            if ws_client.open:
                await ws_client.close(1001, "Server shutting down")
        ipc_logger.info("IPC Server has shut down.")


def start():
    """Entry point for starting the IPC server process."""
    if not ipc_logger.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        ipc_logger.info("IPC process received KeyboardInterrupt. Shutting down.")
    except Exception as e:
        ipc_logger.critical(f"IPC process failed critically: {e}", exc_info=True)
    finally:
        ipc_logger.info("IPC process finished.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    ipc_logger.info("IPC Server starting directly (not as a child process).")
    start()
