import discord
import logging
import os
import asyncio
import aiohttp
import json
from plugin_manager import PluginManager
from database import Db
from datadog import DDAgent
from backdoor import make_console

log = logging.getLogger('discord')


class Mee6(discord.Client):
    """A modified discord.Client class

    This mod dispatches most events to the different plugins.

    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.redis_url = kwargs.get('redis_url')
        self.mongo_url = kwargs.get('mongo_url')
        self.dd_agent_url = kwargs.get('dd_agent_url')
        self.sentry_dsn = kwargs.get('sentry_dsn')
        self.db = Db(self.redis_url, self.mongo_url, self.loop)
        self.plugin_manager = PluginManager(self)
        self.plugin_manager.load_all()
        self.last_messages = []
        self.stats = DDAgent(self.dd_agent_url)

    def run(self, *args):
        console_coro = self.loop.create_server(make_console(self),
                                               '127.0.0.1', 8000)
        self.loop.run_until_complete(console_coro)
        self.loop.run_until_complete(self.start(*args))

    async def console(reader, writer):
        loop = asyncio.get_event_loop()
        data = await reader.read(100)
        message = data.decode()
        result = eval(message)
        writer.write(result)
        await writer.drain()
        writer.close()

    def send_monitoring_message(self, msg):
        self.loop.create_task(self._send_monitoring_message(msg))

    async def _send_monitoring_message(self, msg):
        url = os.getenv('MONITORING_WH')
        headers = {}
        headers['Content-Type'] = 'application/json'
        data = json.dumps({'content': msg})
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data) as d:
                pass

    async def on_socket_raw_receive(self, payload):
        tags = {'op': str(payload['op']),
                't': payload.get('t') or "NONE"}
        self.stats.incr('discord.event', tags=tags)

        if payload['op'] == 0:
            if payload['t'] == 'READY':
                if hasattr(self, 'shard_id'):
                    msg = "**[READY]** shard {}/{}".format(self.shard_id,
                                                           self.shard_count)
                    self.send_monitoring_message(msg)
            if payload['t'] == 'RESUMED':
                if hasattr(self, 'shard_id'):
                    msg = "**[RESUMED]** shard {}/{}".format(self.shard_id,
                                                             self.shard_count)
                    self.send_monitoring_message(msg)

    async def on_ready(self):
        """Called when the bot is ready.

        Connects to the database
        Dispatched all the ready events

        """
        log.info('Connected to the database')

        await self.add_all_servers()
        for plugin in self.plugins:
            self.loop.create_task(plugin.on_ready())

    async def add_all_servers(self):
        """Syncing all the servers to the DB"""
        log.debug('Syncing servers and db')
        for server in self.servers:
            self.stats.set('mee6.servers', server.id)
            log.debug('Adding server {}\'s id to db'.format(server.id))
            await self.db.redis.sadd('servers', server.id)
            if server.name:
                await self.db.redis.set(
                    'server:{}:name'.format(server.id),
                    server.name
                )
            if server.icon:
                await self.db.redis.set(
                    'server:{}:icon'.format(server.id),
                    server.icon
                )

    async def on_server_join(self, server):
        """Called when joining a new server"""

        self.stats.set('mee6.servers', server.id)
        self.stats.incr('mee6.server_join')
        await self.db.redis.sadd('servers', server.id)

        log.info('Joined {} server : {} !'.format(
            server.owner.name,
            server.name
        ))
        log.debug('Adding server {}\'s id to db'.format(server.id))
        await self.db.redis.set('server:{}:name'.format(server.id), server.name)
        if server.icon:
            await self.db.redis.set(
                'server:{}:icon'.format(server.id),
                server.icon
            )
        # Dispatching to global plugins
        for plugin in self.plugins:
            if plugin.is_global:
                self.loop.create_task(plugin.on_server_join(server))

    async def on_server_remove(self, server):
        """Called when leaving or kicked from a server

        Removes the server from the db.

        """
        log.info('Leaving {} server : {} !'.format(
            server.owner.name,
            server.name
        ))
        log.debug('Removing server {}\'s id from the db'.format(
            server.id
        ))
        await self.db.redis.srem('servers', server.id)

    async def get_plugins(self, server):
        plugins = await self.plugin_manager.get_all(server)
        return plugins

    async def send_message(self, *args, **kwargs):
        self.stats.incr('mee6.sent_messages')
        return await super().send_message(*args, **kwargs)

    async def on_message(self, message):
        self.stats.incr('mee6.recv_messages')
        if message.channel.is_private:
            return

        wh_key = 'channel.{}.last_message_whid'.format(message.channel.id)
        if message.webhook_id:
            self.db.redis.set(wh_key, message.webhook_id)
        else:
            self.db.redis.delete(wh_key)

        if message.author.__class__ != discord.Member:
            return

        server = message.server

        if message.content == "!shard?":
            if hasattr(self, 'shard_id'):
                await self.send_message(
                    message.channel,
                    "shard {}/{}".format(self.shard_id, self.shard_count)
                )

        enabled_plugins = await self.get_plugins(server)
        for plugin in enabled_plugins:
            self.loop.create_task(plugin._on_message(message))

    async def on_message_edit(self, before, after):
        if before.channel.is_private:
            return

        server = after.server
        enabled_plugins = await self.get_plugins(server)
        for plugin in enabled_plugins:
            self.loop.create_task(plugin.on_message_edit(before, after))

    async def on_message_delete(self, message):
        if message.channel.is_private:
            return

        server = message.server
        enabled_plugins = await self.get_plugins(server)
        for plugin in enabled_plugins:
            self.loop.create_task(plugin.on_message_delete(message))

    async def on_channel_create(self, channel):
        if channel.is_private:
            return

        server = channel.server
        enabled_plugins = await self.get_plugins(server)
        for plugin in enabled_plugins:
            self.loop.create_task(plugin.on_channel_create(channel))

    async def on_channel_update(self, before, after):
        if before.is_private:
            return

        server = after.server
        enabled_plugins = await self.get_plugins(server)
        for plugin in enabled_plugins:
            self.loop.create_task(plugin.on_channel_update(before, after))

    async def on_channel_delete(self, channel):
        if channel.is_private:
            return

        server = channel.server
        enabled_plugins = await self.get_plugins(server)
        for plugin in enabled_plugins:
            self.loop.create_task(plugin.on_channel_delete(channel))

    async def on_member_join(self, member):
        server = member.server
        enabled_plugins = await self.get_plugins(server)
        for plugin in enabled_plugins:
            self.loop.create_task(plugin.on_member_join(member))

    async def on_member_remove(self, member):
        server = member.server
        enabled_plugins = await self.get_plugins(server)
        for plugin in enabled_plugins:
            self.loop.create_task(plugin.on_member_remove(member))

    async def on_member_update(self, before, after):
        server = after.server
        enabled_plugins = await self.get_plugins(server)
        for plugin in enabled_plugins:
            self.loop.create_task(plugin.on_member_update(before, after))

    async def on_server_update(self, before, after):
        server = after
        enabled_plugins = await self.get_plugins(server)
        for plugin in enabled_plugins:
            self.loop.create_task(plugin.on_server_update(before, after))
