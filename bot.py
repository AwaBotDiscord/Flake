import re
from datetime import timedelta
import discord
from discord.ext import commands
import asyncio
import logging


class ClusterBot(commands.AutoShardedBot):
    def __init__(self, **kwargs):
        self.pipe = kwargs.pop("pipe")
        self.cluster_name = kwargs.pop("cluster_name")
        intents = kwargs.get("intents")
        if not intents:
            intents = discord.Intents.default()
            intents.message_content = True

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        super().__init__(**kwargs, loop=loop, intents=intents)

        self.websocket = None
        self._last_result = None
        self.ws_task = None
        self.responses = asyncio.Queue()

        log = logging.getLogger(f"Cluster#{self.cluster_name}")
        log.setLevel(logging.DEBUG)
        if not log.handlers:
            fh = logging.FileHandler(
                f"cluster-{self.cluster_name}.log", encoding="utf-8", mode="a"
            )
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            fh.setFormatter(formatter)
            log.addHandler(fh)

        self.log = log
        self.log.info(
            f"Initializing Cluster#{self.cluster_name} with Shard IDs: {kwargs.get('shard_ids')}, Total Shards: {kwargs.get('shard_count')}"
        )

        try:
            token = kwargs["token"]
            self.run(token)
        except KeyError:
            self.log.critical("Bot token not provided in kwargs!")
            if self.pipe:
                try:
                    self.pipe.send(-1)
                except Exception as e:
                    self.log.error(f"Failed to send error signal via pipe: {e}")
                finally:
                    self.pipe.close()
            raise
        except Exception as e:
            self.log.critical(f"An error occurred during bot run: {e}", exc_info=True)
            if self.pipe:
                try:
                    self.pipe.send(-2)
                except Exception as pe:
                    self.log.error(f"Failed to send run error signal via pipe: {pe}")
                finally:
                    self.pipe.close()
            raise

    async def on_ready(self):
        self.log.info(
            f"[Cluster#{self.cluster_name}] Logged in as {self.user} (ID: {self.user.id}). Checking for application commands..."
        )

        if not self.application_commands:
            self.log.warning(
                f"[Cluster#{self.cluster_name}] No application commands found on the bot instance."
            )
        else:
            self.log.info(
                f"[Cluster#{self.cluster_name}] Found {len(self.application_commands)} application commands registered on the bot instance:"
            )
            for command in self.application_commands:
                self.log.info(
                    f"  - Command Name: {command.name}, Type: {type(command)}"
                )
                if hasattr(command, "description") and command.description:
                    self.log.info(f"    Description: {command.description}")
                if hasattr(command, "guild_ids") and command.guild_ids is not None:
                    self.log.info(f"    Guild IDs: {command.guild_ids}")
                else:
                    self.log.info(f"    Scope: Global")

        self.log.info(
            f"[Cluster#{self.cluster_name}] Attempting to sync application commands with Discord..."
        )
        try:
            await self.sync_commands()
            self.log.info(
                f"[Cluster#{self.cluster_name}] Application commands sync process initiated/completed."
            )
        except discord.HTTPException as e:
            self.log.error(
                f"[Cluster#{self.cluster_name}] A Discord API error occurred during command sync: {e.status} {e.text if hasattr(e, 'text') else 'No text available'}",
                exc_info=True,
            )
        except Exception as e:
            self.log.error(
                f"[Cluster#{self.cluster_name}] An unexpected error occurred during command sync: {e}",
                exc_info=True,
            )

        if self.pipe:
            try:
                if not self.pipe.closed:
                    self.pipe.send(1)
                    self.log.info("Sent success signal to launcher.")
                else:
                    self.log.warning(
                        "Pipe was already closed before attempting to send success signal."
                    )
            except Exception as e:
                self.log.error(
                    f"Failed to send success signal via pipe: {e}", exc_info=True
                )
            finally:
                if (
                    self.pipe and not self.pipe.closed
                ):  # Ensure pipe exists and is not closed before closing
                    self.pipe.close()
                    self.log.info("Pipe closed after on_ready.")
                elif self.pipe and self.pipe.closed:
                    self.log.info("Pipe was already closed when on_ready finished.")
                # If self.pipe is None, do nothing

    async def close(self, *args, **kwargs):
        self.log.info(f"Cluster#{self.cluster_name} shutting down...")
        if self.websocket:
            await self.websocket.close()
            self.log.info("IPC Websocket connection closed.")
        if self.ws_task and not self.ws_task.done():
            self.ws_task.cancel()
            try:
                await self.ws_task
            except asyncio.CancelledError:
                self.log.info("IPC Websocket task cancelled.")
            except Exception as e:
                self.log.error(
                    f"Error during IPC websocket task shutdown: {e}", exc_info=True
                )

        await super().close()
        self.log.info(f"Cluster#{self.cluster_name} has been closed.")

    async def on_shard_ready(self, shard_id):
        self.log.info(f"[Cluster#{self.cluster_name}] Shard {shard_id} is ready.")

    async def on_shard_connect(self, shard_id):
        self.log.info(f"[Cluster#{self.cluster_name}] Shard {shard_id} connected.")

    @discord.slash_command(name="ping")
    async def ping_command(self, ctx):
        """Responds with pong!"""
        await ctx.respond(f"Pong! Latency: {self.latency*1000:.2f}ms")
